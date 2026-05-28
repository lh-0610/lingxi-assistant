import os
import json
import time
import difflib
import subprocess
import urllib.parse
import urllib.request
import urllib.error
from langchain_core.tools import tool

from . import state
from . import checkpoint as _checkpoint
from .paths import _app_data_dir, logger
from .limits import (
    READ_FILE_DEFAULT_LIMIT,
    RUN_COMMAND_MAX_OUTPUT_CHARS,
    RUN_COMMAND_TIMEOUT_S,
    SEARCH_FILES_MAX_RESULTS,
    SEARCH_IN_FILE_DEFAULT_LIMIT,
    SEARCH_IN_FILE_MAX_LIMIT,
)


# ══════════════════════════════════════
# ComfyUI 集成（本机优先，挂了回退 Pollinations）
# ══════════════════════════════════════


def _load_comfy_config():
    """从 config.json 读 ComfyUI 配置"""
    try:
        with open(os.path.join(_app_data_dir(), "config.json"), "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
        return {
            "base_url": cfg.get("comfy_base_url", "").rstrip("/"),
            "workflow_path": cfg.get("comfy_workflow_path", ""),
            "checkpoint": cfg.get("comfy_checkpoint", "sd_xl_base_1.0.safetensors"),
            "vae": cfg.get("comfy_vae", ""),
            "negative_prompt": cfg.get("comfy_negative_prompt", ""),
            # [{"name": "xxx.safetensors", "strength_model": 0.7, "strength_clip": 0.7}, ...]
            # 兼容旧写法: {"name": "...", "strength": 0.7}
            "loras": cfg.get("comfy_loras", []),
            # 是否启用 FaceDetailer（需要装 Impact Pack）
            "face_detailer": cfg.get("comfy_face_detailer", True),
        }
    except Exception:
        return {"base_url": "", "workflow_path": "", "checkpoint": "", "vae": "", "negative_prompt": "", "loras": [], "face_detailer": False}


def _detect_model_style(checkpoint):
    """根据 checkpoint 文件名识别模型风格，返回 (style, is_vpred)
    style: 'pony' / 'noobai' / 'illustrious' / 'sdxl'
    is_vpred: 是否 v-prediction（noobAI vPred 等）
    """
    name = checkpoint.lower()
    is_vpred = "vpred" in name or "v-pred" in name or "v_pred" in name
    # AutismMix 是 Pony finetune，用 score_X 标签
    if "autism" in name or "pony" in name:
        return "pony", is_vpred
    if "noob" in name or "nai" in name:
        return "noobai", is_vpred
    if "illustrious" in name or "illust" in name:
        return "illustrious", is_vpred
    return "sdxl", is_vpred


def _quality_prefix(style):
    """按模型风格返回 prompt 前置质量标签"""
    return {
        "pony":        "score_9, score_8_up, score_7_up, source_anime, rating_safe, ",
        # NoobAI 默认偏半写实，强制叠 anime / 2d 标签拉回二次元
        "noobai":      ("masterpiece, best quality, very aesthetic, absurdres, newest, "
                        "anime style, anime coloring, 2d, "),
        "illustrious": "masterpiece, best quality, very aesthetic, absurdres, newest, anime style, 2d, ",
        "sdxl":        "masterpiece, best quality, highly detailed, ",
    }.get(style, "")


def _negative_prompt(style):
    """按模型风格返回负向 prompt"""
    if style == "pony":
        return ("score_6, score_5, score_4, score_3, score_2, score_1, "
                "worst quality, low quality, blurry, watermark, signature, text, "
                "deformed, ugly, bad anatomy, extra limbs, fewer digits")
    if style in ("noobai", "illustrious"):
        # NoobAI 关键：压制旧风格 + 写实风 + 低质感
        return ("worst quality, low quality, lowres, normal quality, "
                "bad anatomy, bad hands, extra digits, fewer digits, "
                "jpeg artifacts, signature, watermark, username, "
                "ai-generated, old, early, mid, simple background, blurry, "
                "realistic, photorealistic, 3d, photo, photograph")
    return "worst quality, low quality, blurry, watermark, signature, text, deformed, ugly, bad anatomy, extra limbs"


def _comfy_default_workflow(prompt, width, height, checkpoint, vae=None, negative_prompt=None, loras=None, face_detailer=False):
    """内置默认工作流（API 格式），自动识别 Pony / NoobAI / vPred 等并适配。
    用户配 comfy_workflow_path 可完全覆盖。
    vae: 可选外部 VAE 文件名（放在 ComfyUI/models/vae 下），为空则用 checkpoint 自带 VAE。
    loras: [{"name": "xxx.safetensors", "strength_model": 0.7, "strength_clip": 0.7}, ...] 自动串联。
    兼容旧写法: [{"name": "xxx.safetensors", "strength": 0.7}, ...]
    face_detailer: 是否在末尾加 FaceDetailer（需要 ComfyUI 装 Impact Pack）。
    """
    import random
    style, is_vpred = _detect_model_style(checkpoint)
    full_prompt = _quality_prefix(style) + prompt
    negative = _negative_prompt(style)
    if negative_prompt:
        negative = f"{negative}, {negative_prompt}"

    # 默认参数按模型类型调整（社区实测推荐值）
    if style == "pony":
        steps, cfg, sampler, scheduler = 28, 7.0, "euler_ancestral", "normal"
    elif style == "noobai":
        # NoobAI vPred 实测：CFG 3.5-5、euler/euler_ancestral、karras 调度器画质更好
        steps, cfg, sampler, scheduler = 30, 4.5, "euler_ancestral", "karras"
    elif style == "illustrious":
        steps, cfg, sampler, scheduler = 28, 5.0, "euler_ancestral", "normal"
    else:
        steps, cfg, sampler, scheduler = 20, 7.0, "euler", "normal"

    # vPred 模型必须加 ModelSamplingDiscrete 节点切到 v_prediction 采样
    workflow = {
        "4": {
            "inputs": {"ckpt_name": checkpoint},
            "class_type": "CheckpointLoaderSimple",
        },
        "5": {
            "inputs": {"width": width, "height": height, "batch_size": 1},
            "class_type": "EmptyLatentImage",
        },
        "6": {
            "inputs": {"text": full_prompt, "clip": ["4", 1]},
            "class_type": "CLIPTextEncode",
        },
        "7": {
            "inputs": {"text": negative, "clip": ["4", 1]},
            "class_type": "CLIPTextEncode",
        },
        "8": {
            "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
            "class_type": "VAEDecode",
        },
        "9": {
            "inputs": {"filename_prefix": "lingxi", "images": ["8", 0]},
            "class_type": "SaveImage",
        },
    }
    vae_ref = ["4", 2]
    if vae:
        workflow["12"] = {
            "inputs": {"vae_name": vae},
            "class_type": "VAELoader",
        }
        vae_ref = ["12", 0]
        workflow["8"]["inputs"]["vae"] = vae_ref

    # ── LoRA 链：把 Checkpoint 的 (model, clip) 输出串过多个 LoraLoader ──
    current_model_ref = ["4", 0]   # CheckpointLoader 的 model 输出
    current_clip_ref = ["4", 1]    # CheckpointLoader 的 clip 输出
    lora_node_id = 100             # LoRA 节点从 100 开始编号，避免跟其它节点冲突
    if loras:
        for lora in loras:
            name = lora.get("name") if isinstance(lora, dict) else lora
            if isinstance(lora, dict):
                fallback_strength = lora.get("strength", 0.7)
                strength_model = lora.get("strength_model", fallback_strength)
                strength_clip = lora.get("strength_clip", fallback_strength)
            else:
                strength_model = 0.7
                strength_clip = 0.7
            if not name:
                continue
            workflow[str(lora_node_id)] = {
                "inputs": {
                    "lora_name": name,
                    "strength_model": strength_model,
                    "strength_clip": strength_clip,
                    "model": current_model_ref,
                    "clip": current_clip_ref,
                },
                "class_type": "LoraLoader",
            }
            current_model_ref = [str(lora_node_id), 0]
            current_clip_ref = [str(lora_node_id), 1]
            lora_node_id += 1

    # CLIP 编码节点要用串联后的 clip
    workflow["6"]["inputs"]["clip"] = current_clip_ref
    workflow["7"]["inputs"]["clip"] = current_clip_ref

    if is_vpred:
        # 加 ModelSamplingDiscrete，把模型切到 v_prediction 模式（接在 LoRA 之后）
        workflow["10"] = {
            "inputs": {
                "sampling": "v_prediction",
                "zsnr": True,
                "model": current_model_ref,
            },
            "class_type": "ModelSamplingDiscrete",
        }
        current_model_ref = ["10", 0]

    # 加 FreeU_V2 免费提质（SDXL 推荐参数）—— 接在所有调整之后
    workflow["11"] = {
        "inputs": {
            "b1": 1.3, "b2": 1.4, "s1": 0.9, "s2": 0.2,
            "model": current_model_ref,
        },
        "class_type": "FreeU_V2",
    }
    model_ref = ["11", 0]

    workflow["3"] = {
        "inputs": {
            "seed": random.randint(1, 2**31 - 1),
            "steps": steps,
            "cfg": cfg,
            "sampler_name": sampler,
            "scheduler": scheduler,
            "denoise": 1,
            "model": model_ref,
            "positive": ["6", 0],
            "negative": ["7", 0],
            "latent_image": ["5", 0],
        },
        "class_type": "KSampler",
    }

    # ── FaceDetailer 修脸（需 Impact Pack 已装） ──
    if face_detailer:
        # 12: YOLO 人脸检测器加载
        workflow["20"] = {
            "inputs": {"model_name": "bbox/face_yolov8m.pt"},
            "class_type": "UltralyticsDetectorProvider",
        }
        # 13: FaceDetailer 节点 —— 输入原图 + 同套 model/clip/vae/conditioning
        workflow["21"] = {
            "inputs": {
                "image": ["8", 0],
                "model": model_ref,
                "clip": current_clip_ref,
                "vae": vae_ref,
                "positive": ["6", 0],
                "negative": ["7", 0],
                "bbox_detector": ["20", 0],
                # 修脸专用采样参数
                "guide_size": 512,
                "guide_size_for": True,
                "max_size": 1024,
                "seed": random.randint(1, 2**31 - 1),
                "steps": 20,
                "cfg": cfg,
                "sampler_name": sampler,
                "scheduler": scheduler,
                "denoise": 0.4,           # 0.4 ~ 0.6 修脸不变脸
                "feather": 5,
                "noise_mask": True,
                "force_inpaint": True,
                "bbox_threshold": 0.5,
                "bbox_dilation": 10,
                "bbox_crop_factor": 3.0,
                "sam_detection_hint": "center-1",
                "sam_dilation": 0,
                "sam_threshold": 0.93,
                "sam_bbox_expansion": 0,
                "sam_mask_hint_threshold": 0.7,
                "sam_mask_hint_use_negative": "False",
                "drop_size": 10,
                "wildcard": "",
                "cycle": 1,
                "inpaint_model": False,
                "noise_mask_feather": 20,
            },
            "class_type": "FaceDetailer",
        }
        # SaveImage 改成保存修脸后的图（13.0 是 FaceDetailer 输出的 image）
        workflow["9"]["inputs"]["images"] = ["21", 0]

    return workflow


def _comfy_available(base_url, timeout=2):
    """检测 ComfyUI 是否在线"""
    if not base_url:
        return False
    try:
        with urllib.request.urlopen(f"{base_url}/system_stats", timeout=timeout):
            return True
    except Exception:
        return False


def _comfy_load_workflow(workflow_path, prompt, width, height, checkpoint, vae=None, negative_prompt=None, loras=None, face_detailer=False):
    """加载工作流：用户 JSON 优先；找 CLIPTextEncode 正向节点替换 prompt"""
    if workflow_path and os.path.exists(workflow_path):
        with open(workflow_path, "r", encoding="utf-8") as f:
            wf = json.load(f)
        # 用户工作流：把首个含 PROMPT_PLACEHOLDER 的文本替换为真实 prompt；
        # 若没有占位符就替换首个 CLIPTextEncode 的 text 字段
        placeholder_replaced = False
        for node in wf.values():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs", {})
            for k, v in list(inputs.items()):
                if isinstance(v, str) and "PROMPT_PLACEHOLDER" in v:
                    inputs[k] = v.replace("PROMPT_PLACEHOLDER", prompt)
                    placeholder_replaced = True
        if not placeholder_replaced:
            for node in wf.values():
                if isinstance(node, dict) and node.get("class_type") == "CLIPTextEncode":
                    node["inputs"]["text"] = prompt
                    break
        # 尺寸也尝试覆盖 EmptyLatentImage
        for node in wf.values():
            if isinstance(node, dict) and node.get("class_type") == "EmptyLatentImage":
                node["inputs"]["width"] = width
                node["inputs"]["height"] = height
        return wf
    return _comfy_default_workflow(prompt, width, height, checkpoint, vae, negative_prompt, loras, face_detailer)


def _call_comfy(prompt, width, height):
    """调用本机 ComfyUI 生成图，返回 PNG 二进制；失败抛异常。"""
    cfg = _load_comfy_config()
    base_url = cfg["base_url"]
    if not _comfy_available(base_url):
        raise RuntimeError("ComfyUI 未在线")

    workflow = _comfy_load_workflow(
        cfg["workflow_path"], prompt, width, height, cfg["checkpoint"],
        cfg.get("vae"), cfg.get("negative_prompt"), cfg.get("loras"), cfg.get("face_detailer", False)
    )
    # 1. 提交任务
    body = json.dumps({"prompt": workflow}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/prompt",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    prompt_id = result.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI 提交失败: {result}")

    # 2. 轮询完成
    img_info = None
    for _ in range(180):  # 最多等 3 分钟
        time.sleep(1)
        try:
            with urllib.request.urlopen(
                f"{base_url}/history/{prompt_id}", timeout=5
            ) as resp:
                history = json.loads(resp.read().decode("utf-8"))
            if prompt_id in history:
                outputs = history[prompt_id].get("outputs", {})
                for node_output in outputs.values():
                    if "images" in node_output and node_output["images"]:
                        img_info = node_output["images"][0]
                        break
                if img_info:
                    break
        except Exception:
            continue
    if not img_info:
        raise TimeoutError("ComfyUI 生成超时（3 分钟）")

    # 3. 下载结果
    params = urllib.parse.urlencode({
        "filename": img_info["filename"],
        "subfolder": img_info.get("subfolder", ""),
        "type": img_info.get("type", "output"),
    })
    with urllib.request.urlopen(f"{base_url}/view?{params}", timeout=30) as resp:
        return resp.read()


def _project_cwd() -> str:
    """所有命令 / 文件工具的有效工作目录。

    优先用 state.current_project（用户在侧边栏切换的项目根）；
    没设或路径已不存在时，回退到 Python 进程的 cwd（一般是项目源码目录）。
    """
    proj = getattr(state, "current_project", None)
    if proj and os.path.isdir(proj):
        return proj
    return os.getcwd()


def _resolve_path(path: str) -> str:
    """相对路径按当前项目根解析；绝对路径原样返回。"""
    if not path:
        return path
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(_project_cwd(), path))


