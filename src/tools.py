import os
import sys
import json
import time
import re
import difflib
import shutil
import subprocess
import threading
import urllib.parse
import urllib.request
import urllib.error
from collections import deque
from langchain_core.tools import tool

from . import state
from . import session as _session
from . import checkpoint as _checkpoint
from .verification import mark_dirty as _v_mark_dirty, mark_check as _v_mark_check, mark_tests as _v_mark_tests, mark_diff_reviewed as _v_mark_diff_reviewed
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

    优先用【当前会话】锚定的 project：多会话下 worker 跑工具时用它自己会话的项目根，
    不会因为用户在前台切了项目就把 A 会话的 read_file/run_command 落到 B。会话还没锚定
    （_UNSET，如刚新建没存盘）→ 回退全局 state.current_project；都没有 / 路径不存在 →
    进程 cwd。None 是合法的"无项目（全局）"。
    """
    from . import session as _session
    proj = _session.current_project()   # 会话级：_UNSET 回退全局，统一来源
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


def _norm_vpath(path: str) -> str:
    """规范化路径用于验证状态追踪：转成相对项目根的正斜杠路径。"""
    abs_path = _resolve_path(path)
    cwd = _project_cwd()
    abs_real = os.path.realpath(abs_path)
    cwd_real = os.path.realpath(cwd) if cwd else ""
    try:
        inside = cwd_real and os.path.normcase(os.path.commonpath([abs_real, cwd_real])) == os.path.normcase(cwd_real)
    except ValueError:
        inside = False
    if inside:
        rel = os.path.relpath(abs_real, cwd_real)
        return rel.replace("\\", "/")
    return os.path.normpath(abs_path).replace("\\", "/")


def _mark_current_dirty(full_path: str) -> None:
    try:
        _v_mark_dirty(_session.get_verification(), _norm_vpath(full_path))
    except Exception as e:
        logger.debug(f"验证状态标记 dirty 失败: {e}")


def _mark_current_check(full_path: str, passed, checker: str = "") -> None:
    try:
        _v_mark_check(_session.get_verification(), _norm_vpath(full_path), passed, checker or "")
    except Exception as e:
        logger.debug(f"验证状态标记 check 失败: {e}")


def _shell_cwd() -> str:
    """run_command 实际用的 cwd：shell_cwd（存在且是目录）否则退回项目根。"""
    base = getattr(state, "shell_cwd", None)
    if base and os.path.isdir(base):
        return base
    return _project_cwd()


def _parse_cd(command: str):
    """纯 cd 命令 → 返回目标【绝对路径】；非纯 cd（复合/重定向/非 cd）→ None。"""
    import re as _re
    s = command.strip()
    # 含 && || | ; 换行 > < 的复合/重定向命令不算"纯 cd"
    if any(op in s for op in ("&&", "||", "|", ";", "\n", ">", "<")):
        return None
    # "cd" / "cd X"；"cdrom" 不匹配（要求 cd 后面要么结尾要么空白）
    m = _re.match(r'^cd(?:\s+(.+))?$', s, _re.IGNORECASE)
    if not m:
        return None
    arg = (m.group(1) or "").strip().strip('"').strip("'")
    if not arg or arg == "~":
        return _project_cwd()                               # cd / cd ~ → 回项目根
    if arg.startswith("~") and (len(arg) == 1 or arg[1] in ("/", "\\")):
        # ~/sub 或 ~\sub → 项目根/sub
        arg = arg[2:].lstrip("/\\") or "."
        target = os.path.join(_project_cwd(), arg)
    else:
        target = arg if os.path.isabs(arg) else os.path.join(_shell_cwd(), arg)
    return os.path.normpath(target)


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
        allowed, user_feedback = ui.confirm_edit(full, diff_text)
    except Exception as e:
        logger.warning(f"文件写入确认对话框异常，默认拒绝: {e}")
        return False, f"用户确认对话框出错，已拒绝写入: {e}"
    if not allowed:
        _msg = "已拒绝：用户不允许此次写入。"
        if user_feedback:
            _msg += f"\n用户补充说明：{user_feedback}"
        logger.info(f"用户拒绝写入 {full}")
        return False, _msg
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
        _mark_current_dirty(full)
        return f"成功写入文件: {full}" + _auto_check_suffix(full)
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
        _mark_current_dirty(full)
        return f"成功追加到文件: {full}" + _auto_check_suffix(full)
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

    _mark_current_dirty(full)

    # 成功信息
    count = len(spans)
    primary_line = line_nos[0] if line_nos else "?"
    level_hint = f"（{match_desc}）" if "L1" not in match_desc else ""
    suffix = _auto_check_suffix(full)
    if count == 1:
        return f"成功编辑 {full}（第 {primary_line} 行附近替换 1 处）{level_hint}" + suffix
    else:
        return f"成功编辑 {full}（替换全部 {count} 处出现，第一处在第 {primary_line} 行）{level_hint}" + suffix


@tool
def list_directory(path: str = ".") -> str:
    """列出目录下的文件和文件夹。path: 目录路径，默认当前项目根（无项目时为进程目录）"""
    try:
        full = _resolve_path(path)
        items = os.listdir(full)
        dirs, files = [], []
        for item in sorted(items):
            full_item = os.path.join(full, item)
            if os.path.isdir(full_item):
                dirs.append(f"📁 {item}/")
            else:
                size = os.path.getsize(full_item)
                if size < 1024:
                    s = f"{size}B"
                elif size < 1024 * 1024:
                    s = f"{size/1024:.1f}KB"
                else:
                    s = f"{size/1024/1024:.1f}MB"
                files.append(f"📄 {item}  ({s})")
        result = dirs + files
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


# ══════════════════════════════════════
# 后台进程注册表
# ══════════════════════════════════════
# bg_id → {proc, command, output: deque(maxlen=2000), start_ts}
_bg_procs: dict[str, dict] = {}
_bg_lock = threading.Lock()
_bg_counter = [0]


def _new_bg_id() -> str:
    with _bg_lock:
        _bg_counter[0] += 1
        return f"bg{_bg_counter[0]}"


@tool
def run_command(command: str, timeout: int | None = None, background: bool = False) -> str:
    """执行系统命令并**流式**返回输出（边跑边显示，不必等命令结束）。

    命令耗时 > 几秒时（pytest / npm test / build / 长 curl 等），UI 上能实时
    看到 stdout/stderr 进度；AI 拿到的工具结果仍是完整输出（超过 5000 字会截断）。
    默认 5 分钟超时；传 timeout 参数（秒）可覆盖（如跑大测试套件传 600）。
    随时可点停止按钮中断；执行前会弹用户确认卡片；危险命令需用户允许。

    background=True 时命令在后台运行（适用于 dev server / watch / 长服务），
    立即返回 bg_id；用 read_background_output 看输出，stop_background_command 停止。
    """
    import threading as _thr_local

    effective_timeout = timeout if timeout is not None else RUN_COMMAND_TIMEOUT_S

    cmd_lower = command.lower().strip()
    for blocked in BLOCKED_COMMANDS:
        parts = [p.strip() for p in cmd_lower.replace("&&", "|").split("|")]
        for part in parts:
            if part == blocked or part.startswith(blocked + " "):
                    return f"拒绝执行: '{blocked}' 是交互式命令，会导致程序挂起"

    # ── 纯 cd 拦截：只切目录、不弹确认、不起进程 ──
    cd_target = _parse_cd(command)
    if cd_target is not None:
        if os.path.isdir(cd_target):
            state.shell_cwd = cd_target
            return f"已切换工作目录到: {cd_target}"
        return f"目录不存在: {cd_target}"

    # ── 用户确认（同原逻辑）──
    ui = getattr(state, "ui_ref", None)
    if ui is not None:
        try:
            allowed, user_feedback = ui.confirm_command(command)
        except Exception as e:
            logger.warning(f"确认对话框异常，默认拒绝执行: {e}")
            return f"用户确认对话框出错，已拒绝执行: {e}"
        if not allowed:
            _msg = "已拒绝：用户不允许执行此命令。"
            if user_feedback:
                _msg += f"\n用户补充说明：{user_feedback}"
            logger.info(f"用户拒绝执行命令: {command}")
            return _msg

    run_cwd = _shell_cwd()

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

    # ── 后台模式：起 reader 线程写 deque，立即返回 ──
    if background:
        bg_id = _new_bg_id()
        out_deque: deque[str] = deque(maxlen=2000)
        start_ts = time.time()

        def _bg_reader():
            """后台 reader：把输出 append 进 deque，不刷 UI。"""
            try:
                buf = b""
                while True:
                    raw = proc.stdout.read(4096)
                    if not raw:
                        if buf:
                            text = _decode_chunk(buf)
                            with _bg_lock:
                                out_deque.append(text)
                        break
                    buf += raw
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        text = _decode_chunk(line + b"\n")
                        with _bg_lock:
                            out_deque.append(text)
            except Exception:
                pass  # 进程被杀时 stdout 关闭会抛异常，忽略

        with _bg_lock:
            _bg_procs[bg_id] = {
                "proc": proc,
                "command": command,
                "output": out_deque,
                "start_ts": start_ts,
            }
        bg_thread = threading.Thread(target=_bg_reader, daemon=True)
        bg_thread.start()
        logger.info(f"后台命令已启动 [{bg_id}]: {command}")
        return (
            f"已后台启动 [{bg_id}]: {command}\n"
            f"用 read_background_output('{bg_id}') 看输出，"
            f"stop_background_command('{bg_id}') 停止。"
        )

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
        if elapsed > effective_timeout:
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
                ui.show_message(f"\n⏱️ 超时强杀（{effective_timeout}s）\n", "tool_result")
            except Exception:
                pass
        return f"命令执行超时（{effective_timeout} 秒），已强杀进程"
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
        full = _resolve_path(path)
        # search_in_file 是单文件搜索；传目录会让 open() 在 Windows 上报 Errno 13
        # Permission denied，给个清晰提示引导用 search_files，而不是抛系统错。
        if os.path.isdir(full):
            return f"`{path}` 是目录、不是单个文件。跨目录搜索请用 search_files（正则、自动遍历目录、忽略噪声目录）。"
        if not os.path.isfile(full):
            return f"文件不存在: {path}"
        with open(full, "r", encoding="utf-8") as f:
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
    # 用会话级 project（_project_cwd 同源）：后台会话生图存到自己项目，不跟前台切项目走
    from . import session as _session
    proj = _session.current_project()
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
        except Exception:
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


@tool
def get_project_instructions(path: str = ".") -> str:
    """读取目标路径适用的 AI 编码规则（CLAUDE.md / AGENTS.md / .lingxirules）。

    path 可指向当前项目内的文件或目录；返回项目根到目标目录的完整规则链。
    不传参数时只读取当前项目根规则。
    """
    from .roles import load_project_rules
    root = _project_cwd()
    target = _resolve_path(path or ".")
    result = load_project_rules(root, target)
    if not result:
        return (
            f"路径 `{path or '.'}` 没有适用的项目规则，"
            "或该路径不在当前项目根目录内。"
        )
    return result


@tool
def notify_user(title: str, message: str, level: str = "info") -> str:
    """主动向用户手机推送 Telegram 通知。

    用于 AI 判断需要提醒用户的场景（如长时间任务的关键节点、需要用户注意的事项）。
    level: info / done / action_needed / error（默认 info）
    """
    from .notify import notify
    ok = notify(level, title, message, f"ai_notify_{level}")
    if ok:
        return f"已推送 Telegram 通知: {title}"
    return "通知未发送（Telegram 未配置或被节流）"


@tool
def update_plan(plan: str) -> str:
    """创建或更新当前任务的执行计划（待办清单）。

    **何时用**：任务需要 3 步以上、或要改多个文件时，动手前先调一次列出全部步骤；
    之后每开始/完成一步就再调一次更新状态，直到所有步骤都 [x]。

    plan: 多行文本，每行一个步骤，行首用状态标记：
      [ ] 未开始    [~] 进行中（同一时间只标一个）    [x] 已完成
    每次传**完整的当前计划**（全量覆盖，不是增量追加）。

    示例：
      update_plan("[x] 读 config.py 看结构\n[~] 在 state.py 加状态\n[ ] 加 update_plan 工具")
    """
    from . import state
    # 历史压缩摘要曾被模型续接进 plan 参数；摘要不是计划的一部分，硬截断防止
    # 它被宽松解析后撑成几十条假步骤。
    clean_plan = re.split(
        r"\s*\[(?:历史摘要|之前已有摘要|新增对话)\]\s*:?",
        plan or "",
        maxsplit=1,
    )[0]
    items = state.parse_plan(clean_plan)
    if plan and not items:
        return "计划未更新：没有检测到合法 checklist 行，请使用 [ ] / [~] / [x] 标记。"
    guard = _validate_plan_update(getattr(state, "current_plan", None) or [], items, clean_plan)
    if guard:
        return guard
    state.current_plan = items
    # 预留 UI 钩子：当前阶段 UI 没实现 show_plan，hasattr 判空安全跳过
    _ui = getattr(state, "ui_ref", None)
    if _ui is not None and hasattr(_ui, "show_plan"):
        try:
            _ui.show_plan(list(items))
        except Exception:
            pass
    if not items:
        return "计划已清空。"
    done = sum(1 for it in items if it["status"] == "done")
    return f"计划已更新（{done}/{len(items)} 完成）：\n" + state.render_plan(items)


_PLAN_CHANGE_REASON_RE = re.compile(
    r"(调整说明|变更说明|合并说明|删除说明|结构调整|计划调整|合并原因|删除原因)"
)
_PLAN_VALIDATION_RE = re.compile(
    r"(测试|全量|pytest|run_tests|git\s*diff|diff|验收|复核|检查改动)",
    re.IGNORECASE,
)


def _plan_text_key(text: str) -> str:
    """计划项稳定匹配 key：忽略空白和常见编号差异。"""
    s = (text or "").strip()
    s = re.sub(r"^\d+[.)、]\s*", "", s)
    s = re.sub(r"\s+", "", s)
    return s.lower()


def _validate_plan_update(old_items: list, new_items: list, raw_plan: str) -> str:
    """防止模型执行中静默删除未完成计划项。

    update_plan 是全量覆盖工具；没有这层保护时，模型会把 9 项计划随手改成 8 项，
    让测试 / diff 等验收步骤悄悄消失。允许新增和状态更新；删除未完成项需有明确
    调整说明，未完成的验收项始终不允许删除。
    """
    if not old_items or not new_items:
        return ""

    new_keys = {_plan_text_key(it.get("text", "")) for it in new_items}
    missing = []
    missing_validation = []
    for item in old_items:
        text = item.get("text", "")
        if item.get("status") == "done":
            continue
        if _plan_text_key(text) not in new_keys:
            missing.append(text)
            if _PLAN_VALIDATION_RE.search(text or ""):
                missing_validation.append(text)

    if not missing:
        return ""

    if missing_validation:
        return (
            "计划未更新：不能删除尚未完成的验收步骤。\n"
            "被删除的验收项：\n"
            + "\n".join(f"- {x}" for x in missing_validation)
            + "\n\n请保留测试 / 全量测试 / git diff 等验收步骤，只更新状态。"
        )

    if not _PLAN_CHANGE_REASON_RE.search(raw_plan or ""):
        return (
            "计划未更新：新计划删除了尚未完成的步骤，但没有说明是合并、拆分还是取消。\n"
            "被删除的未完成项：\n"
            + "\n".join(f"- {x}" for x in missing)
            + "\n\n默认请只更新 [ ]/[~]/[x] 状态；如确需调整结构，请在计划文本中加入“调整说明：...”。"
        )

    return ""


# ══════════════════════════════════════
# 后台命令管理工具
# ══════════════════════════════════════


@tool
def read_background_output(bg_id: str, tail: int = 50) -> str:
    """读后台命令的累积输出（最后 tail 行）。tail<=0 看全部缓冲。"""
    with _bg_lock:
        info = _bg_procs.get(bg_id)
    if info is None:
        return f"未找到后台命令 '{bg_id}'。可用 list_background_commands() 查看所有。"

    proc = info["proc"]
    with _bg_lock:
        lines = list(info["output"])
    total = len(lines)  # 锁内快照长度；下面不再锁外迭代 deque（reader 并发 append 会 RuntimeError）

    status = "运行中" if proc.poll() is None else f"已退出(码 {proc.returncode})"
    elapsed = int(time.time() - info["start_ts"])

    if tail > 0 and total > tail:
        lines = lines[-tail:]
        truncated_hint = f"\n... (仅显示最后 {tail} 行，共缓冲 {total} 段)"
    else:
        truncated_hint = ""

    return (
        f"[{bg_id}] {status} | {elapsed}s | {info['command']}\n"
        + "".join(lines) + truncated_hint
    )


@tool
def list_background_commands() -> str:
    """列出所有后台命令：bg_id / 命令 / 运行中或已退出 / 启动多久。"""
    with _bg_lock:
        if not _bg_procs:
            return "没有后台命令在运行。"
        rows = []
        for bg_id, info in _bg_procs.items():
            proc = info["proc"]
            status = "运行中" if proc.poll() is None else f"已退出(码 {proc.returncode})"
            elapsed = int(time.time() - info["start_ts"])
            rows.append(f"  [{bg_id}] {status} | {elapsed}s | {info['command']}")
    return "后台命令列表:\n" + "\n".join(rows)


@tool
def stop_background_command(bg_id: str) -> str:
    """停止一个后台命令（taskkill 杀进程树），并从注册表移除。"""
    with _bg_lock:
        info = _bg_procs.pop(bg_id, None)
    if info is None:
        return f"未找到后台命令 '{bg_id}'。"

    proc = info["proc"]
    _kill_proc_tree(proc)
    elapsed = int(time.time() - info["start_ts"])
    logger.info(f"已停止后台命令 [{bg_id}]: {info['command']}（运行 {elapsed}s）")
    return f"已停止 [{bg_id}]: {info['command']}（运行 {elapsed}s）"


def stop_all_background():
    """停止所有后台命令（应用退出时调用）。"""
    with _bg_lock:
        procs = list(_bg_procs.items())
        _bg_procs.clear()
    for bg_id, info in procs:
        try:
            _kill_proc_tree(info["proc"])
            logger.info(f"退出清理：停止 [{bg_id}] {info['command']}")
        except Exception:
            pass


def _locate_symbol(src: str, name: str):
    """在源码里找符号 name 第一次以独立标识符出现的 (line 1-based, col 0-based)；没有→(None, None)。"""
    import re
    m = re.search(rf"\b{re.escape(name)}\b", src)
    if not m:
        return None, None
    line = src.count("\n", 0, m.start()) + 1
    col = m.start() - (src.rfind("\n", 0, m.start()) + 1)
    return line, col


def _locate_all_symbols(src: str, name: str):
    """源码里 name 作为独立标识符出现的所有 (line 1-based, col 0-based)，按出现顺序。
    用于在指定文件内逐个位置尝试解析——首处可能落在注释/字符串里 jedi 解析不到。"""
    import re
    out = []
    for m in re.finditer(rf"\b{re.escape(name)}\b", src):
        line = src.count("\n", 0, m.start()) + 1
        col = m.start() - (src.rfind("\n", 0, m.start()) + 1)
        out.append((line, col))
    return out


def _fmt_jedi_name(n, root):
    """把 jedi Name 格式化成一行：相对路径:行  [类型]  描述。"""
    mp = str(n.module_path) if n.module_path else "?"
    try:
        mp = os.path.relpath(mp, root)
    except Exception:
        pass
    return f"  {mp}:{n.line}  [{n.type}]  {(n.description or '').strip()[:80]}"


@tool
def find_definition(name: str, path: str = "") -> str:
    """跳到符号（函数/类/变量）的定义位置。比 search_files 正则准——jedi 懂作用域/import/继承。
    name: 符号名，如 "agent_loop" / "Session"。
    path: 指定某文件（相对项目根）里该符号的引用点去跟踪定义；留空则在整个项目搜该符号的定义。
    """
    try:
        import jedi
    except ImportError:
        return "未安装 jedi（pip install jedi 启用精准代码导航）；可改用 search_files 正则搜定义。"
    root = _project_cwd()
    proj = jedi.Project(root)
    names = []
    if path:
        full = _resolve_path(path)
        try:
            with open(full, encoding="utf-8") as f:
                src = f.read()
        except Exception as e:
            return f"读取 {path} 失败: {e}"
        # 遍历该文件里符号的每处出现，逐个尝试跟踪定义——首处可能落在注释/字符串里
        # jedi 解析不到，但真实绑定（def/赋值/调用）能解析。限定本文件：path 给定即
        # 「找这个文件里的符号」，绝不 fallback 到全项目而返回别的文件的定义（那会误导）。
        script = jedi.Script(code=src, path=full, project=proj)
        for line, col in _locate_all_symbols(src, name):
            names = script.goto(line, col, follow_imports=True)
            if names:
                break
        if not names:
            return (f"在 {path} 里没找到 `{name}` 的可解析定义"
                    f"（可能只是注释/字符串里提到它，没有真实绑定）；"
                    f"留空 path 可在整个项目搜该符号定义。")
    else:
        # path 没给 → 全项目搜该符号定义。Project.search 不依赖位置，最稳。
        names = list(proj.search(name))
        if not names:
            return f"没找到 `{name}` 的定义。"
    return f"`{name}` 的定义：\n" + "\n".join(_fmt_jedi_name(n, root) for n in names[:10])


@tool
def find_references(name: str, path: str = "") -> str:
    """找符号的所有引用/调用处（谁用了它）。比 search_files 准——按真实绑定找、不误匹配同名。
    name: 符号名。path: 该符号出现的文件（相对项目根）；留空则先在项目里定位其定义再找引用。
    """
    try:
        import jedi
    except ImportError:
        return "未安装 jedi（pip install jedi 启用）；可改用 search_files 正则搜引用。"
    root = _project_cwd()
    proj = jedi.Project(root)
    refs = []
    if path:
        full = _resolve_path(path)
        try:
            with open(full, encoding="utf-8") as f:
                src = f.read()
        except Exception as e:
            return f"读取 {path} 失败: {e}"
        # 遍历该文件里符号的每处出现，逐个尝试找引用；限定本文件，绝不跨到别的文件
        # （否则注释里提到同名 API 时会退到全项目第一个定义，返回和 path 无关的引用）。
        script = jedi.Script(code=src, path=full, project=proj)
        for line, col in _locate_all_symbols(src, name):
            refs = script.get_references(line, col, include_builtins=False)
            if refs:
                break
        if not refs:
            return (f"在 {path} 里没找到 `{name}` 的可解析引用"
                    f"（可能只是注释/字符串里提到它）；"
                    f"留空 path 可先在全项目定位定义再找引用。")
    else:
        # path 没给 → 先全项目搜该符号定义，从定义处找引用
        defs = list(proj.search(name))
        if not defs:
            return f"项目里没找到符号 `{name}`，无引用可查。"
        d = defs[0]
        refs = jedi.Script(path=str(d.module_path), project=proj).get_references(
            d.line, d.column, include_builtins=False)
    if not refs:
        return f"没找到 `{name}` 的引用。"
    return f"`{name}` 的引用（{len(refs)} 处）：\n" + "\n".join(_fmt_jedi_name(n, root) for n in refs[:50])


@tool
def code_map(path: str = "", max_chars: int = 8000) -> str:
    """列出项目（或指定子目录）每个源码文件的类/函数清单（带行号），用于快速定位
    "某功能/类在哪个文件"，省去逐个 read_file 摸索。
    path: 相对项目根的子目录，空 = 整个项目。只读、安全。"""
    import re as _re

    # ── 路径起点 ──
    base = _resolve_path(path) if path else _project_cwd()
    # 安全：不允许 .. 逃出项目根（防扫到项目外 / 敏感目录）
    root = _project_cwd()
    try:
        if os.path.commonpath([os.path.realpath(base), os.path.realpath(root)]) != os.path.realpath(root):
            return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
    except ValueError:  # 不同盘符（Windows）→ 必然越界
        return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
    if not os.path.isdir(base):
        return f"失败：目录不存在 {base}"

    # ── 优先用 tree-sitter（多语言、更准确）；不可用/失败时静默回退 regex ──
    try:
        from .codeintel import code_map_ts
        ts_result = code_map_ts(base, max_chars)
        if ts_result is not None:
            return ts_result
    except Exception:
        pass  # codeintel 模块不可用，回退 regex

    # ── 回退：按扩展名定义正则 ──
    _py_re = _re.compile(r'^(?P<indent>\s*)(?P<kw>async\s+def|def|class)\s+(?P<name>\w+)')
    _js_re = _re.compile(r'^(?P<indent>\s*)(?:export\s+)?(?:async\s+)?(?P<kw>function|class)\s+(?P<name>\w+)')
    _EXT_MAP = {
        ".py": _py_re,
        ".js": _js_re, ".ts": _js_re, ".jsx": _js_re, ".tsx": _js_re,
    }
    _exts = set(_EXT_MAP.keys())

    # ── os.walk：复用 search_files 的噪声目录忽略集合 ──
    files_to_scan = []
    for root, dirs, filenames in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _SEARCH_IGNORE_DIRS and not d.startswith(".")]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in _exts:
                continue
            fpath = os.path.join(root, fname)
            try:
                if os.path.getsize(fpath) > _SEARCH_MAX_FILE_SIZE:
                    continue
            except OSError:
                continue
            files_to_scan.append((fpath, ext))

    files_to_scan.sort()

    # ── 逐文件正则提取符号 ──
    output_lines = []
    for fpath, ext in files_to_scan:
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue

        pat = _EXT_MAP[ext]
        symbols = []
        for i, line in enumerate(lines, 1):
            m = pat.match(line)
            if m:
                indent = m.group("indent")
                keyword = m.group("kw").strip()
                name = m.group("name")
                symbols.append((indent, i, keyword, name))
        if not symbols:
            continue

        rel = os.path.relpath(fpath, base).replace(os.sep, "/")
        output_lines.append(rel)
        for indent, lineno, keyword, name in symbols:
            level = len(indent) // 2 if indent else 0
            prefix = "  " * level
            output_lines.append(f"  L{lineno:<5d} {prefix}{keyword} {name}")

    if not output_lines:
        return f"在 {base} 下未找到可扫描的源文件（.py/.js/.ts/.jsx/.tsx）"

    result = "\n".join(output_lines)
    if len(result) > max_chars:
        result = (
            result[:max_chars]
            + f"\n\n... [输出已截断（{len(output_lines)} 行中的 {max_chars} 字符）；"
            f"用 path 参数缩到子目录重新查看]"
        )
    return result


# ══════════════════════════════════════
# find_tests / related_files 辅助函数
# ══════════════════════════════════════

import ast as _ast


def _is_noise_dir(name: str) -> bool:
    """判断目录名是否为应跳过的噪声目录（.git/venv/node_modules 等）。"""
    return name in _SEARCH_IGNORE_DIRS or name.startswith(".")


def _iter_project_files(root: str, extensions: tuple = (".py",), max_file_size: int = 0):
    """遍历项目根下的文件，跳过噪声目录，返回 (absolute_path, relative_path) 元组。
    max_file_size <= 0 时用默认 _SEARCH_MAX_FILE_SIZE。"""
    _max = max_file_size if max_file_size > 0 else _SEARCH_MAX_FILE_SIZE
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _is_noise_dir(d)]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in extensions:
                continue
            abs_path = os.path.join(dirpath, fname)
            try:
                if os.path.getsize(abs_path) > _max:
                    continue
            except OSError:
                continue
            rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
            yield abs_path, rel


def _relpath(path: str, root: str) -> str:
    """将绝对路径转为相对项目根的正斜杠路径。"""
    return os.path.relpath(path, root).replace(os.sep, "/")


def _module_name_for_py(abs_path: str, root: str) -> str:
    """将 Python 文件绝对路径转为模块名（src/tools.py → src.tools）。
    __init__.py 返回包名（src/__init__.py → src）。"""
    rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
    if rel.endswith(".py"):
        rel = rel[:-3]
    rel = rel.replace("/", ".")
    if rel.endswith(".__init__"):
        rel = rel[:-9]
    return rel


def _module_to_path(module: str, root: str) -> str | None:
    """将模块名（如 src.tools）映射回项目内的文件路径。返回绝对路径或 None。
    支持 src.tools → src/tools.py 和 src.ui.header → src/ui/header.py。
    也尝试包目录 src/__init__.py。"""
    parts = module.split(".")
    # 先试文件
    fpath = os.path.join(root, *parts) + ".py"
    if os.path.isfile(fpath):
        return fpath
    # 再试包目录
    pkg_init = os.path.join(root, *parts, "__init__.py")
    if os.path.isfile(pkg_init):
        return pkg_init
    return None


def _extract_imports_py(abs_path: str, file_module: str = "") -> list[str]:
    """用 ast 从 Python 文件提取 import 的模块名列表。
    file_module: 文件自身的模块名（如 "src.tools"），用于解析相对导入。
    解析失败时退回正则匹配 import/from 语句。"""
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
        tree = _ast.parse(source, filename=abs_path)
    except Exception:
        # 退回到正则
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
        except Exception:
            return []
        modules = []
        for m in re.finditer(r'^\s*(?:import|from)\s+([\w.]+)', source, re.MULTILINE):
            modules.append(m.group(1))
        return modules

    modules = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for alias in node.names:
                modules.append(alias.name)
        elif isinstance(node, _ast.ImportFrom):
            if node.level == 0 and node.module:
                # 同时保留容器模块和导入成员。后者若是项目子模块，
                # `from src import state` 才能精确识别为 src.state。
                modules.append(node.module)
                modules.extend(f"{node.module}.{alias.name}" for alias in node.names)
            elif node.level > 0 and file_module:
                # 相对导入基于当前 package，而非当前文件模块。
                is_package = os.path.basename(abs_path) == "__init__.py"
                package = file_module if is_package else file_module.rpartition(".")[0]
                base_parts = package.split(".") if package else []
                up = node.level - 1
                base = ".".join(base_parts[:len(base_parts) - up]) if up <= len(base_parts) else ""
                if node.module:
                    mod = f"{base}.{node.module}" if base else node.module
                    modules.append(mod)
                    modules.extend(f"{mod}.{alias.name}" for alias in node.names)
                elif base:
                    for alias in node.names:
                        modules.append(f"{base}.{alias.name}")
            elif node.module:
                modules.append(node.module)
    return list(dict.fromkeys(modules))


def _imports_target(imports: list[str], target_module: str) -> bool:
    """是否确实导入目标模块；父包 import 不等于导入其任意子模块。"""
    if not target_module:
        return False
    return any(
        imp == target_module or imp.startswith(target_module + ".")
        for imp in imports
    )


def _extract_imports_generic(abs_path: str) -> list[str]:
    """用 codeintel 从非 Python 文件提取导入路径/模块名。
    返回的字符串是原始 import specifier（如 './utils'、'lodash'、'./api'）。
    codeintel 不可用或解析失败时返回空列表。"""
    try:
        from .codeintel import extract_imports_from_content
    except Exception:
        return []
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return []
    return extract_imports_from_content(content, path=abs_path)


def _resolve_import_to_file(specifier: str, source_dir: str, project_root: str) -> str | None:
    """把 import specifier（如 './utils'、'../services/api'、'./helpers.ts'）解析为
    项目内的绝对文件路径。返回 None 表示解析不到。
    只匹配项目根范围内的文件，第三方库跳过。"""
    # 只处理相对路径（以 ./ 或 ../ 开头）；绝对路径和裸模块名（如 'react'）跳过
    if not specifier.startswith("."):
        return None

    base_dir = os.path.normpath(os.path.join(source_dir, specifier))
    # 尝试后缀列表（按常见度排序）
    extensions = [".ts", ".tsx", ".js", ".jsx", ".py", ""]
    # 先试自身带扩展名的情况
    candidates = [base_dir] + [base_dir + ext for ext in extensions]
    # 也试 index 文件
    candidates += [os.path.join(base_dir, "index" + ext) for ext in extensions]

    real_root = os.path.realpath(project_root)
    for cand in candidates:
        if os.path.isfile(cand):
            real_cand = os.path.realpath(cand)
            if os.path.commonpath([real_cand, real_root]) == real_root:
                return real_cand
    return None


def _find_test_files(root: str) -> list[tuple[str, str]]:
    """在项目根下查找所有测试文件，返回 [(abs_path, rel_path), ...]。
    rel_path 始终相对于项目根。搜索范围：tests/, test/, scripts/ 目录 + 根下所有 test_*.py / *_test.py。"""
    test_files = []
    seen = set()

    # 已知测试目录。即便在 scripts/ 下，也只把 pytest 命名文件当测试，
    # 避免 conftest.py、构建脚本和一次性工具被推荐给 run_tests。
    for d in ("tests", "test", "scripts"):
        dpath = os.path.join(root, d)
        if os.path.isdir(dpath):
            for abs_path, _ in _iter_project_files(dpath, (".py",)):
                basename = os.path.basename(abs_path)
                if not (basename.startswith("test_") or basename.endswith("_test.py")):
                    continue
                if abs_path not in seen:
                    seen.add(abs_path)
                    rel = _relpath(abs_path, root)
                    test_files.append((abs_path, rel))

    # 项目根下直接匹配 test_*.py / *_test.py
    for abs_path, rel in _iter_project_files(root, (".py",)):
        basename = os.path.basename(abs_path)
        if (basename.startswith("test_") or basename.endswith("_test.py")) and abs_path not in seen:
            seen.add(abs_path)
            test_files.append((abs_path, rel))

    return test_files


def _score_test_candidate(
    test_abs: str, test_rel: str,
    target_stem: str, target_module: str,
    symbol: str, root: str,
) -> tuple[int, list[str]]:
    """为一个测试文件打分，返回 (score, reasons)。
    target_stem: 目标文件的无后缀名（如 tools）
    target_module: 目标文件的模块名（如 src.tools）
    symbol: 可选符号名。"""
    score = 0
    reasons = []
    test_stem = os.path.splitext(os.path.basename(test_abs))[0]

    # 1. 文件名强匹配 +50
    if test_stem in (f"test_{target_stem}", f"{target_stem}_test"):
        score += 50
        reasons.append("文件名匹配")
    elif target_stem in test_stem and test_stem.startswith("test_"):
        score += 30
        reasons.append("文件名部分匹配")

    # 2. import 目标模块 +40
    try:
        test_module = _module_name_for_py(test_abs, root)
        imports = _extract_imports_py(test_abs, test_module)
        if _imports_target(imports, target_module):
            score += 40
            reasons.append("import 匹配")
    except Exception:
        pass

    # 3. 符号相关
    if symbol:
        try:
            with open(test_abs, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            # 测试函数名包含 symbol +35
            for m in re.finditer(r'def\s+(test_\w*' + re.escape(symbol) + r'\w*)\s*\(', content):
                score += 35
                reasons.append(f"测试函数 {m.group(1)}")
                break
            else:
                # 文件内容包含 symbol +25
                if symbol in content:
                    score += 25
                    reasons.append(f"内容包含 {symbol}")
        except Exception:
            pass

    # 4. 同目录 / 常见测试目录弱匹配 +10
    top_dir = test_rel.replace("\\", "/").split("/", 1)[0]
    if top_dir in ("tests", "test", "scripts"):
        score += 10
        reasons.append("测试目录")

    return score, reasons


# ══════════════════════════════════════
# find_tests / related_files 工具
# ══════════════════════════════════════


@tool
def find_tests(path: str = "", symbol: str = "", max_results: int = 20) -> str:
    """根据源码文件路径和可选符号名，返回最可能相关的测试文件 / 测试用例候选。
    path: 源码文件或目录，相对项目根或绝对路径。留空 = 项目根。
    symbol: 可选函数 / 类 / 方法名。
    max_results: 最多返回条数，范围 1-50。
    返回匹配的测试文件、命中原因和推荐 run_tests 命令。"""
    max_results = max(1, min(50, max_results))
    root = _project_cwd()

    # 路径解析和安全校验
    if path:
        full = _resolve_path(path)
        try:
            if os.path.commonpath([os.path.realpath(full), os.path.realpath(root)]) != os.path.realpath(root):
                return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
        except ValueError:
            return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
        if not os.path.exists(full):
            return f"失败：路径不存在 {path}"
    else:
        full = root

    # 确定目标文件名和模块名
    if os.path.isfile(full):
        target_stem = os.path.splitext(os.path.basename(full))[0]
        target_module = _module_name_for_py(full, root) if full.endswith(".py") else ""
    elif os.path.isdir(full):
        target_stem = os.path.basename(full.rstrip("/\\")) or ""
        target_module = ""
    else:
        return f"失败：路径不存在 {path}"

    # 搜索测试文件
    test_files = _find_test_files(root)
    if not test_files:
        return "未找到任何测试文件（搜索了 tests/ test/ scripts/ 和 test_*.py / *_test.py 模式）。"

    # 评分
    scored = []
    for test_abs, test_rel in test_files:
        sc, reasons = _score_test_candidate(test_abs, test_rel, target_stem, target_module, symbol, root)
        # 指定目标时，单凭“位于测试目录”的 10 分不算相关；否则所有测试都会入榜。
        # 无 path 时视为“列出项目测试”，允许测试目录弱候选。
        if sc > (0 if not path else 10):
            scored.append((sc, test_rel, reasons))
    scored.sort(key=lambda x: (-x[0], x[1].lower()))

    if not scored:
        return (f"未找到与 `{path or '项目根'}` 相关的测试文件。\n"
                "建议：检查项目是否有 tests/ 或 scripts/ 目录，或手动运行 run_tests() 跑全量测试。")

    # 输出
    lines = [f"为 `{path or '项目根'}` 找到的测试候选（共 {len(scored)} 个，显示前 {max_results}）："]
    if symbol:
        lines.append(f"符号过滤: `{symbol}`")
    lines.append("")

    for sc, rel, reasons in scored[:max_results]:
        reason_str = ", ".join(reasons)
        lines.append(f"- {rel}  (分数 {sc}: {reason_str})")

    # 推荐运行命令
    lines.append("")
    lines.append("建议运行：")
    top_score = scored[0][0]
    top_files = [rel for score, rel, _ in scored if score == top_score][:3]
    if symbol:
        for top_file in top_files:
            try:
                with open(os.path.join(root, top_file), "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                has_test_fn = bool(re.search(r'def\s+test_\w*' + re.escape(symbol), content))
            except Exception:
                has_test_fn = False
            if has_test_fn:
                lines.append(f'run_tests("{top_file}", k="{symbol}")')
            else:
                lines.append(f'run_tests("{top_file}")')
    else:
        for top_file in top_files:
            lines.append(f'run_tests("{top_file}")')
    if len(top_files) > 1:
        lines.append("（以上候选同分；可提供 symbol 进一步缩小范围。）")

    return "\n".join(lines)


@tool
def related_files(path: str, max_results: int = 30) -> str:
    """给定一个源码文件，返回修改它前应关注的相关文件。
    path: 源码文件路径（相对项目根或绝对路径）。
    max_results: 最多返回条数，范围 1-100。
    输出分组：目标文件、它导入的项目内文件、导入它的项目内文件、相关测试候选、建议下一步。"""
    max_results = max(1, min(100, max_results))
    root = _project_cwd()

    # 路径解析和安全校验
    full = _resolve_path(path)
    try:
        if os.path.commonpath([os.path.realpath(full), os.path.realpath(root)]) != os.path.realpath(root):
            return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
    except ValueError:
        return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
    if not os.path.isfile(full):
        return f"失败：文件不存在: {path}"

    rel_target = _relpath(full, root)
    is_py = full.endswith(".py")
    source_dir = os.path.dirname(full)
    target_stem = os.path.splitext(os.path.basename(full))[0]

    # 1. 解析目标文件的 imports
    if is_py:
        target_module = _module_name_for_py(full, root)
        imports = _extract_imports_py(full, target_module)
        imported_files = []
        for mod in imports:
            p = _module_to_path(mod, root)
            if p and os.path.isfile(p):
                imported_files.append(_relpath(p, root))
    else:
        target_module = ""
        imports = _extract_imports_generic(full)
        imported_files = []
        for spec in imports:
            p = _resolve_import_to_file(spec, source_dir, root)
            if p:
                imported_files.append(_relpath(p, root))

    # 去重保持顺序
    seen = set()
    imported_files = [f for f in imported_files if f not in seen and not seen.add(f)]

    # 2. 反向导入：扫描项目源文件，找谁 import 了 target
    reverse_importers = []
    if is_py:
        all_files = list(_iter_project_files(root, (".py",)))
    else:
        exts = (".ts", ".tsx", ".js", ".jsx", ".py")
        all_files = list(_iter_project_files(root, exts))

    for abs_path, rel in all_files:
        if abs_path == full:
            continue
        try:
            if abs_path.endswith(".py") and is_py:
                file_module = _module_name_for_py(abs_path, root)
                file_imports = _extract_imports_py(abs_path, file_module)
                if _imports_target(file_imports, target_module):
                    reverse_importers.append(rel)
            else:
                # 非 Python（或目标非 Python）：用 codeintel 匹配导入路径
                file_imports = _extract_imports_generic(abs_path)
                file_dir = os.path.dirname(abs_path)
                for spec in file_imports:
                    resolved = _resolve_import_to_file(spec, file_dir, root)
                    if resolved and os.path.normcase(resolved) == os.path.normcase(full):
                        reverse_importers.append(rel)
                        break
        except Exception:
            pass

    # 3. 测试候选（复用 find_tests 逻辑）
    test_files = _find_test_files(root)
    test_candidates = []
    for test_abs, test_rel in test_files:
        sc, reasons = _score_test_candidate(test_abs, test_rel, target_stem, target_module, "", root)
        if sc > 10:
            test_candidates.append((sc, test_rel, reasons))
    test_candidates.sort(key=lambda x: (-x[0], x[1].lower()))

    # 构建输出
    lines = [f"目标文件: {rel_target}", ""]

    # 它导入的项目内文件
    if imported_files:
        lines.append("它导入的项目内文件:")
        for f in imported_files[:max_results]:
            lines.append(f"- {f}")
        lines.append("")

    # 导入它的项目内文件
    if reverse_importers:
        lines.append("导入它的项目内文件:")
        for f in reverse_importers[:max_results]:
            lines.append(f"- {f}")
        lines.append("")

    # 相关测试候选
    if test_candidates:
        lines.append("相关测试候选:")
        for sc, rel, reasons in test_candidates[:10]:
            reason_str = ", ".join(reasons)
            lines.append(f"- {rel}  ({reason_str})")
        lines.append("")

    # 建议下一步
    lines.append("建议下一步:")
    lines.append(f'- read_file("{rel_target}", ...)')
    if imported_files:
        lines.append(f'- read_file("{imported_files[0]}", ...)')
    if reverse_importers:
        lines.append(f'- read_file("{reverse_importers[0]}", ...)')
    if test_candidates:
        top_test = test_candidates[0][1]
        lines.append(f'- run_tests("{top_test}")')
    else:
        lines.append("- run_tests()  # 全量测试")

    return "\n".join(lines)


# ══════════════════════════════════════
# 测试运行工具
# ══════════════════════════════════════


def _parse_pytest_output(stdout: str, elapsed: float = 0.0) -> str:
    """解析 pytest stdout，提取计数 + 失败用例摘要 + 错误详情；
    解析不到则退回末尾 ~2000 字。
    失败时在末尾追加 [REPAIR_INFO] 结构化块，供自动修复循环使用。"""
    lines = stdout.strip().splitlines()

    # ── 计数：从末尾 summary 行抓 passed / failed / error ──
    passed = failed = errors = 0
    m_passed = re.search(r'(\d+)\s+passed', stdout)
    m_failed = re.search(r'(\d+)\s+failed', stdout)
    m_error  = re.search(r'(\d+)\s+error', stdout)
    if m_passed:
        passed = int(m_passed.group(1))
    if m_failed:
        failed = int(m_failed.group(1))
    if m_error:
        errors = int(m_error.group(1))

    has_counts = bool(m_passed or m_failed or m_error)

    # ── 失败用例行：pytest -q 末尾 "FAILED path::test — ErrorType: msg" ──
    failed_lines = []
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("FAILED "):
            failed_lines.insert(0, stripped)
        elif failed_lines:
            # 遇到非 FAILED 行就停（FAILED 块通常是连续的）
            break

    # ── 提取每个失败用例的短 traceback（--tb=short 输出段） ──
    # pytest --tb=short 格式：
    #   FAILED tests/test_foo.py::test_bar - AssertionError: ...
    #   ====== short test summary =======
    #   或在 stdout 中间有 "____ test_bar ____" 分隔段 + traceback
    error_details: dict[str, str] = {}  # test_id -> 最后几行错误信息
    _tb_section_re = re.compile(r'^_{2,}\s+(.+?)\s+_{2,}$')  # ______ test_bar ______
    _file_line_re = re.compile(r'^\s+(.+\.py):(\d+):\s+in\s+')  # 文件:行号: in func
    _assertion_re = re.compile(r'^[Ee]\s+(.+)$')  # E   AssertionError: ...

    i = 0
    while i < len(lines):
        # 匹配 traceback 段的标题行 "____ test_name ____"
        m = _tb_section_re.match(lines[i])
        if m:
            test_name = m.group(1)
            # 往后扫描几行，找文件:行号 和 E 行
            tb_lines: list[str] = []
            j = i + 1
            while j < len(lines) and j < i + 30:  # 限制扫描范围
                line = lines[j]
                # 遇到下一个段标题或短 summary 行停止
                if _tb_section_re.match(line) and j > i + 1:
                    break
                if line.strip().startswith("FAILED ") or line.strip().startswith("===="):
                    break
                fm = _file_line_re.match(line)
                if fm:
                    tb_lines.append(f"  File {fm.group(1)}:{fm.group(2)}")
                am = _assertion_re.match(line)
                if am:
                    tb_lines.append(f"  {am.group(1)}")
                j += 1
            if tb_lines:
                error_details[test_name] = "\n".join(tb_lines[-4:])  # 最多 4 行
            i = j
            continue
        i += 1

    # ── 无法解析：退回 stdout 末尾 ~2000 字 ──
    if not has_counts and not failed_lines:
        tail = stdout[-2000:] if len(stdout) > 2000 else stdout
        return f"（pytest 输出解析未命中计数/失败行，以下为原始输出尾部）\n{tail}"

    # ── 拼精炼摘要 ──
    time_str = f"（{elapsed:.2f}s）" if elapsed > 0 else ""
    parts = []
    if failed or errors:
        parts.append(f"❌ {failed + errors} failed / {passed} passed{time_str}")
    else:
        parts.append(f"✅ {passed} passed{time_str}，全部通过")

    if failed_lines:
        parts.append("失败用例：")
        for fl in failed_lines[:20]:  # 最多列 20 条
            # "FAILED path::test — ErrorType: msg" 原样展示
            test_info = fl[len("FAILED "):]
            parts.append(f"  - {test_info}")
            # 尝试匹配 traceback 详情
            # test_info 格式："path::test_name — ErrorType: msg"
            test_id = test_info.split(" — ")[0].strip() if " — " in test_info else test_info.strip()
            # 按 test_id 或 test_id 的最后部分（::test_name）匹配
            for key, detail in error_details.items():
                if key == test_id or test_id.endswith(f"::{key}") or key in test_id:
                    parts.append(detail)
                    break
        if len(failed_lines) > 20:
            parts.append(f"  ...（共 {len(failed_lines)} 个失败用例，仅列前 20）")

    # ── [REPAIR_INFO] 结构化块（供自动修复循环解析） ──
    if failed or errors:
        repair_info: list[str] = []
        repair_info.append(f"status=failed|tests={failed + errors}|passed={passed}|errors={errors}")
        for fl in failed_lines[:5]:  # 最多报 5 个
            test_info = fl[len("FAILED "):]
            repair_info.append(f"test: {test_info}")
        parts.append("\n[REPAIR_INFO]")
        parts.append("\n".join(repair_info))
        parts.append("[/REPAIR_INFO]")
        parts.append("\n（用 read_file 打开对应文件定位修复）")

    return "\n".join(parts)


def _resolve_python():
    """挑一个真 Python 解释器跑 pytest（不是裸 sys.executable）：
    ① 项目内 venv（.venv/venv/env）——最贴合被测项目的依赖
    ② 开发期（非 frozen）用 sys.executable（应用自己的 Python，跟项目同环境时正好）
    ③ 系统 PATH 上的 python/python3
    打包(frozen)后 sys.executable=灵犀.exe、`-m pytest` 跑不了，故 frozen 下跳过 ②。"""
    root = _project_cwd()
    bindir = "Scripts" if os.name == "nt" else "bin"
    pyname = "python.exe" if os.name == "nt" else "python"
    for venv in (".venv", "venv", "env"):
        cand = os.path.join(root, venv, bindir, pyname)
        if os.path.isfile(cand):
            return cand
    if not getattr(sys, "frozen", False):
        return sys.executable
    return shutil.which("python") or shutil.which("python3") or sys.executable


@tool
def run_tests(path: str = "", k: str = "", timeout: int = 300) -> str:
    """跑 pytest 测试，返回精炼结果：通过/失败数 + 每个失败用例的位置和错误摘要。
    path: 测试路径/文件（相对项目根，空 = pytest 自动发现）。k: pytest -k 过滤表达式。
    比 run_command 跑 pytest 省事——直接给你哪些挂了、错在哪，便于定位修复。"""
    # ── 构建命令：<解释器> -m pytest（frozen 下 sys.executable=exe 不能用，见 _resolve_python） ──
    cmd = [_resolve_python(), "-m", "pytest", "--tb=short", "-q"]

    # ── path 安全校验：_resolve_path + commonpath 防逃逸（同 code_map） ──
    if path:
        resolved = _resolve_path(path)
        root = _project_cwd()
        try:
            if os.path.commonpath([os.path.realpath(resolved), os.path.realpath(root)]) != os.path.realpath(root):
                return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
        except ValueError:
            return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
        cmd.append(resolved)

    if k:
        cmd.extend(["-k", k])

    # ── 执行 ──
    try:
        t0 = time.time()
        result = subprocess.run(
            cmd, cwd=_shell_cwd(),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        elapsed = time.time() - t0
    except FileNotFoundError:
        try: _v_mark_tests(_session.get_verification(), None, "pytest 未安装或找不到")
        except Exception: pass
        return "pytest 未安装或找不到，请先运行 `pip install pytest` 安装。"
    except subprocess.TimeoutExpired:
        try: _v_mark_tests(_session.get_verification(), None, f"测试超时（>{timeout}s）")
        except Exception: pass
        return f"测试超时（>{timeout}s），可能有用例卡住，请检查或增大 timeout。"
    except Exception as e:
        try: _v_mark_tests(_session.get_verification(), None, f"运行 pytest 失败: {e}")
        except Exception: pass
        return f"运行 pytest 失败: {e}"

    # ── 解析输出 ──
    stdout = result.stdout or ""
    stderr = result.stderr or ""

    # pytest 没装时 stderr 里会有 "No module named pytest"
    if stderr and "No module named pytest" in stderr:
        try: _v_mark_tests(_session.get_verification(), None, "pytest 未安装")
        except Exception: pass
        return "pytest 未安装，请先运行 `pip install pytest` 安装。"

    summary = _parse_pytest_output(stdout, elapsed)

    # pytest 的 warning/提示信息单独附加（有的话挺有用）
    warning_lines = []
    for line in (stderr or "").strip().splitlines():
        if "warning" in line.lower() or "Warning" in line:
            warning_lines.append(line)
    if warning_lines and len("\n".join(warning_lines)) < 500:
        summary += "\n\n⚠️ pytest warnings:\n" + "\n".join(warning_lines[:10])

    # ── 输出截断防爆 ──
    if len(summary) > 6000:
        summary = summary[:6000] + "\n... [输出已截断，超过 6000 字]"

    # ── 标记验证状态：测试已执行 ──
    try: _v_mark_tests(_session.get_verification(), result.returncode == 0)
    except Exception: pass

    return summary


# ══════════════════════════════════════
# 自我校验闭环：静态检查（lint/语法），编辑后自动回灌错误给模型自修
# ══════════════════════════════════════


def _bundled_ruff():
    """打包随 app 发的 ruff 可执行文件（onefile 在 _MEIPASS、onedir 在 exe 旁）。
    见 lingxi.spec 的 _ruff_datas。没有返回 None。"""
    name = "ruff.exe" if os.name == "nt" else "ruff"
    bases = [getattr(sys, "_MEIPASS", None)]
    if getattr(sys, "frozen", False):
        bases.append(os.path.dirname(sys.executable))
    for base in bases:
        if base:
            p = os.path.join(base, name)
            if os.path.isfile(p):
                return p
    return None


def _run_code_check(full_path: str):
    """对单个文件跑静态检查。返回 (issues, checker)：
    - checker=None → 没有可用检查器（不支持的语言且没配 check_command）
    - issues=""    → 检查通过、无问题
    - issues=非空  → 问题文本（file:line: 说明）
    Python 优先 ruff（只选 F/E9 = pyflakes 正确性 + 语法错，避开风格噪声），
    没装 ruff 退化到 py_compile（只查语法）。其它语言走 config 的 check_command。"""
    from .config import CHECK_COMMAND
    ext = os.path.splitext(full_path)[1].lower()
    cwd = _shell_cwd()

    # 其它语言：用户自定义命令（shell 执行，{file} 占位）
    if CHECK_COMMAND:
        return _run_check_subprocess(CHECK_COMMAND.replace("{file}", full_path), cwd, True, "check_command")

    if ext != ".py":
        return None, None

    # Python：优先 ruff。打包后 sys.executable=exe，-m ruff 跑不了，所以 frozen 下只认
    # 独立二进制（ruff 是自包含 exe）；开发期才用 sys.executable -m ruff（不看 PATH 最稳）。
    import importlib.util
    frozen = getattr(sys, "frozen", False)
    bundled = _bundled_ruff()
    ruff_cmd = None
    if bundled:
        # 最优先：随包发的 ruff.exe（打包产物开箱即用、版本可控）
        ruff_cmd = [bundled, "check", "--select", "F,E9", full_path]
    elif not frozen and importlib.util.find_spec("ruff") is not None:
        ruff_cmd = [sys.executable, "-m", "ruff", "check", "--select", "F,E9", full_path]
    elif shutil.which("ruff"):
        ruff_cmd = [shutil.which("ruff"), "check", "--select", "F,E9", full_path]
    if ruff_cmd:
        return _run_check_subprocess(ruff_cmd, cwd, False, "ruff")

    # 兜底：内置 compile() 进程内查语法——不起子进程，打包后（sys.executable=exe）照样可用。
    # 只查语法错（抓不到未定义名/未用导入），建议在应用 Python 里 pip install ruff 拿完整检查。
    return _py_syntax_check(full_path), "py_compile"


def _run_check_subprocess(cmd, cwd, use_shell, checker):
    """跑一个检查命令，返回 (issues, checker)。退出码 0 = 通过("")；非 0 = 问题文本。"""
    try:
        r = subprocess.run(
            cmd, cwd=cwd, shell=use_shell, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=60,
        )
    except Exception:
        return None, None
    if r.returncode == 0:
        return "", checker
    out = ((r.stdout or "") + ("\n" + r.stderr if r.stderr else "")).strip()
    if len(out) > 2000:
        out = out[:2000] + "\n... [检查输出已截断]"
    return out or f"{checker} 返回非零退出码（无输出）", checker


def _py_syntax_check(full_path):
    """用内置 compile() 在进程内查 Python 语法错（不起子进程，打包后也能用）。
    通过返回 ""；语法错返回 "文件:行: SyntaxError: ..."；读不了文件返回 ""（不打扰）。"""
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            src = f.read()
    except Exception:
        return ""
    try:
        compile(src, full_path, "exec")
        return ""
    except SyntaxError as e:
        return f"{os.path.basename(full_path)}:{e.lineno or '?'}: SyntaxError: {e.msg}"
    except Exception as e:
        return f"{os.path.basename(full_path)}: 语法检查失败: {e}"


def _auto_check_suffix(full_path: str) -> str:
    """编辑/写入成功后自动校验，返回追加到工具结果的提示串。
    无问题 / 不支持的语言 / 开关关闭 → 返回 ""（不打扰）。"""
    from .config import AUTO_CHECK_AFTER_EDIT
    if not AUTO_CHECK_AFTER_EDIT:
        return ""
    try:
        issues, checker = _run_code_check(full_path)
    except Exception:
        return ""
    if checker:
        _mark_current_check(full_path, not bool(issues), checker)
    elif os.path.splitext(full_path)[1].lower() in {
        ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs",
        ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift",
        ".kt", ".kts", ".vue", ".svelte",
    }:
        _mark_current_check(full_path, None, "")
    if not checker or not issues:
        return ""
    return f"\n\n⚠️ 自动校验（{checker}）发现问题，请修复后再继续：\n{issues}"


def _parse_patch(content: str):
    """解析 patch 字符串，返回 (operations, errors)。

    每个 operation 是 dict:
      {"action": "add"|"update"|"delete", "path": str, "content": str,
       "hunks": [{"hint": str, "lines": [str]}] (update only),
       "new_lines": [str] (add only)}
    errors 是 list[str]。
    """
    raw_lines = content.split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines = raw_lines[:-1]

    if not any(l.startswith("*** Begin Patch") for l in raw_lines):
        return [], ["缺少 *** Begin Patch 标记"]

    operations = []
    errors = []
    current_op = None

    for line in raw_lines:
        sline = line.strip()

        # 整体开始/结束标记（忽略）
        if sline == "*** Begin Patch":
            continue
        if sline == "*** End Patch":
            break

        if sline.startswith("*** Update File:"):
            path = sline[len("*** Update File:"):].strip()
            current_op = {"action": "update", "path": path, "hunks": []}
            operations.append(current_op)
        elif sline.startswith("*** Add File:"):
            path = sline[len("*** Add File:"):].strip()
            current_op = {"action": "add", "path": path, "new_lines": []}
            operations.append(current_op)
        elif sline.startswith("*** Delete File:"):
            path = sline[len("*** Delete File:"):].strip()
            current_op = {"action": "delete", "path": path}
            operations.append(current_op)
        elif sline.startswith("***"):
            errors.append(f"无法识别的文件操作: {sline}")
        elif current_op is not None and current_op["action"] == "update":
            if line.startswith("@@"):
                hint = line[2:].strip()
                current_op["hunks"].append({"hint": hint, "lines": []})
            elif current_op["hunks"]:
                current_op["hunks"][-1]["lines"].append(line)
            else:
                if line and not line.startswith(" "):
                    errors.append(f"在 hunk 头 (@@) 之前遇到非上下文行: {line}")
                current_op.setdefault("_preamble", []).append(line)
        elif current_op is not None and current_op["action"] == "add":
            if line.startswith("+"):
                current_op["new_lines"].append(line[1:])
            elif not line.strip():
                pass
            else:
                current_op["new_lines"].append(line)

    return operations, errors


@tool
def apply_patch(patch: str) -> str:
    """批量文件补丁工具：在一个原子操作中创建、修改、删除多个文件。

    Patch 格式（类似 git diff，但靠上下文定位、不用行号）：

    *** Begin Patch
    *** Update File: src/utils.py
    @@
     def greet(name):
    -    print("hi")
    +    print(f"hi {name}")
    *** Add File: src/bar.py
    +def baz():
    +    return 1
    *** Delete File: src/old.py
    *** End Patch

    规则（务必照做，否则文件内容会错）：
    - 每个文件块以 *** Update File / Add File / Delete File: <相对路径> 开头
    - Update 用 @@ 起一个 hunk；行首第一个字符是标记：空格=上下文、- =删除、+ =新增
    - **标记后【紧跟】内容，标记和内容之间不要再加空格**：写 `+def x():` / `+    return 1`，
      别写 `+ def x():`——那个空格会变成文件内容，导致缩进 / 语法错。缩进是内容自身的缩进。
    - 上下文行写文件里【真实存在且连续】的行，**不能跳过中间的空行或其它行**
      （定位靠精确匹配，不够精确会判失败、让你补全上下文重试，绝不模糊猜测）
    - Add File：每行都是 +<内容>；目标已存在 → 失败
    - Delete File：无 hunk；目标不存在 → 失败
    - 路径不能用 ../ 逃出项目；任何 hunk 定位失败或路径非法 → 整个 patch 中止、不改任何文件
    - 改完自动跑代码检查（lint/语法），有问题会一并提示
    """
    # ── Phase 1: 解析 ──
    operations, parse_errors = _parse_patch(patch)
    if parse_errors:
        return "Patch 格式错误:\n" + "\n".join(f"  - {e}" for e in parse_errors)

    if not operations:
        return "Patch 为空（没有文件操作）。"

    # ── Phase 2: 校验 + 内存计算（不写盘）──
    root = _project_cwd()
    resolved_ops = []       # (action, full_path, old_content, new_content, resolved_path)
    errors = []

    for op in operations:
        action = op["action"]
        path = op["path"]
        if not path:
            errors.append(f"路径为空（{action} 操作）")
            continue

        resolved = _resolve_path(path)
        try:
            if os.path.commonpath([os.path.realpath(resolved), os.path.realpath(root)]) != os.path.realpath(root):
                errors.append(f"路径超出项目范围，不允许: {path}")
                continue
        except ValueError:
            errors.append(f"路径超出项目范围，不允许: {path}")
            continue

        if action == "update":
            if not os.path.isfile(resolved):
                errors.append(f"文件不存在，无法更新: {path}")
                continue
            with open(resolved, "r", encoding="utf-8") as f:
                content = f.read()

            hunk_failures = []
            for i, hunk in enumerate(op["hunks"]):
                # hunk → old_block(上下文+删除行) / new_block(上下文+新增行)，复用 edit_file 的
                # _locate_edit 做【连续块】匹配 + 缩进对齐（不自己造"允许间隙"的匹配器——那会把
                # 上下文行锚到文件里不相干的散落位置、产出垃圾编辑）。
                old_lines, new_lines = [], []
                for line in hunk["lines"]:
                    if line.startswith("-"):
                        old_lines.append(line[1:])
                    elif line.startswith("+"):
                        new_lines.append(line[1:])
                    else:
                        c = line[1:] if line.startswith(" ") else line
                        old_lines.append(c)
                        new_lines.append(c)

                if not old_lines:
                    hunk_failures.append(f"Hunk {i+1}: 无上下文/删除行，无法定位（纯新增请带上下文）")
                    continue

                old_block = "\n".join(old_lines)
                new_block = "\n".join(new_lines)
                status, spans, new_texts, info = _locate_edit(content, old_block, new_block, False)
                # 多文件原子补丁不做模糊猜测：只接受精确 / 规范化(去行尾空白 + 缩进重对齐)匹配，
                # 且必须唯一命中。none/fuzzy/multi 一律判失败——让模型补全连续上下文重试，
                # 绝不在原子补丁里靠相似度猜位置（会 silent 改错地方）。
                if status not in ("exact", "normalized") or len(spans) != 1:
                    reason = {
                        "none": "未找到对应连续块",
                        "fuzzy": "只能模糊匹配，上下文不够精确",
                        "multi": "匹配到多处无法确定",
                    }.get(status, status)
                    hunk_failures.append(f"Hunk {i+1} 定位失败（{reason}）——请给更精确的连续上下文")
                    continue
                start, end = spans[0]
                content = content[:start] + new_texts[0] + content[end:]

            if hunk_failures:
                errors.append(f"文件 {path}:\n" + "\n".join(f"  - {e}" for e in hunk_failures))
                continue

            resolved_ops.append((action, path, resolved, None, content))

        elif action == "add":
            if os.path.exists(resolved):
                errors.append(f"文件已存在，无法新增: {path}")
                continue
            new_content = "\n".join(op.get("new_lines", []))
            if new_content and not new_content.endswith("\n"):
                new_content += "\n"
            resolved_ops.append((action, path, resolved, None, new_content))

        elif action == "delete":
            if not os.path.exists(resolved):
                errors.append(f"文件不存在，无法删除: {path}")
                continue
            resolved_ops.append((action, path, resolved, None, None))

    if errors:
        return "Patch 校验失败:\n" + "\n".join(f"  - {e}" for e in errors)

    # ── Phase 3: 汇总 diff ──
    all_diffs = []
    for action, path, resolved, _, new_content in resolved_ops:
        if action == "update":
            with open(resolved, "r", encoding="utf-8") as f:
                old_content = f.read()
            diff_text = "".join(difflib.unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{path}", tofile=f"b/{path}", n=3,
            ))
            if diff_text:
                all_diffs.append(diff_text)
        elif action == "add":
            diff_text = "".join(difflib.unified_diff(
                [],
                new_content.splitlines(keepends=True),
                fromfile="/dev/null", tofile=f"b/{path}", n=3,
            ))
            all_diffs.append(diff_text)
        elif action == "delete":
            with open(resolved, "r", encoding="utf-8") as f:
                old_content = f.read()
            diff_text = "".join(difflib.unified_diff(
                old_content.splitlines(keepends=True),
                [],
                fromfile=f"a/{path}", tofile="/dev/null", n=3,
            ))
            all_diffs.append(diff_text)

    if not all_diffs:
        return "Patch 为空（没有实际变化）。"

    combined_diff = "\n".join(all_diffs)

    # ── Phase 4: 用户确认 ──
    allowed, reject = _confirm_file_write("(patch)", "", combined_diff)
    if not allowed:
        return reject

    # ── Phase 5: 写盘 ──
    added_files = 0
    modified_files = 0
    deleted_files = 0
    check_warnings = []

    for action, path, resolved, _, new_content in resolved_ops:
        try:
            _checkpoint.make_checkpoint(root, f"apply_patch_{action}", resolved)
        except Exception as e:
            logger.warning(f"checkpoint 失败（不影响 patch 应用）: {e}")

        if action == "add":
            os.makedirs(os.path.dirname(os.path.abspath(resolved)), exist_ok=True)
            with open(resolved, "w", encoding="utf-8") as f:
                f.write(new_content)
            added_files += 1
        elif action == "update":
            with open(resolved, "w", encoding="utf-8") as f:
                f.write(new_content)
            modified_files += 1
        elif action == "delete":
            os.remove(resolved)
            deleted_files += 1

        if action in ("add", "update"):
            _mark_current_dirty(resolved)
            issues, checker = _run_code_check(resolved)
            if checker:
                _mark_current_check(resolved, not bool(issues), checker)
            else:
                _mark_current_check(resolved, None, "")
            if checker and issues:
                check_warnings.append(f"{path}:\n{issues}")
        elif action == "delete":
            _mark_current_dirty(resolved)

    # ── 组装结果 ──
    parts = [f"Patch 已应用: {added_files} 个新增, {modified_files} 个修改, {deleted_files} 个删除"]
    if check_warnings:
        parts.append("\n⚠️ 自动校验发现问题:\n" + "\n".join(check_warnings))
    return "\n".join(parts)


@tool
def check_code(path: str) -> str:
    """静态检查单个代码文件（lint/语法），返回问题列表。Python 用 ruff（没装则
    py_compile 只查语法）；其它语言用 config 的 check_command（{file} 占位）。
    path: 要检查的文件（相对项目根）。注：编辑文件后已会自动校验，这个用于手动复查。"""
    if not path:
        return "请指定要检查的文件 path。"
    resolved = _resolve_path(path)
    root = _project_cwd()
    try:
        if os.path.commonpath([os.path.realpath(resolved), os.path.realpath(root)]) != os.path.realpath(root):
            return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
    except ValueError:
        return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
    if not os.path.exists(resolved):
        return f"文件不存在: {resolved}"
    issues, checker = _run_code_check(resolved)
    if checker is None:
        ext = os.path.splitext(resolved)[1] or "（无扩展名）"
        _mark_current_check(resolved, None, "")
        return f"没有可用的检查器处理 {ext} 文件。可在 config.json 配 check_command（用 {{file}} 占位）。"
    passed = not issues
    _mark_current_check(resolved, passed, checker)
    if passed:
        return f"✓ {checker} 检查通过，无问题。"
    # 提取问题的文件:行号供自动修复循环使用
    issue_lines = issues.strip().splitlines()
    repair_entries: list[str] = [f"status=failed|checker={checker}"]
    for il in issue_lines[:10]:
        repair_entries.append(f"issue: {il.strip()}")
    parts = [f"{checker} 检查发现问题：\n{issues}"]
    parts.append("\n[REPAIR_INFO]")
    parts.append("\n".join(repair_entries))
    parts.append("[/REPAIR_INFO]")
    return "\n".join(parts)


# ══════════════════════════════════════
# Git 只读工具（diff / log，绝不碰 commit/add/push/reset）
# ══════════════════════════════════════


@tool
def git_diff(path: str = "", staged: bool = False, max_chars: int = 8000) -> str:
    """查看 git 改动（默认未暂存的工作区改动）。path: 限定文件/目录（相对项目根，空=全部）。
    staged=True 看已暂存（git add 过）的改动。只读、调研用。"""
    try:
        import shutil as _shutil
        if not _shutil.which("git"):
            return "git 未安装或不在 PATH 中，无法查看 diff。"

        cwd = _project_cwd()
        cmd = ["git", "diff"]

        if staged:
            cmd.append("--staged")

        if path:
            resolved = _resolve_path(path)
            root = _project_cwd()
            try:
                if os.path.commonpath([os.path.realpath(resolved), os.path.realpath(root)]) != os.path.realpath(root):
                    return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
            except ValueError:
                return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
            cmd.extend(["--", path])

        result = subprocess.run(
            cmd, cwd=cwd,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            if "not a git repository" in stderr.lower():
                return "当前项目不是 git 仓库。"
            return f"git diff 执行出错: {stderr or '未知错误'}"

        output = result.stdout or ""
        if not output.strip():
            return "暂存区没有改动。" if staged else "工作区干净，没有未提交改动。"

        if len(output) > max_chars:
            output = (
                output[:max_chars]
                + f"\n\n... [输出已截断（共 {len(output)} 字符），"
                f"可用 path 参数缩小到具体文件/目录查看]"
            )
        # 标记 diff 已审查（验证状态）
        try:
            from . import session as _session
            _v_mark_diff_reviewed(_session.get_verification())
        except Exception:
            pass
        return output

    except FileNotFoundError:
        return "git 未安装或不在 PATH 中，无法查看 diff。"
    except subprocess.TimeoutExpired:
        return "git diff 执行超时（10s），仓库可能过大。"
    except Exception as e:
        return f"git diff 执行异常: {e}"


@tool
def git_log(path: str = "", limit: int = 15) -> str:
    """查看最近 git 提交历史（短 hash + 日期 + 提交信息 + 改动文件）。
    path: 限定某文件/目录（相对项目根）。limit: 条数。只读。"""
    try:
        import shutil as _shutil
        if not _shutil.which("git"):
            return "git 未安装或不在 PATH 中，无法查看 log。"

        cwd = _project_cwd()
        cmd = [
            "git", "log",
            "-n", str(limit),
            "--date=short",
            "--pretty=format:%h %ad %s",
            "--stat",
        ]

        if path:
            resolved = _resolve_path(path)
            root = _project_cwd()
            try:
                if os.path.commonpath([os.path.realpath(resolved), os.path.realpath(root)]) != os.path.realpath(root):
                    return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
            except ValueError:
                return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
            cmd.extend(["--", path])

        result = subprocess.run(
            cmd, cwd=cwd,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            if "not a git repository" in stderr.lower():
                return "当前项目不是 git 仓库。"
            if "does not have any commits" in stderr:
                return "仓库还没有任何提交记录。"
            return f"git log 执行出错: {stderr or '未知错误'}"

        output = result.stdout or ""
        if not output.strip():
            return "没有提交历史（仓库可能为空或 path 下无提交记录）。"

        if len(output) > 8000:
            output = output[:8000] + "\n\n... [输出已截断，可减小 limit 或用 path 限定查看]"
        return output

    except FileNotFoundError:
        return "git 未安装或不在 PATH 中，无法查看 log。"
    except subprocess.TimeoutExpired:
        return "git log 执行超时（10s）。"
    except Exception as e:
        return f"git log 执行异常: {e}"


def _is_inside_git_repo(cwd: str) -> bool:
    """检测 cwd 是否在 git 仓库内。"""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _git_available() -> bool:
    """git 是否可用。"""
    import shutil as _shutil
    return _shutil.which("git") is not None


def build_git_write_confirmation(name: str, args: dict | None = None) -> str:
    """构造 Git 写操作确认文案；只读，不改变索引或工作区。"""
    args = args or {}
    if name == "git_commit":
        message = str(args.get("message", "") or "").strip()
        cwd = _project_cwd()
        staged = ""
        if _git_available() and _is_inside_git_repo(cwd):
            try:
                result = subprocess.run(
                    ["git", "diff", "--cached", "--name-status"],
                    cwd=cwd, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=10,
                )
                staged = (result.stdout or "").strip()
            except Exception:
                staged = ""
        staged_text = staged or "（暂存区为空，工具会拒绝提交）"
        return (
            "将执行 Git 写操作：创建本地提交\n\n"
            f"提交信息：{message or '（空）'}\n\n"
            "将提交当前暂存区的全部内容：\n"
            f"{staged_text}\n\n"
            "不会自动暂存其它文件，也不会 push。是否允许？"
        )

    paths = args.get("paths") or []
    if isinstance(paths, list):
        path_text = "\n".join(f"- {p}" for p in paths) or "（未提供路径）"
    else:
        path_text = str(paths)
    action = "暂存指定路径" if name == "git_stage" else "取消暂存指定路径"
    effect = (
        "会修改 Git 暂存区，不会创建提交，也不会 push。"
        if name == "git_stage"
        else "只修改 Git 暂存区，工作区文件内容保持不变。"
    )
    return (
        f"将执行 Git 写操作：{action}\n\n"
        f"目标路径：\n{path_text}\n\n"
        f"{effect}\n是否允许？"
    )


def _validate_git_paths(paths, root: str) -> tuple[list[str] | None, str]:
    """校验路径列表：非空、无通配符、无越界、存在性。

    返回 (rel_paths, error_msg)。成功时 error_msg 为空串。
    """
    if not paths:
        return None, "失败：路径列表不能为空，请指定具体文件或目录。"
    if not isinstance(paths, list):
        return None, "失败：paths 参数必须是字符串列表。"

    root_real = os.path.realpath(root)
    rel_paths = []
    for p in paths:
        if not isinstance(p, str) or not p.strip():
            return None, f"失败：路径无效（非空字符串）：{p!r}"
        p_clean = p.strip()
        # 拒绝通配符 / shell 危险字符
        if p_clean in (".", "*", "./"):
            return None, f"失败：不允许暂存 '{p_clean}'（只允许具体路径）。"
        if any(c in p_clean for c in ("&", "|", ";", "$", "`", "\n", "\r")):
            return None, f"失败：路径包含非法字符：{p_clean!r}"
        # 解析并检查越界
        resolved = _resolve_path(p_clean)
        abs_real = os.path.realpath(resolved)
        try:
            if os.path.commonpath([abs_real, root_real]) != root_real:
                return None, f"失败：路径超出项目范围：{p_clean}"
        except ValueError:
            return None, f"失败：路径超出项目范围：{p_clean}"
        # 转成相对路径传给 git
        rel = os.path.relpath(abs_real, root_real).replace("\\", "/")
        rel_paths.append(rel)
    return rel_paths, ""


@tool
def git_status(max_chars: int = 12000) -> str:
    """查看当前仓库状态，包含分支、ahead/behind、暂存/未暂存/未跟踪文件。只读。"""
    try:
        if not _git_available():
            return "git 未安装或不在 PATH 中。"
        cwd = _project_cwd()
        if not _is_inside_git_repo(cwd):
            return "当前项目不是 git 仓库。"

        result = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            return f"git status 执行出错: {stderr or '未知错误'}"

        output = result.stdout or ""
        if not output.strip():
            output = "(仓库干净，无任何改动)\n"

        # 截断提示
        truncated = ""
        if len(output) > max_chars:
            truncated = f"\n\n... [输出已截断（共 {len(output)} 字符），可用 path 参数缩小范围]"
            output = output[:max_chars]

        # 解析分支行
        lines = output.strip().splitlines()
        branch_line = lines[0] if lines else ""
        # 统计文件状态
        staged = sum(1 for l in lines[1:] if l and l[0] in "MADRC")
        unstaged = sum(1 for l in lines[1:] if len(l) > 1 and l[1] in "MD")
        untracked = sum(1 for l in lines[1:] if l.startswith("??"))

        summary_parts = []
        if staged:
            summary_parts.append(f"已暂存: {staged}")
        if unstaged:
            summary_parts.append(f"未暂存: {unstaged}")
        if untracked:
            summary_parts.append(f"未跟踪: {untracked}")

        result_text = output.rstrip() + truncated
        result_text += "\n\n--- 摘要 ---"
        if branch_line:
            result_text += f"\n{branch_line}"
        if summary_parts:
            result_text += "\n" + " | ".join(summary_parts)
        else:
            result_text += "\n工作区干净。"
        result_text += "\n\n💡 提交前建议先运行 git_diff(staged=True) 查看暂存区内容。"
        return result_text

    except FileNotFoundError:
        return "git 未安装或不在 PATH 中。"
    except subprocess.TimeoutExpired:
        return "git status 执行超时（10s）。"
    except Exception as e:
        return f"git status 执行异常: {e}"


@tool
def git_stage(paths: list[str]) -> str:
    """暂存指定路径。只接受明确路径列表，不接受空列表、通配符或 '.'。"""
    try:
        if not _git_available():
            return "git 未安装或不在 PATH 中。"
        cwd = _project_cwd()
        if not _is_inside_git_repo(cwd):
            return "当前项目不是 git 仓库。"

        rel_paths, err = _validate_git_paths(paths, cwd)
        if err:
            return err

        cmd = ["git", "add", "--"] + rel_paths
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            return f"git add 执行出错: {stderr or '未知错误'}"

        # 获取暂存区摘要
        cached = subprocess.run(
            ["git", "diff", "--cached", "--name-status"],
            cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        cached_output = (cached.stdout or "").strip()

        output = f"✅ 已暂存以下路径:\n" + "\n".join(f"  · {p}" for p in rel_paths)
        if cached_output:
            output += f"\n\n--- 当前暂存区 ---\n{cached_output}"
        output += "\n\n💡 建议继续用 git_diff(staged=True) 审查暂存内容，确认无误后再提交。"
        return output

    except FileNotFoundError:
        return "git 未安装或不在 PATH 中。"
    except subprocess.TimeoutExpired:
        return "git add 执行超时（30s）。"
    except Exception as e:
        return f"git stage 执行异常: {e}"


@tool
def git_unstage(paths: list[str]) -> str:
    """取消暂存指定路径，不修改工作区内容。"""
    try:
        if not _git_available():
            return "git 未安装或不在 PATH 中。"
        cwd = _project_cwd()
        if not _is_inside_git_repo(cwd):
            return "当前项目不是 git 仓库。"

        rel_paths, err = _validate_git_paths(paths, cwd)
        if err:
            return err

        # 优先用 git restore --staged（Git 2.23+），不支持则降级到 git reset --
        cmd = ["git", "restore", "--staged", "--"] + rel_paths
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        if result.returncode != 0:
            # 降级
            cmd = ["git", "reset", "--"] + rel_paths
            result = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
            )
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                return f"git unstage 执行出错: {stderr or '未知错误'}"

        # 获取暂存区摘要
        cached = subprocess.run(
            ["git", "diff", "--cached", "--name-status"],
            cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        cached_output = (cached.stdout or "").strip()

        output = f"✅ 已取消暂存:\n" + "\n".join(f"  · {p}" for p in rel_paths)
        if cached_output:
            output += f"\n\n--- 当前暂存区 ---\n{cached_output}"
        else:
            output += "\n\n--- 当前暂存区为空 ---"
        output += "\n\nℹ️ 工作区文件内容未被修改。"
        return output

    except FileNotFoundError:
        return "git 未安装或不在 PATH 中。"
    except subprocess.TimeoutExpired:
        return "git unstage 执行超时（30s）。"
    except Exception as e:
        return f"git unstage 执行异常: {e}"


@tool
def git_commit(message: str) -> str:
    """基于当前暂存区创建本地提交。不会自动暂存文件，不会 push。"""
    try:
        if not _git_available():
            return "git 未安装或不在 PATH 中。"
        cwd = _project_cwd()
        if not _is_inside_git_repo(cwd):
            return "当前项目不是 git 仓库。"

        # 校验 message
        if not message or len(message.strip()) < 3:
            return "失败：提交信息不能为空，且去掉空白后长度需 >= 3 个字符。"

        # 检查暂存区是否为空
        diff_cached = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        if diff_cached.returncode == 0:
            # returncode=0 表示暂存区和 HEAD 一样 → 没有暂存内容
            return "失败：暂存区为空，没有可提交的内容。请先用 git_stage 暂存文件。"

        # 检查工作区未暂存改动 / 未跟踪文件（仅提示，不阻止）
        warnings = []
        status_result = subprocess.run(
            ["git", "status", "--short"],
            cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        if status_result.returncode == 0:
            status_lines = (status_result.stdout or "").strip().splitlines()
            unstaged_files = []
            untracked_files = []
            for line in status_lines:
                if not line or len(line) < 2:
                    continue
                # 第二列是工作区状态（暂存区由 commit 处理）
                if line[0] == "?" and line[1] == "?":
                    untracked_files.append(line[3:].strip() if len(line) > 3 else "")
                elif line[1] in "MD":
                    unstaged_files.append(line[3:].strip() if len(line) > 3 else "")
            if unstaged_files:
                warnings.append(f"⚠️ 以下文件有未暂存改动，不会进入本次提交:\n" +
                                "\n".join(f"  · {f}" for f in unstaged_files[:20]))
            if untracked_files:
                warnings.append(f"⚠️ 以下未跟踪文件不会进入本次提交:\n" +
                                "\n".join(f"  · {f}" for f in untracked_files[:20]))

        # 获取暂存文件列表（给提交后的摘要用）
        staged_names = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        staged_files = [f for f in (staged_names.stdout or "").strip().splitlines() if f.strip()]

        # 执行提交
        cmd = ["git", "commit", "-m", message.strip()]
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            # 常见失败：user.name/email 未配置 / pre-commit hook
            msg = stderr or stdout or "未知错误"
            if "user.name" in msg or "user.email" in msg or "Author identity" in msg:
                return f"提交失败：Git 用户信息未配置。请先运行:\n  git config user.name \"你的名字\"\n  git config user.email \"你的邮箱\"\n\n详细: {msg}"
            if "pre-commit" in msg.lower() or "hook" in msg.lower():
                return f"提交失败：pre-commit hook 拦截。请修复后重试。\n\n详细: {msg}"
            return f"提交失败: {msg}"

        # 解析 commit hash
        hash_result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=5,
        )
        commit_hash = (hash_result.stdout or "").strip() or "unknown"

        output = f"✅ 提交成功: {commit_hash}\n"
        output += f"   信息: {message.strip()}\n"
        if staged_files:
            output += f"   包含 {len(staged_files)} 个文件:\n"
            output += "\n".join(f"     · {f}" for f in staged_files[:30])
            if len(staged_files) > 30:
                output += f"\n     ... 等共 {len(staged_files)} 个文件"
        if warnings:
            output += "\n\n" + "\n\n".join(warnings)
        output += "\n\nℹ️ 仅创建本地提交，未执行 push。"
        return output

    except FileNotFoundError:
        return "git 未安装或不在 PATH 中。"
    except subprocess.TimeoutExpired:
        return "git commit 执行超时（30s）。"
    except Exception as e:
        return f"git commit 执行异常: {e}"


# ══════════════════════════════════════
# 网络工具（只读调研用）
# ══════════════════════════════════════


@tool
def fetch_url(url: str, max_chars: int = 8000) -> str:
    """抓取网页正文，用于查文档/报错信息/API 参考。

    url: 要抓取的网址（必须是 http:// 或 https://）
    max_chars: 最大返回字符数，默认 8000

    只允许 http/https 协议；按 Content-Type 处理内容类型；
    HTML 自动去标签转为可读纯文本。只读、不弹确认。"""
    import requests as _requests
    import html as _html

    # 协议白名单
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"不支持的协议: {parsed.scheme or '(无协议)'}。只允许 http:// 或 https://"

    try:
        resp = _requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (LingXi)"})
    except _requests.Timeout:
        return f"请求超时（15 秒）: {url}"
    except _requests.ConnectionError as e:
        return f"连接失败: {e}"
    except Exception as e:
        return f"请求异常: {e}"

    if resp.status_code < 200 or resp.status_code >= 300:
        return f"HTTP {resp.status_code}: 服务器返回非 2xx 状态码"

    content_type = resp.headers.get("Content-Type", "").lower()

    # 二进制类型直接拒绝
    if any(t in content_type for t in ("image/", "application/pdf", "audio/", "video/",
                                        "application/zip", "application/octet-stream")):
        return f"不支持的内容类型: {content_type.split(';')[0].strip()}"

    text = resp.text

    # JSON / 纯文本直接返回
    if "json" in content_type or (content_type.startswith("text/") and "html" not in content_type):
        result = text[:max_chars]
        if len(text) > max_chars:
            result += "... [已截断]"
        return result

    # HTML → 纯文本
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style", "head"]):
            tag.decompose()
        result = soup.get_text(separator="\n")
    except ImportError:
        # beautifulsoup4 未安装，用正则处理
        cleaned = re.sub(r"<(script|style|head)[^>]*>.*?</\1>", "", text, flags=re.S | re.I)
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        result = _html.unescape(cleaned)

    # 收敛连续空白和空行
    result = re.sub(r"[ \t]+", " ", result)
    result = re.sub(r"\n\s*\n+", "\n\n", result)
    result = result.strip()

    truncated = len(result) > max_chars
    result = result[:max_chars]
    if truncated:
        result += "\n... [已截断]"
    return result


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """用 Tavily 搜索引擎搜索网络内容，返回标题、链接和摘要。

    query: 搜索关键词
    max_results: 最大返回结果数，默认 5

    需要在 config.json 配置 web_search_api_key（tavily.com 免费申请）。
    只读、不弹确认。"""
    import requests as _requests

    from .config import WEB_SEARCH_API_KEY
    if not WEB_SEARCH_API_KEY:
        return "未配置搜索服务，请在 config.json 填 web_search_api_key（tavily.com 免费申请）"

    try:
        resp = _requests.post(
            "https://api.tavily.com/search",
            json={"api_key": WEB_SEARCH_API_KEY, "query": query, "max_results": max_results},
            timeout=15,
        )
    except _requests.Timeout:
        return "搜索请求超时（15 秒），请稍后重试"
    except _requests.ConnectionError as e:
        return f"搜索连接失败: {e}"
    except Exception as e:
        return f"搜索请求异常: {e}"

    if resp.status_code < 200 or resp.status_code >= 300:
        return f"搜索服务返回 HTTP {resp.status_code}，请检查 API key 是否正确"

    try:
        data = resp.json()
    except Exception:
        return "搜索服务返回了无法解析的响应"

    results = data.get("results", [])
    if not results:
        return "没搜到"

    lines = []
    for item in results:
        title = item.get("title", "(无标题)")
        url = item.get("url", "")
        content = item.get("content", "")
        lines.append(f"{title}\n  {url}\n  {content}")
    return "\n\n".join(lines)


@tool
def generate_video(prompt: str, image: str = "", width: int = 1152, height: int = 768,
                   num_frames: int = 121, frame_rate: int = 24, max_wait: int = 600) -> str:
    """用 Agnes Video V2.0 生成视频（文生视频；传 image 则图生视频/让图动起来）。
    异步任务：创建 → 轮询（带进度心跳）→ 下载 mp4 存到项目 outputs/。
    prompt: 视频内容文字描述。image: 可选，输入图片——可传 http(s) URL，也可传本地文件路径
            （本地路径会自动上传到 litterbox 临时图床 1h 换成公网 URL，因为 Agnes 只能拉公网图）。
    width/height: 默认 1152x768。num_frames: 帧数，需 ≤441 且为 8n+1（默认 121≈5 秒 @24fps）。
    frame_rate: FPS（1-60）。max_wait: 总时长上限秒数（兜底，默认 600）。
    轮询用心跳判活：进度推进就实时上报、不砍；进度卡住 90 秒不动判为卡死、提前放弃。
    需在 config.json 配 agnes_api_key（agnes-ai.com 免费申请）。"""
    from .config import AGNES_API_KEY
    if not AGNES_API_KEY:
        return "未配置 Agnes API key，请在 config.json 填 agnes_api_key（agnes-ai.com 免费申请）。"
    import requests as _requests

    # 图生视频：image 可以是 http(s) URL（直接用）或本地路径。本地路径自动上传到 litterbox
    # 临时图床（1h 后过期，比永久图床隐私）换成公网 URL——Agnes 服务器只能拉公网图。
    if image and not image.lower().startswith(("http://", "https://")):
        img_path = _resolve_path(image)
        if not os.path.isfile(img_path):
            return f"图片不存在: {image}"
        try:
            with open(img_path, "rb") as _f:
                up = _requests.post(
                    "https://litterbox.catbox.moe/resources/internals/api.php",
                    data={"reqtype": "fileupload", "time": "1h"},
                    files={"fileToUpload": _f}, timeout=60)
            url = (up.text or "").strip()
            if not (200 <= up.status_code < 300) or not url.startswith("http"):
                return f"本地图片上传失败（litterbox）：HTTP {up.status_code} {up.text[:200]}"
            image = url     # 换成公网临时 URL
        except Exception as e:
            return f"本地图片上传失败：{e}"

    base = "https://apihub.agnes-ai.com/v1/videos"
    headers = {"Authorization": f"Bearer {AGNES_API_KEY}", "Content-Type": "application/json"}
    body = {"model": "agnes-video-v2.0", "prompt": prompt,
            "width": width, "height": height, "num_frames": num_frames, "frame_rate": frame_rate}
    if image:
        body["image"] = image     # 图生视频（此时已是公网 URL）

    # F12 调试记录：把这次 Agnes API 调用的请求 / 响应 / 错误记进 debug inspector。
    # 用 _finrec 在每个出口统一收尾，保证无论成功失败都能在 F12 看到。
    try:
        from .debug_log import make_api_record, finalize_api_record
        _rec = make_api_record("agnes-video-v2.0", "agnes", f"POST {base}", body)
    except Exception:
        _rec = None

    def _finrec(text="", error=None):
        if _rec is not None:
            try:
                finalize_api_record(_rec, text, error)
            except Exception:
                pass

    # ① 创建任务
    try:
        r = _requests.post(base, json=body, headers=headers, timeout=30)
    except Exception as e:
        _finrec(error=f"创建请求异常: {e}")
        return f"创建视频任务失败: {e}"
    if not (200 <= r.status_code < 300):
        _finrec(error=f"HTTP {r.status_code}: {r.text[:500]}")
        return f"创建视频任务失败 HTTP {r.status_code}: {r.text[:300]}"
    try:
        create_resp = r.json()
        task_id = create_resp.get("id")
    except Exception:
        _finrec(error=f"创建响应无法解析: {r.text[:500]}")
        return f"创建任务响应无法解析: {r.text[:300]}"
    if not task_id:
        _finrec(error=f"无 task id: {str(create_resp)[:500]}")
        return f"创建任务未返回 task id: {r.text[:300]}"

    _ui = getattr(state, "ui_ref", None)
    if _ui is not None:
        try:
            _ui.show_message(f"\n🎬 视频任务已提交（{task_id}），生成中（约 1-3 分钟）...\n", "tool_result")
        except Exception:
            pass

    # ② 轮询直到 completed / failed / 总超时。
    # 注意：Agnes 实测全程报 progress=0、最后一跳到 100（5 秒视频 ~122s），所以【不能靠进度判卡死】——
    # 只认 status 判活；进度不可靠时按【时间】报"还在生成（已等 Xs）"心跳，让人知道没死。
    poll_url = f"{base}/{task_id}"
    t0 = time.time()
    last_beat = t0                  # 上次给 UI 报活的时刻
    last_shown_prog = -1
    data = {}
    video_url = None
    BEAT = 25                       # 每隔多少秒报一次"还在生成"
    while time.time() - t0 < max_wait:
        time.sleep(6)
        try:
            data = _requests.get(poll_url, headers=headers, timeout=30).json()
        except Exception:
            continue
        status = (data.get("status") or "").lower()
        if status == "completed":
            # 实际视频地址在 remixed_from_video_id（Agnes 文档写 video_url，但实测对不上）
            video_url = data.get("video_url") or data.get("remixed_from_video_id") or data.get("url")
            break
        if status in ("failed", "error", "cancelled"):
            _finrec(error=f"任务 {task_id} status={status}: {str(data)[:500]}")
            return f"视频生成失败（status={status}）: {str(data)[:300]}"
        # 心跳：有真实进度就报进度，否则按时间报"还在生成（已等 Xs）"
        prog = data.get("progress", 0) or 0
        now = time.time()
        if _ui is not None and (prog - last_shown_prog >= 15 or now - last_beat >= BEAT):
            last_shown_prog = max(last_shown_prog, prog)
            last_beat = now
            tip = f"{prog}%" if prog > 0 else f"已等 {int(now - t0)}s"
            try:
                _ui.show_message(f"\n🎬 视频生成中（{tip}）...\n", "tool_result")
            except Exception:
                pass
    if not video_url:
        _finrec(error=f"任务 {task_id} 超 {max_wait}s 未完成，最后状态: {str(data)[:300]}")
        return (f"视频生成超过 {max_wait}s 仍未完成（任务 {task_id}）。可能还在处理，"
                "可稍后查或调大 max_wait。")

    # ③ 下载 mp4 到 outputs/
    try:
        out_dir = os.path.join(_project_cwd(), "outputs")
        os.makedirs(out_dir, exist_ok=True)
        fpath = os.path.join(out_dir, "video_" + time.strftime("%Y%m%d_%H%M%S") + ".mp4")
        vr = _requests.get(video_url, timeout=180)
        with open(fpath, "wb") as f:
            f.write(vr.content)
    except Exception as e:
        _finrec(text=f"video_url={video_url}", error=f"下载失败: {e}")
        return f"视频已生成但下载失败: {e}\n可直接打开源 URL: {video_url}"
    _finrec(text=f"已生成 {fpath}\n{str(data)[:500]}")
    return (f"已生成视频: {fpath}"
            f"（{data.get('size', '?')}, {data.get('seconds', '?')}s）\n源 URL: {video_url}")


# 导出
ALL_TOOLS = [
    read_file, write_file, append_file, edit_file,
    list_directory, run_command,
    search_in_file, search_files,
    find_definition, find_references,
    find_tests, related_files,
    generate_image,
    remember, forget,
    update_plan,
    get_project_instructions,
    notify_user,
    read_background_output, list_background_commands, stop_background_command,
    code_map,
    git_diff, git_log,
    git_status, git_stage, git_unstage, git_commit,
    run_tests, check_code,
    apply_patch,
    fetch_url, web_search,
    generate_video,
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
    "find_definition": "🔎 跳转定义",
    "find_references": "🔗 查找引用",
    "find_tests": "🧪 查找测试",
    "related_files": "📁 关联文件",
    "generate_image": "🎨 生成图片",
    "generate_video": "🎬 生成视频",
    "remember": "🧠 记住事实",
    "forget": "🗑️ 遗忘记忆",
    "update_plan": "📋 更新计划",
    "read_background_output": "📋 读取后台输出",
    "list_background_commands": "📋 列出后台命令",
    "stop_background_command": "⏹ 停止后台命令",
    "code_map": "🗺 代码地图",
    "git_diff": "🔀 查看改动",
    "git_log": "📜 提交历史",
    "git_status": "📌 Git 状态",
    "git_stage": "➕ 暂存文件",
    "git_unstage": "➖ 取消暂存",
    "git_commit": "✅ 创建提交",
    "run_tests": "🧪 跑测试",
    "check_code": "🔎 代码检查",
    "apply_patch": "📦 批量补丁",
    "fetch_url": "🌐 抓取网页",
    "web_search": "🔍 网络搜索",
    "get_project_instructions": "📜 项目规则",
}


TOOL_MAP = get_tool_map()
