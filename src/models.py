"""模型注册表 + LLM 工厂 + 视觉能力判断。

- MODEL_LIST：可选模型清单（显示名 / 类型 / 模型ID / 是否支持思考）
- VISION_MODEL_IDS：支持图片输入的模型 ID 集合
- _create_llm()：按 model_index 创建对应 LangChain ChatXxx 实例
- describe_images_with_vision()：用视觉模型把图片转文本，给非视觉模型使用
- check_ollama()：检测 Ollama 本机服务是否在线
"""
import os
import urllib.request

from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage

from . import state
from .config import (
    OLLAMA_BASE_URL,
    CLOUD_API_KEY,
    CLOUD_BASE_URL,
    ANTHROPIC_API_KEY,
    GOOGLE_API_KEY,
    MIMO_API_KEY,
    MIMO_BASE_URL,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    CUSTOM_MODELS,
)


# 可选模型列表: (显示名, 类型, 模型ID, 支持思考)
# 用户自定义模型（来自 config.json: custom_models）会在加载时追加进来，类型 = "custom"
BUILTIN_MODEL_LIST = [
    ("MiMo V2.5 Pro",          "mimo",        "mimo-v2.5-pro",               False),
    ("MiMo V2.5",              "mimo",        "mimo-v2.5",                   False),
    ("MiMo V2 Pro",            "mimo",        "mimo-v2-pro",                 False),
    ("MiMo V2 Omni (多模态)",  "mimo",        "mimo-v2-omni",                False),
    ("Claude Code",            "claude-code", "claude",               False),
    ("Qwen3.5 本地",           "ollama",      "qwen3.5:latest",       True),
    ("Qwen3.5-Plus 云端",      "cloud",       "qwen3.5-plus",         False),
    ("Qwen-Max 云端",          "cloud",       "qwen-max",             False),
    ("Qwen-Plus 云端",         "cloud",       "qwen-plus",            False),
    ("Qwen-Turbo 云端",        "cloud",       "qwen-turbo",           False),
    ("Claude Sonnet 4 API",    "anthropic",   "claude-sonnet-4-20250514",    False),
    ("Claude Haiku 3.5 API",   "anthropic",   "claude-3-5-haiku-20241022",   False),
    # DeepSeek V4：思考模式开启时 langchain 不会把 reasoning_content 回传给 API，
    # 多轮工具调用会触发 "reasoning_content must be passed back" 400 错误。
    # 暂时关闭思考模式（模型本身依然在内部推理，只是不暴露 think 块）。
    ("DeepSeek V4 Flash",      "deepseek",    "deepseek-v4-flash",           False),
    ("DeepSeek V4 Pro",        "deepseek",    "deepseek-v4-pro",             False),
]


def _build_model_list():
    """合成最终的 MODEL_LIST：内置 + 用户自定义。

    自定义条目 4-tuple 跟内置一致 (name, type, model_id, supports_thinking)，
    type 固定 = "custom"。真正的 protocol / base_url / api_key 走 CUSTOM_MODELS
    那个 dict（_create_llm 时按 model_id 反查）。
    """
    base = list(BUILTIN_MODEL_LIST)
    for cm in CUSTOM_MODELS or []:
        try:
            base.append((
                f"⚙ {cm.get('name', cm.get('model_id', '?'))}",
                "custom",
                cm.get("model_id", ""),
                bool(cm.get("supports_thinking", False)),
            ))
        except Exception:
            continue
    return base


MODEL_LIST = _build_model_list()


def _lookup_custom_model(model_id: str):
    """按 model_id 在 CUSTOM_MODELS 里找回完整配置。找不到返回 None。"""
    for cm in CUSTOM_MODELS or []:
        if cm.get("model_id") == model_id:
            return cm
    return None


def _looks_like_placeholder(value: str) -> bool:
    v = str(value or "").strip().lower()
    if not v:
        return True
    return "xxxx" in v or v in {"your-api-key", "your_api_key", "api-key", "sk-"}