@tool
def read_file(path: str, offset: int = 1, limit: int = READ_FILE_DEFAULT_LIMIT) -> str:
    """读取文件内容，按行返回（带行号前缀，方便后续 edit_file 定位）。

    参数：
      path: 文件路径（绝对或相对项目根）
      offset: 起始行号（**从 1 开始**），默认 1
      limit: 最多读取的行数，默认 2000（大文件请分批）

    返回格式（类 `cat -n`）：
        1: import os
        2: import sys
        ...
        [显示第 1-50 行 / 共 200 行]

    用法：
      - 想看大文件中段：`read_file("a.py", offset=500, limit=200)`
      - 默认 2000 行通常已经够；如果文件 > 2000 行，会自动截断并提示总行数
    """
    try:
        with open(_resolve_path(path), "r", encoding="utf-8") as f:
            all_lines = f.readlines()
    except Exception as e:
        return f"读取失败: {e}"

    total = len(all_lines)
    if total == 0:
        return "（空文件）"

    # 1-indexed offset，做边界保护
    if offset < 1:
        offset = 1
    if offset > total:
        return f"[文件共 {total} 行，offset={offset} 超出范围]"
    if limit < 1:
        limit = 1

    start = offset - 1
    end = min(start + limit, total)
    lines = all_lines[start:end]

    # 行号宽度按总行数算（5 位足够 99999 行）
    width = max(4, len(str(end)))
    rendered = "\n".join(
        f"{(start + i + 1):>{width}}: {ln.rstrip()}" for i, ln in enumerate(lines)
    )

    if end >= total and offset == 1:
        footer = f"\n[完整文件，共 {total} 行]"
    elif end >= total:
        footer = f"\n[显示第 {offset}-{end} 行 / 共 {total} 行（已读到末尾）]"
    else:
        remaining = total - end
        footer = (
            f"\n[显示第 {offset}-{end} 行 / 共 {total} 行，"
            f"还有 {remaining} 行未读——继续读用 offset={end + 1}]"
        )
    return rendered + footer