def get_model_config_issues(model_index=None):
    """Return user-facing config problems for the selected model."""
    if model_index is None:
        model_index = state.current_model_index
    if model_index < 0 or model_index >= len(MODEL_LIST):
        return ["当前模型索引无效。"]

    name, mtype, model_id, _ = MODEL_LIST[model_index]
    issues = []

    def require_key(label, key):
        if _looks_like_placeholder(key):
            issues.append(f"{name} 需要在 config.json 配置 {label}。")

    if mtype == "cloud":
        require_key("qwen_api_key", CLOUD_API_KEY)
    elif mtype == "anthropic":
        require_key("anthropic_api_key", ANTHROPIC_API_KEY)
    elif mtype == "mimo":
        require_key("mimo_api_key", MIMO_API_KEY)
    elif mtype == "gemini":
        require_key("google_api_key", GOOGLE_API_KEY)
    elif mtype == "deepseek":
        require_key("deepseek_api_key", DEEPSEEK_API_KEY)
    elif mtype == "custom":
        cm = _lookup_custom_model(model_id) or {}
        require_key(f"custom_models 中 {name} 的 api_key", cm.get("api_key", ""))
        protocol = (cm.get("protocol") or "openai").lower()
        if protocol not in {"openai", "anthropic"}:
            issues.append(f"{name} 的 custom protocol 暂不支持：{protocol}")

    return issues


# 支持图片输入的模型 ID 集合（用于 UI 在用户发图片时自动切换）
_BUILTIN_VISION_IDS = {
    "mimo-v2-omni",
    "claude-sonnet-4-20250514",
    "claude-3-5-haiku-20241022",
    "qwen-vl-plus",
    "qwen-vl-max",
}
# 自定义模型用户标记了 supports_vision=True 时也归到这个集合
VISION_MODEL_IDS = _BUILTIN_VISION_IDS | {
    cm.get("model_id", "")
    for cm in (CUSTOM_MODELS or [])
    if cm.get("supports_vision")
}


_LLM_CACHE = {}


def current_model_supports_vision():
    """当前选中的模型是否支持图片"""
    return MODEL_LIST[state.current_model_index][2] in VISION_MODEL_IDS


def find_vision_model_index():
    """返回首个支持图片的模型 index，找不到返回 -1"""
    for i, (_, _, model_id, _) in enumerate(MODEL_LIST):
        if model_id in VISION_MODEL_IDS:
            return i
    return -1


def check_ollama():
    """检测 Ollama 服务是否可用"""
    try:
        urllib.request.urlopen(OLLAMA_BASE_URL, timeout=3)
        return True
    except Exception:
        return False


def _create_llm(model_index=None, reasoning=None):
    """根据选择创建 LLM 实例"""
    if model_index is None:
        model_index = state.current_model_index
    if reasoning is None:
        reasoning = state.reasoning_enabled

    name, mtype, model_id, supports_think = MODEL_LIST[model_index]

    # 长超时：深度思考阶段服务端可能数分钟不发 SSE，默认超时容易被中间代理切断
    LONG_TIMEOUT = 1800  # 30 分钟

    if mtype == "ollama":
        kwargs = {"model": model_id, "base_url": OLLAMA_BASE_URL}
        if supports_think and reasoning:
            kwargs["reasoning"] = True
        return ChatOllama(**kwargs)
    elif mtype == "anthropic":
        return ChatAnthropic(
            model=model_id,
            api_key=ANTHROPIC_API_KEY,
            max_tokens=8192,
            default_request_timeout=LONG_TIMEOUT,
        )
    elif mtype == "mimo":
        return ChatAnthropic(
            model=model_id,
            api_key=MIMO_API_KEY,
            base_url=MIMO_BASE_URL,
            max_tokens=8192,
            default_request_timeout=LONG_TIMEOUT,
        )
    elif mtype == "gemini":
        return ChatGoogleGenerativeAI(
            model=model_id,
            google_api_key=GOOGLE_API_KEY,
            timeout=LONG_TIMEOUT,
        )
    elif mtype == "deepseek":
        kwargs = {
            "model": model_id,
            "api_key": DEEPSEEK_API_KEY,
            "base_url": DEEPSEEK_BASE_URL,
            "timeout": LONG_TIMEOUT,
        }
        # DeepSeek V4 服务端默认开启思考模式，但 langchain 不能把 reasoning_content
        # 回传到下一轮，会触发 "reasoning_content must be passed back" 400 错。
        # 必须显式禁用思考模式。未来如果灵犀能正确保留 reasoning_content，再支持开启。
        if "v4" in model_id.lower():
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        return ChatOpenAI(**kwargs)
    elif mtype == "custom":
        # 用户自定义模型：从 CUSTOM_MODELS 反查完整配置，按 protocol 选 SDK
        cm = _lookup_custom_model(model_id) or {}
        protocol = (cm.get("protocol") or "openai").lower()
        api_key = cm.get("api_key", "")
        base_url = cm.get("base_url", "")
        if protocol == "anthropic":
            return ChatAnthropic(
                model=model_id,
                api_key=api_key,
                base_url=base_url or None,
                max_tokens=8192,
                default_request_timeout=LONG_TIMEOUT,
            )
        # 默认 OpenAI 兼容协议（适配大多数第三方 API：OpenAI / 月之暗面 /
        # 火山引擎 / 智谱 / 硅基流动 / 自部署 vLLM 等都走这个）
        kwargs = {
            "model": model_id,
            "api_key": api_key,
            "timeout": LONG_TIMEOUT,
        }
        if base_url:
            kwargs["base_url"] = base_url
        return ChatOpenAI(**kwargs)
    else:
        kwargs = {
            "model": model_id,
            "api_key": CLOUD_API_KEY,
            "base_url": CLOUD_BASE_URL,
            "timeout": LONG_TIMEOUT,
        }
        return ChatOpenAI(**kwargs)