def _confirm_file_write(full: str, old_content: str, new_content: str):
    """写盘前的 diff 确认（写盘类工具共用）。

    worker 线程算 unified diff → 通过 SignalBridge 投到 UI 主线程弹 diff 卡 →
    阻塞等用户点完。无 UI（CLI / 测试）时返回 (True, None) 直接放行。

    返回 (allowed, reject_message)：allowed=True 时 reject_message 为 None；
    allowed=False 时 reject_message 是给 AI 的拒绝文案。
    """
    ui = getattr(state, "ui_ref", None)
    if ui is None:
        return True, None
    import difflib as _difflib
    base = os.path.basename(full)
    diff_text = "".join(_difflib.unified_diff(
        (old_content or "").splitlines(keepends=True),
        (new_content or "").splitlines(keepends=True),
        fromfile=f"a/{base}",
        tofile=f"b/{base}",
        n=3,
    ))
    if not diff_text:
        diff_text = f"--- a/{base}\n+++ b/{base}\n(无 diff，可能是看不见的空白差异)\n"
    try:
        allowed = ui.confirm_edit(full, diff_text)
    except Exception as e:
        logger.warning(f"文件写入确认对话框异常，默认拒绝: {e}")
        return False, f"用户确认对话框出错，已拒绝写入: {e}"
    if not allowed:
        logger.info(f"用户拒绝写入 {full}")
        return False, "已拒绝：用户不允许此次写入。"
    return True, None


@tool
def write_file(path: str, content: str) -> str:
    """写入内容到文件（覆盖）。path: 文件路径, content: 要写入的内容"""
    try:
        full = _resolve_path(path)
        # 读旧内容算 diff（文件不存在视为空）
        old_content = ""
        if os.path.exists(full):
            try:
                with open(full, "r", encoding="utf-8") as f:
                    old_content = f.read()
            except Exception:
                old_content = ""
        # 写盘前确认（全量覆盖比 edit_file 更危险，必须让用户审 diff）
        allowed, reject = _confirm_file_write(full, old_content, content)
        if not allowed:
            return reject
        os.makedirs(os.path.dirname(os.path.abspath(full)), exist_ok=True)
        # 改动前打 checkpoint（git 项目自动 stash 一份，方便用户撤销）
        proj = _project_cwd()
        try:
            _checkpoint.make_checkpoint(proj, "write_file", full)
        except Exception as e:
            logger.warning(f"checkpoint 失败（不影响写入）: {e}")
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return f"成功写入文件: {full}"
    except Exception as e:
        return f"写入失败: {e}"


@tool
def append_file(path: str, content: str) -> str:
    """追加内容到文件末尾。path: 文件路径, content: 要追加的内容"""
    try:
        full = _resolve_path(path)
        # 读旧内容算 diff（追加 = 旧内容 + 新内容）
        old_content = ""
        if os.path.exists(full):
            try:
                with open(full, "r", encoding="utf-8") as f:
                    old_content = f.read()
            except Exception:
                old_content = ""
        allowed, reject = _confirm_file_write(full, old_content, old_content + content)
        if not allowed:
            return reject
        proj = _project_cwd()
        try:
            _checkpoint.make_checkpoint(proj, "append_file", full)
        except Exception as e:
            logger.warning(f"checkpoint 失败（不影响追加）: {e}")
        with open(full, "a", encoding="utf-8") as f:
            f.write(content)
        return f"成功追加到文件: {full}"
    except Exception as e:
        return f"追加失败: {e}"


def _get_indent(line):
    return line[:len(line) - len(line.lstrip())]


def _detect_indent_unit(lines):
    """从一组行里推断"一级缩进"。

    取最短的非空前导空白当一级单元。整段顶格则返回 ""（无依据）。
    例:
      ['class Foo:', '    def bar():', '        return 1']  → '    '
      ['class Foo:', '\tdef bar():',   '\t\treturn 1']      → '\t'
      ['x = 1', 'y = 2']                                     → ''  （整段顶格）
    """
    units = []
    for ln in lines:
        if not ln.strip():
            continue  # 跳过空行
        leading = ln[:len(ln) - len(ln.lstrip())]
        if leading:
            units.append(leading)
    if not units:
        return ""
    return min(units, key=len)


def _realign_indent(new_string, file_indent_unit, model_indent_unit):
    """按缩进单元换算：模型 N 级 model_unit → file 的 N 级 file_unit。

    比"首行 prefix 替换"更鲁棒:
    - 文件 / 模型首行顶格时仍能从子行推断 unit
    - tab ↔ 空格混用时按层级正确换算
    """
    if not model_indent_unit or not file_indent_unit:
        return new_string  # 任一侧整段顶格，无依据重算

    mu_len = len(model_indent_unit)
    result = []
    for line in new_string.splitlines(keepends=True):
        if not line.strip():
            result.append(line)  # 空行原样
            continue
        leading = line[:len(line) - len(line.lstrip())]
        level = len(leading) // mu_len
        result.append(file_indent_unit * level + line.lstrip())
    return "".join(result)