_create_llm_uncached = _create_llm


def _create_llm(model_index=None, reasoning=None):
    """Create or reuse a LangChain LLM instance for the selected model."""
    if model_index is None:
        model_index = state.current_model_index
    if reasoning is None:
        reasoning = state.reasoning_enabled
    _, mtype, model_id, supports_think = MODEL_LIST[model_index]
    effective_reasoning = bool(reasoning and supports_think)
    custom = _lookup_custom_model(model_id) if mtype == "custom" else None
    custom_key = None
    if custom:
        custom_key = (
            custom.get("protocol", ""),
            custom.get("api_key", ""),
            custom.get("base_url", ""),
        )
    key = (model_index, mtype, model_id, effective_reasoning, custom_key)
    if key not in _LLM_CACHE:
        _LLM_CACHE[key] = _create_llm_uncached(model_index=model_index, reasoning=reasoning)
    return _LLM_CACHE[key]


def _image_content_block_for_model(model_index, path, b64):
    """按指定模型协议构造图片 content block。"""
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    mime = "jpeg" if ext in ("jpg", "jpeg") else (ext or "png")
    name, mtype, model_id, _ = MODEL_LIST[model_index]
    # 判断协议：内置 anthropic/mimo 走 anthropic block；custom 看 protocol 字段
    use_anthropic = mtype in ("anthropic", "mimo")
    if mtype == "custom":
        cm = _lookup_custom_model(model_id) or {}
        use_anthropic = (cm.get("protocol") or "openai").lower() == "anthropic"
    if use_anthropic:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": f"image/{mime}",
                "data": b64,
            },
        }
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/{mime};base64,{b64}"},
    }


def describe_images_with_vision(user_text, images):
    """用视觉模型把图片转成文本描述，供非视觉强模型继续处理。

    images: [(path, base64), ...]
    返回: (vision_model_name, description)
    """
    vision_idx = find_vision_model_index()
    if vision_idx < 0:
        raise RuntimeError("没有可用的视觉模型")

    vision_name = MODEL_LIST[vision_idx][0]
    vision_llm = _create_llm(model_index=vision_idx, reasoning=False)

    content = []
    for path, b64 in images:
        content.append(_image_content_block_for_model(vision_idx, path, b64))

    original_question = (user_text or "").strip() or "用户只上传了图片，没有附加文字。"
    content.append({
        "type": "text",
        "text": (
            "你是图片识别/OCR 助手。请把图片内容转换成给另一个更强文本/代码模型使用的中文上下文。\n"
            "要求：\n"
            "1. 客观描述图片里可见的信息，不要脑补。\n"
            "2. 如果是报错、代码、终端、网页、软件界面或设计稿，优先提取所有关键文字、错误信息、路径、行号、按钮、布局和状态。\n"
            "3. 如果图片里有代码，请尽量按原样抄录关键片段。\n"
            "4. 不要直接解决用户问题，不要写最终答案，只输出识别结果。\n\n"
            f"用户原始问题：{original_question}"
        ),
    })

    resp = vision_llm.invoke([HumanMessage(content=content)])
    desc = getattr(resp, "content", str(resp))
    if isinstance(desc, list):
        parts = []
        for part in desc:
            if isinstance(part, dict):
                parts.append(part.get("text", ""))
            else:
                parts.append(str(part))
        desc = "\n".join(p for p in parts if p)
    return vision_name, str(desc).strip()