def _locate_edit(content: str, old: str, new_string: str, replace_all: bool):
    """分层匹配级联：L1 精确 → L2 去行尾空白 → L3 去全部首尾空白+缩进重对齐 → L4 模糊。

    返回 (status, spans, new_texts, info)：
      status: "exact" | "normalized" | "fuzzy" | "multi" | "none"
      spans: [(start_char, end_char)] 要替换的【文件真实】字符区间
      new_texts: 与 spans 对应的替换文本（L3/L4 已做缩进重对齐；L1/L2 直接用 new_string）
      info: 成功时为 (match_level_desc, line_numbers)；失败时为 (closest_snippet_desc, None)
    """
    old_lines = old.splitlines(keepends=True)
    file_lines = content.splitlines(keepends=True)
    old_line_count = len(old_lines)
    file_line_count = len(file_lines)

    if old_line_count == 0:
        return "none", [], [], ("old_string 为空行", None)

    # ── L1 精确匹配 ──
    count = content.count(old)
    if count > 0:
        if count > 1 and not replace_all:
            # 多处命中，收集行号
            line_nos = []
            idx = 0
            while True:
                idx = content.find(old, idx)
                if idx == -1:
                    break
                line_nos.append(content[:idx].count("\n") + 1)
                idx += 1
            return "multi", [], [], (f"L1 精确匹配到 {count} 处", line_nos)
        # replace_all 或唯一命中
        spans = []
        idx = 0
        while True:
            idx = content.find(old, idx)
            if idx == -1:
                break
            spans.append((idx, idx + len(old)))
            idx += len(old)
        line_no = content[:spans[0][0]].count("\n") + 1
        return "exact", spans, [new_string] * len(spans), ("L1 精确匹配", [line_no])

    # ── L2 逐行 rstrip 比对（按行滑窗）──
    def _rstrip_lines(lines):
        return [l.rstrip() for l in lines]

    old_rstripped = _rstrip_lines(old_lines)
    file_rstripped = _rstrip_lines(file_lines)
    l2_hits = []
    for i in range(file_line_count - old_line_count + 1):
        if file_rstripped[i:i + old_line_count] == old_rstripped:
            l2_hits.append(i)
    if l2_hits:
        if len(l2_hits) > 1 and not replace_all:
            line_nos = [i + 1 for i in l2_hits]
            return "multi", [], [], (f"L2 去行尾空白匹配到 {len(l2_hits)} 处", line_nos)
        # 唯一或 replace_all
        spans = []
        for start_line in l2_hits:
            char_start = sum(len(file_lines[j]) for j in range(start_line))
            char_end = sum(len(file_lines[j]) for j in range(start_line + old_line_count))
            spans.append((char_start, char_end))
        line_no = l2_hits[0] + 1
        return "normalized", spans, [new_string] * len(spans), ("L2 去行尾空白匹配", [line_no])

    # ── L3 逐行 strip 比对 + 缩进重对齐 ──
    old_stripped = [l.strip() for l in old_lines]
    file_stripped = [l.strip() for l in file_lines]
    l3_hits = []
    for i in range(file_line_count - old_line_count + 1):
        if file_stripped[i:i + old_line_count] == old_stripped:
            l3_hits.append(i)
    if l3_hits:
        if len(l3_hits) > 1 and not replace_all:
            line_nos = [i + 1 for i in l3_hits]
            return "multi", [], [], (f"L3 strip 匹配到 {len(l3_hits)} 处", line_nos)
        # 唯一或 replace_all → 做缩进重对齐
        spans = []
        new_texts = []
        for start_line in l3_hits:
            file_indent_unit = _detect_indent_unit(file_lines[start_line:start_line + old_line_count])
            model_indent_unit = _detect_indent_unit(old_lines)
            realigned = _realign_indent(new_string, file_indent_unit, model_indent_unit)
            char_start = sum(len(file_lines[j]) for j in range(start_line))
            char_end = sum(len(file_lines[j]) for j in range(start_line + old_line_count))
            spans.append((char_start, char_end))
            new_texts.append(realigned)
        line_no = l3_hits[0] + 1
        return "normalized", spans, new_texts, ("L3 strip+缩进重对齐匹配", [line_no])

    # ── L4 difflib 模糊滑窗（多档容差）──
    # 尝试 [len-2, len], [len-1, len+1], [len, len+2] 窗口大小
    best_hits = []  # (start_line, ratio, window_size)
    for delta in range(-2, 3):
        ws = old_line_count + delta
        if ws < 1 or ws > file_line_count:
            continue
        sm = difflib.SequenceMatcher()
        sm.set_seq2(old_stripped)
        for i in range(file_line_count - ws + 1):
            sm.set_seq1(file_stripped[i:i + ws])
            ratio = sm.ratio()
            if ratio >= 0.85:
                best_hits.append((i, ratio, ws))

    if best_hits:
        # 找最优
        max_ratio = max(r for _, r, _ in best_hits)
        # 次优低于最优 0.1 以上才算唯一
        sorted_ratios = sorted(set(r for _, r, _ in best_hits), reverse=True)
        second_best = sorted_ratios[1] if len(sorted_ratios) > 1 else 0
        unique = (max_ratio - second_best) >= 0.1

        if not unique and not replace_all:
            # 多个等价候选
            candidates = [(s, r, w) for s, r, w in best_hits if r >= max_ratio - 0.05]
            line_nos = [s + 1 for s, _, _ in candidates]
            return "multi", [], [], (f"L4 模糊匹配到 {len(candidates)} 个相似位置", line_nos)

        # 取最优的那些（ratio 最高的）
        top_hits = [(s, r, w) for s, r, w in best_hits if abs(r - max_ratio) < 0.001]
        if not replace_all and len(top_hits) > 1:
            line_nos = [s + 1 for s, _, _ in top_hits]
            return "multi", [], [], (f"L4 模糊匹配到 {len(top_hits)} 个相似位置", line_nos)

        # 缩进重对齐（使用模块级 _realign_indent）
        spans = []
        new_texts = []
        for start_line, ratio, window_size in top_hits:
            file_indent_unit = _detect_indent_unit(file_lines[start_line:start_line + window_size])
            model_indent_unit = _detect_indent_unit(old_lines)
            realigned = _realign_indent(new_string, file_indent_unit, model_indent_unit)
            char_start = sum(len(file_lines[j]) for j in range(start_line))
            char_end = sum(len(file_lines[j]) for j in range(start_line + window_size))
            spans.append((char_start, char_end))
            new_texts.append(realigned)
        line_no = top_hits[0][0] + 1
        return "fuzzy", spans, new_texts, (f"L4 模糊匹配 (ratio={max_ratio:.2f})", [line_no])

    # ── 全部失败 → 自纠反馈 ──
    # 用 difflib 找文件里与 old 最相似的片段
    best_i = 0
    best_ratio = 0.0
    if file_line_count >= old_line_count:
        sm = difflib.SequenceMatcher()
        sm.set_seq2(old_stripped)
        for i in range(file_line_count - old_line_count + 1):
            sm.set_seq1(file_stripped[i:i + old_line_count])
            r = sm.ratio()
            if r > best_ratio:
                best_ratio = r
                best_i = i
    else:
        # 文件比 old 还短，整体比较
        sm = difflib.SequenceMatcher(None, old_stripped, file_stripped)
        best_ratio = sm.ratio()

    # 取最接近片段上下文 ±2 行
    snippet_start = max(0, best_i - 2)
    snippet_end = min(file_line_count, best_i + old_line_count + 2)
    snippet_lines = []
    for idx in range(snippet_start, snippet_end):
        snippet_lines.append(f"  第 {idx + 1} 行: {file_lines[idx].rstrip()}")
    snippet_text = "\n".join(snippet_lines)
    desc = (
        f"失败：没找到匹配的 old_string。文件里最接近的是第 {best_i + 1}–{best_i + old_line_count} 行"
        f"（相似度 {best_ratio:.0%}）：\n{snippet_text}\n"
        "请直接复制上面的真实内容作为 old_string 重试（注意缩进与空行）。"
    )
    return "none", [], [], (desc, None)


@tool
def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """在文件中精确替换字符串（适合改大文件的局部，比 write_file 全量重写更安全更省 token）。

    - `old_string` 必须**完整**包含要被替换的那段（保留缩进、换行、标点）；
    - 默认要求 `old_string` 在文件中**只出现一次**——出现多次或没找到都会报错；
    - 想替换所有出现请显式传 `replace_all=True`；
    - 用这个工具比 write_file 安全：write_file 是全文覆盖容易丢内容，edit_file 只动指定那段。

    参数：
      path: 文件路径（绝对或相对项目根）
      old_string: 要被替换的旧文本（必须与文件中的原文一字不差，含空白）
      new_string: 替换成的新文本
      replace_all: True 时替换全部出现；False（默认）时要求只出现一次
    """
    if not old_string:
        return "失败：old_string 不能为空"
    if old_string == new_string:
        return "失败：old_string 和 new_string 相同，不需要替换"

    full = _resolve_path(path)
    if not os.path.exists(full):
        return f"失败：文件不存在 {full}"

    try:
        with open(full, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return f"读取失败: {e}"

    # ── 分层匹配级联 ──
    status, spans, new_texts, info = _locate_edit(content, old_string, new_string, replace_all)
    match_desc, line_nos = info

    # multi：多处候选，返回行号提示
    if status == "multi":
        lines_str = ", ".join(str(n) for n in line_nos)
        return (
            f"失败：{match_desc}（行 {lines_str}）。"
            "请提供更多上下文让它唯一，或显式传 replace_all=True 替换全部。"
        )

    # none：全部失败，返回自纠反馈
    if status == "none":
        return match_desc  # 已包含完整自纠文案

    # ── 成功命中：构建新内容 ──
    # spans 按位置排序（从后往前替换避免偏移）
    if spans:
        sorted_pairs = sorted(zip(spans, new_texts), key=lambda x: x[0][0], reverse=True)
        new_content = content
        for (start, end), replacement in sorted_pairs:
            new_content = new_content[:start] + replacement + new_content[end:]
    else:
        # fallback（不应发生）
        new_content = content.replace(old_string, new_string)

    # ── Diff 预览 + 用户确认（写盘前的最后一道关）──
    allowed, reject = _confirm_file_write(full, content, new_content)
    if not allowed:
        return reject

    # 写盘前打 checkpoint
    try:
        _checkpoint.make_checkpoint(_project_cwd(), "edit_file", full)
    except Exception as e:
        logger.warning(f"checkpoint 失败（不影响编辑）: {e}")

    try:
        with open(full, "w", encoding="utf-8") as f:
            f.write(new_content)
    except Exception as e:
        return f"写入失败: {e}"

    # 成功信息
    count = len(spans)
    primary_line = line_nos[0] if line_nos else "?"
    level_hint = f"（{match_desc}）" if "L1" not in match_desc else ""
    if count == 1:
        return f"成功编辑 {full}（第 {primary_line} 行附近替换 1 处）{level_hint}"
    else:
        return f"成功编辑 {full}（替换全部 {count} 处出现，第一处在第 {primary_line} 行）{level_hint}"


@tool
def list_directory(path: str = ".") -> str:
    """列出目录下的文件和文件夹。path: 目录路径，默认当前项目根（无项目时为进程目录）"""
    try:
        full = _resolve_path(path)
        items = os.listdir(full)
        result = []
        for item in sorted(items):
            full_item = os.path.join(full, item)
            if os.path.isdir(full_item):
                result.append(f"📁 {item}/")
            else:
                size = os.path.getsize(full_item)
                if size < 1024:
                    s = f"{size}B"
                elif size < 1024 * 1024:
                    s = f"{size/1024:.1f}KB"
                else:
                    s = f"{size/1024/1024:.1f}MB"
                result.append(f"📄 {item}  ({s})")
        header = f"[目录: {full}]\n"
        return header + ("\n".join(result) if result else "空目录")
    except Exception as e:
        return f"列目录失败: {e}"


BLOCKED_COMMANDS = [
    "more", "pause", "edit", "choice", "set /p",
    "cmd /k", "powershell -noexit", "python -i",
    "nslookup", "ftp", "telnet", "ssh", "diskpart",
]


def _kill_proc_tree(proc):
    """跨平台杀整个进程树。

    Windows: `shell=True` 的 Popen 启动的是 cmd.exe，cmd 又 spawn 真正的命令进程。
    `proc.kill()` 只杀 cmd，子进程会继续跑（"中断不掉"的根因）。这里用
    `taskkill /F /T /PID` 把进程树整个连根拔。
    Unix: 进程组可以一起杀，但 shell=True 也有类似问题，这里 fallback 到 proc.kill()。
    """
    if proc is None or proc.poll() is not None:
        return
    import sys as _sys
    if _sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
            return
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def _decode_chunk(b: bytes) -> str:
    """命令输出按 utf-8 → gbk 顺序兜底解码。Windows 中文环境下 npm/pip/git 走 UTF-8、
    cmd 内置走 GBK，混着来很常见。"""
    if not b:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")


@tool
def run_command(command: str) -> str:
    """执行系统命令并**流式**返回输出（边跑边显示，不必等命令结束）。

    命令耗时 > 几秒时（pytest / npm test / build / 长 curl 等），UI 上能实时
    看到 stdout/stderr 进度；AI 拿到的工具结果仍是完整输出（超过 5000 字会截断）。
    超过 30 秒强杀；执行前会弹用户确认卡片；危险命令需用户允许。
    """
    import threading as _thr_local

    cmd_lower = command.lower().strip()
    for blocked in BLOCKED_COMMANDS:
        parts = [p.strip() for p in cmd_lower.replace("&&", "|").split("|")]
        for part in parts:
            if part == blocked or part.startswith(blocked + " "):
                return f"拒绝执行: '{blocked}' 是交互式命令，会导致程序挂起"

    # ── 用户确认（同原逻辑）──
    ui = getattr(state, "ui_ref", None)
    if ui is not None:
        try:
            allowed = ui.confirm_command(command)
        except Exception as e:
            logger.warning(f"确认对话框异常，默认拒绝执行: {e}")
            return f"用户确认对话框出错，已拒绝执行: {e}"
        if not allowed:
            logger.info(f"用户拒绝执行命令: {command}")
            return "已拒绝：用户不允许执行此命令。"

    run_cwd = _project_cwd()

    # stderr 合并进 stdout 走同一管道，按时间顺序输出（不再分开拼接）
    try:
        proc = subprocess.Popen(
            command, shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=run_cwd,
            bufsize=0,
        )
    except Exception as e:
        return f"启动失败: {e}"

    output_chunks: list[str] = []
    chunks_lock = _thr_local.Lock()
    reader_done = _thr_local.Event()

    def _reader():
        """子线程：从 proc.stdout 读字节，按 utf-8/gbk 解码，行边界 push 到 UI。"""
        try:
            buf = b""
            while True:
                raw = proc.stdout.read(4096)
                if not raw:
                    if buf:
                        text = _decode_chunk(buf)
                        with chunks_lock:
                            output_chunks.append(text)
                        if ui is not None:
                            try:
                                ui.show_message(text, "tool_result")
                            except Exception:
                                pass
                    break
                buf += raw
                # 按 \n 切分，把已经完整的行 flush 出去，剩余半行留在 buf 等下次
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = _decode_chunk(line + b"\n")
                    with chunks_lock:
                        output_chunks.append(text)
                    if ui is not None:
                        try:
                            ui.show_message(text, "tool_result")
                        except Exception:
                            pass
        finally:
            reader_done.set()

    rt = _thr_local.Thread(target=_reader, daemon=True)
    rt.start()

    # 主循环：等进程结束 / 监控 stop_flag / 监控超时
    start = time.time()
    timed_out = False
    interrupted = False
    while True:
        if proc.poll() is not None:
            break
        elapsed = time.time() - start
        if elapsed > RUN_COMMAND_TIMEOUT_S:
            timed_out = True
            _kill_proc_tree(proc)
            break
        if getattr(state, "stop_flag", False):
            interrupted = True
            _kill_proc_tree(proc)
            break
        time.sleep(0.05)

    # 让 reader 把剩余 buf flush 完
    reader_done.wait(timeout=2)
    try:
        proc.stdout.close()
    except Exception:
        pass

    if timed_out:
        if ui is not None:
            try:
                ui.show_message(f"\n⏱️ 超时强杀（{RUN_COMMAND_TIMEOUT_S}s）\n", "tool_result")
            except Exception:
                pass
        return f"命令执行超时（{RUN_COMMAND_TIMEOUT_S} 秒），已强杀进程"
    if interrupted:
        if ui is not None:
            try:
                ui.show_message("\n⏹ 用户中断\n", "tool_result")
            except Exception:
                pass
        return "用户中断执行"

    with chunks_lock:
        output = "".join(output_chunks)

    if not output:
        output = "(无输出)"
    if len(output) > RUN_COMMAND_MAX_OUTPUT_CHARS:
        output = (
            output[:RUN_COMMAND_MAX_OUTPUT_CHARS]
            + f"\n... [输出过长，已截断；UI 上能看到全量约 {len(output)} 字符]"
        )

    # 完成标记一行（让 UI 上能看到"结束了"，不会和上一段输出粘在一起）
    if ui is not None:
        try:
            ui.show_message(f"\n✓ 退出码 {proc.returncode}\n", "tool_result")
        except Exception:
            pass

    return f"退出码: {proc.returncode}\n{output}"


@tool
def search_in_file(path: str, keyword: str, offset: int = 0, limit: int = SEARCH_IN_FILE_DEFAULT_LIMIT) -> str:
    """在单个文件中搜索关键词，返回匹配的行。
    path: 文件路径, keyword: 搜索关键词, offset: 从第几处匹配开始显示（0-based）, limit: 本次最多显示多少处。
    跨文件 / 跨目录搜索请用 `search_files`。"""
    try:
        offset = max(0, int(offset or 0))
        limit = max(1, min(SEARCH_IN_FILE_MAX_LIMIT, int(limit or SEARCH_IN_FILE_DEFAULT_LIMIT)))
        with open(_resolve_path(path), "r", encoding="utf-8") as f:
            lines = f.readlines()
        matches = []
        for i, line in enumerate(lines, 1):
            if keyword.lower() in line.lower():
                matches.append(f"  L{i}: {line.rstrip()}")
        if matches:
            shown = matches[offset:offset + limit]
            if not shown:
                return (
                    f"找到 {len(matches)} 处匹配，但 offset={offset} 已超出范围。"
                    f" 请使用 0 到 {max(0, len(matches) - 1)} 之间的 offset。"
                )
            next_offset = offset + len(shown)
            remaining = max(0, len(matches) - next_offset)
            tail = (
                f"\n... [本次显示 {offset + 1}-{next_offset} / {len(matches)} 处，"
                f"还有 {remaining} 处未列出；继续查看用 offset={next_offset}]"
                if remaining > 0 else
                f"\n[已显示 {offset + 1}-{next_offset} / {len(matches)} 处匹配]"
            )
            return f"找到 {len(matches)} 处匹配:\n" + "\n".join(shown) + tail
        return f"未找到 '{keyword}'"
    except Exception as e:
        return f"搜索失败: {e}"


# search_files 默认跳过的目录（编译产物、依赖、版本控制等噪声）
_SEARCH_IGNORE_DIRS = {
    ".git", ".hg", ".svn",
    "node_modules", "bower_components",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", "env", ".env",
    "build", "dist", "target", "out",
    ".next", ".nuxt", ".idea", ".vscode",
}
_SEARCH_MAX_FILE_SIZE = 1 * 1024 * 1024  # 单文件 > 1MB 跳过（大概率是二进制 / 数据文件）


@tool
def search_files(regex: str, path: str = ".", file_pattern: str = "*", max_results: int = SEARCH_FILES_MAX_RESULTS) -> str:
    """跨文件 / 跨目录用**正则**搜索（ripgrep 风格），返回 `相对路径:行号:内容`。

    用法：
      - `regex`：Python 正则，例如 `def\\s+\\w+\\(` / `TODO|FIXME` / `class \\w+\\(BaseModel\\)`
      - `path`：搜索目录的相对/绝对路径（默认 `.` 即项目根）
      - `file_pattern`：glob 过滤文件名，例如 `*.py` / `*.{ts,tsx}` / `test_*.py`
      - `max_results`：截断阈值（默认 50，超过会提示 "还有 N 处未列出"）

    默认忽略噪声目录：.git / node_modules / __pycache__ / .venv / venv / build / dist 等；
    单文件 > 1MB 自动跳过（避免读到二进制大文件）。
    """
    import re as _re
    import fnmatch

    if not regex:
        return "失败：regex 不能为空"
    try:
        pat = _re.compile(regex)
    except _re.error as e:
        return f"失败：正则不合法 — {e}"

    full = _resolve_path(path) if path else _project_cwd()
    if not os.path.isdir(full):
        return f"失败：目录不存在 {full}"

    # 支持 `*.{ts,tsx}` 这种 brace expansion
    def _expand_braces(pattern):
        m = _re.match(r"^(.*)\{([^{}]+)\}(.*)$", pattern)
        if not m:
            return [pattern]
        prefix, choices, suffix = m.group(1), m.group(2).split(","), m.group(3)
        return [f"{prefix}{c.strip()}{suffix}" for c in choices]

    patterns = _expand_braces(file_pattern)

    matches = []
    total = 0
    truncated = False

    for root, dirs, files in os.walk(full):
        # 原地修改 dirs 跳过忽略目录
        dirs[:] = [d for d in dirs if d not in _SEARCH_IGNORE_DIRS and not d.startswith(".")]
        for fname in files:
            if not any(fnmatch.fnmatch(fname, p) for p in patterns):
                continue
            fpath = os.path.join(root, fname)
            try:
                if os.path.getsize(fpath) > _SEARCH_MAX_FILE_SIZE:
                    continue
            except OSError:
                continue
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    for ln, line in enumerate(f, 1):
                        if pat.search(line):
                            total += 1
                            if len(matches) < max_results:
                                rel = os.path.relpath(fpath, full).replace(os.sep, "/")
                                matches.append(f"{rel}:{ln}:{line.rstrip()}")
                            else:
                                truncated = True
            except (OSError, UnicodeDecodeError):
                continue

    if not matches:
        return f"未在 {full} 找到匹配 /{regex}/ 的内容（file_pattern={file_pattern}）"

    header = f"在 {full} 下找到 {total} 处匹配 /{regex}/（file_pattern={file_pattern}）:\n"
    body = "\n".join(matches)
    if truncated:
        body += f"\n... [仅显示前 {max_results} 处，还有 {total - max_results} 处未列出，请缩小 path 或 file_pattern]"
    return header + body


@tool
def generate_image(prompt: str, width: int = 1024, height: int = 1024, model: str = "flux") -> str:
    """根据文字描述生成一张图片并保存到本地。
    优先调用本机 ComfyUI（http://127.0.0.1:8188，可在 config.json 改），未启动时自动回退到 Pollinations.ai 免费服务。
    prompt: 详细的图片描述（英文效果通常更好），例如 "1girl, cute anime maid, twin ponytails, black hair, lace headdress, black and white maid uniform, masterpiece, best quality"
    width: 宽度像素，默认 1024（正方形）。**大多数场景留默认即可**。竖图建议 832（2:3 比例），横图 1216
    height: 高度像素，默认 1024。竖图建议 1216，横图 832
    model: 仅在回退 Pollinations 时使用：'flux'（默认）、'flux-realism'（写实风）、'turbo'（速度快质量低）"""
    from datetime import datetime

    # 优先存到当前项目根的 outputs/，没项目就存到 chat_memory/generated/，
    # 避免硬编码到某个人的本地目录（之前是 D:\games\servicedaily\photos）
    proj = getattr(state, "current_project", None)
    if proj and os.path.isdir(proj):
        save_dir = os.path.join(proj, "outputs")
    else:
        save_dir = os.path.join(_app_data_dir(), "chat_memory", "generated")
    try:
        os.makedirs(save_dir, exist_ok=True)
    except Exception as e:
        return f"创建保存目录失败: {e}"

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(save_dir, f"gen_{ts}.png")

    # ── 优先：本机 ComfyUI ──
    cfg = _load_comfy_config()
    if cfg["base_url"] and _comfy_available(cfg["base_url"]):
        try:
            data = _call_comfy(prompt, width, height)
            if data and len(data) >= 1000:
                with open(filepath, "wb") as f:
                    f.write(data)
                return (
                    f"已生成图片 (本机 ComfyUI): {filepath} (尺寸 {width}x{height}). "
                    f"图片已成功显示给用户，**不要再次调用本工具**，直接用一句话回应用户即可。"
                )
        except Exception as e:
            # ComfyUI 出问题就回退，不直接报错
            pass

    # ── 回退：Pollinations.ai ──
    try:
        encoded = urllib.parse.quote(prompt)
        url = (
            f"https://image.pollinations.ai/prompt/{encoded}"
            f"?width={width}&height={height}"
            f"&model={model}&enhance=true&nologo=true&safe=false"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = None
        last_err = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    data = resp.read()
                if len(data) >= 1000:
                    break
            except Exception as e:
                last_err = e
                if attempt < 2:
                    time.sleep(2)
        if data is None or len(data) < 1000:
            err_info = f": {last_err}" if last_err else f"，仅 {len(data) if data else 0} 字节"
            return f"生成失败（本机 ComfyUI 未启动 & Pollinations 服务异常）{err_info}，请稍后重试或启动 ComfyUI"
        with open(filepath, "wb") as f:
            f.write(data)
        return (
            f"已生成图片 (Pollinations 回退): {filepath} (尺寸 {width}x{height}, 模型 {model}). "
            f"图片已成功显示给用户，**不要再次调用本工具**，直接用一句话回应用户即可。"
        )
    except Exception as e:
        return f"生成失败: {e}"


# ══════════════════════════════════════
# 长期记忆工具
# ══════════════════════════════════════


@tool
def remember(fact: str) -> str:
    """存一条关于用户的长期记忆。

    当用户透露了值得长期记住的个人信息、偏好、项目约定时调用。
    fact 应该是简洁的一句话陈述，不要太长。
    示例：用户说"我习惯用 pytest 测试"，存为 "用户用 pytest 做测试"
    """
    from .memory_store import add_memory
    result = add_memory(fact)
    if result:
        return f"已记住: {result['text']}"
    return "该记忆已存在，无需重复保存"


@tool
def forget(query: str) -> str:
    """按关键词删除长期记忆。

    列出匹配项并删除，返回删除了哪些记忆。
    示例：forget("pytest") 会删除包含 "pytest" 的记忆
    """
    from .memory_store import search_memories, delete_memory
    matches = search_memories(query)
    if not matches:
        return f"未找到包含 '{query}' 的记忆"
    
    deleted = []
    for mem in matches:
        if delete_memory(mem["id"]):
            deleted.append(mem["text"])
    
    if deleted:
        return f"已删除 {len(deleted)} 条记忆:\n" + "\n".join(f"- {t}" for t in deleted)
    return "删除失败，请稍后重试"


# 导出
ALL_TOOLS = [
    read_file, write_file, append_file, edit_file,
    list_directory, run_command,
    search_in_file, search_files,
    generate_image,
    remember, forget,
]


def get_mcp_tools() -> list:
    """延迟导入 MCP 工具列表（mcp_client.init_mcp 后才填充）。"""
    try:
        from .mcp_client import MCP_TOOLS
        return list(MCP_TOOLS)
    except Exception:
        return []


def build_all_tools() -> list:
    """返回内置工具 + 远程 MCP 工具的完整列表。"""
    return ALL_TOOLS + get_mcp_tools()


def get_tool_map() -> dict:
    """返回内置工具 + MCP 工具的 name→tool 映射（动态，每次调用重新计算）。"""
    tool_map = {t.name: t for t in ALL_TOOLS}
    for t in get_mcp_tools():
        tool_map[t.name] = t
    # 合并 MCP display names 到 TOOL_DISPLAY_NAMES（运行时注入）
    try:
        from .mcp_client import MCP_DISPLAY_NAMES
        TOOL_DISPLAY_NAMES.update(MCP_DISPLAY_NAMES)
    except Exception:
        pass
    return tool_map


TOOL_DISPLAY_NAMES = {
    "read_file": "📖 读取文件",
    "write_file": "✏️ 写入文件",
    "append_file": "📝 追加文件",
    "edit_file": "🪄 精确编辑",
    "list_directory": "📂 列出目录",
    "run_command": "⚡ 执行命令",
    "search_in_file": "🔍 单文件搜索",
    "search_files": "🌐 跨文件搜索",
    "generate_image": "🎨 生成图片",
    "remember": "🧠 记住事实",
    "forget": "🗑️ 遗忘记忆",
}


TOOL_MAP = get_tool_map()
