"""config.json 加载与密钥导出。

启动时一次性读取，对外暴露各家上游的常量。
任何模块要拿密钥/base_url 都从这里导入，不要重复读文件。
"""
import json

from .paths import CONFIG_PATH, logger


def _safe_float(key: str, default: float, min_val: float = 0.1, max_val: float = 10.0) -> float:
    """安全读取浮点数配置，自动校验范围并回退默认值"""
    try:
        val = float(_config.get(key, default))
        if val < min_val or val > max_val:
            logger.warning(f"{key}={val} 超出合理范围 [{min_val}, {max_val}]，重置为 {default}")
            return default
        return val
    except (ValueError, TypeError):
        logger.warning(f"{key} 配置值无效，重置为 {default}")
        return default


try:
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as _f:
        _config = json.load(_f)
except FileNotFoundError:
    logger.warning("config.json 不存在，请复制 config.example.json 为 config.json 并填入密钥")
    _config = {}
except json.JSONDecodeError as e:
    logger.error(f"config.json 格式错误: {e}，使用空配置")
    _config = {}


OLLAMA_BASE_URL = _config.get("ollama_base_url", "http://127.0.0.1:11434")
CLOUD_API_KEY = _config.get("qwen_api_key", "")
CLOUD_BASE_URL = _config.get("qwen_base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")
ANTHROPIC_API_KEY = _config.get("anthropic_api_key", "")
GOOGLE_API_KEY = _config.get("google_api_key", "")
MIMO_API_KEY = _config.get("mimo_api_key", "")
MIMO_BASE_URL = _config.get("mimo_base_url", "https://token-plan-sgp.xiaomimimo.com/anthropic")
DEEPSEEK_API_KEY = _config.get("deepseek_api_key", "")
DEEPSEEK_BASE_URL = _config.get("deepseek_base_url", "https://api.deepseek.com")

# 各 provider 的可选 model_id 列表（用户在设置里编辑，重启后生效）
MIMO_MODELS       = _config.get("mimo_models", ["mimo-v2.5-pro", "mimo-v2.5", "mimo-v2-pro", "mimo-v2-omni"])
QWEN_CLOUD_MODELS = _config.get("qwen_cloud_models", ["qwen3.5-plus", "qwen-max", "qwen-plus", "qwen-turbo"])
OLLAMA_MODELS     = _config.get("ollama_models", ["qwen3.5:latest"])
ANTHROPIC_MODELS  = _config.get("anthropic_models", ["claude-sonnet-4-20250514", "claude-3-5-haiku-20241022"])
GEMINI_MODELS     = _config.get("gemini_models", [])
DEEPSEEK_MODELS   = _config.get("deepseek_models", ["deepseek-v4-flash", "deepseek-v4-pro"])
CLAUDE_CODE_MODEL = _config.get("claude_code_model", "")
VISION_MODEL_ID   = _config.get("vision_model_id", "")
# 启动默认选中的模型（按 model_id 匹配；找不到退回列表第一个）
DEFAULT_MODEL_ID  = _config.get("default_model_id", "mimo-v2.5-pro")


# 自定义模型列表。用户在设置里加自己的 OpenAI/Anthropic 兼容模型。
# 每项格式：{
#   "name":              "GPT-4 Turbo",         # 显示名（顶栏下拉看到的）
#   "model_id":          "gpt-4-turbo",         # 发给 API 的 model 字段
#   "api_key":           "sk-...",
#   "base_url":          "https://api.openai.com/v1",
#   "protocol":          "openai" | "anthropic",  # 走哪个 SDK
#   "supports_vision":   false,                  # 是否能吃图片
#   "supports_thinking": false,                  # 是否支持 reasoning 模式
# }
CUSTOM_MODELS = _config.get("custom_models", [])

# 语音
VOICE_STT_MODEL = _config.get("voice_stt_model", "small")            # whisper 模型 size
VOICE_STT_LANGUAGE = _config.get("voice_stt_language", "zh")
VOICE_TTS_DEFAULT_ENABLED = _config.get("voice_tts_default_enabled", False)

# GPT-SoVITS TTS（唯一支持的 TTS 后端）
GPT_SOVITS_URL = _config.get("gpt_sovits_url", "http://127.0.0.1:9880")
GPT_SOVITS_REF_AUDIO = _config.get("gpt_sovits_ref_audio", "")        # 参考音频文件路径
GPT_SOVITS_PROMPT_TEXT = _config.get("gpt_sovits_prompt_text", "")    # 参考音频对应的文本
GPT_SOVITS_PROMPT_LANG = _config.get("gpt_sovits_prompt_lang", "zh")  # zh / en / ja / yue
GPT_SOVITS_TEXT_LANG = _config.get("gpt_sovits_text_lang", "zh")     # 要合成文本的语言
GPT_SOVITS_MEDIA_TYPE = _config.get("gpt_sovits_media_type", "wav")  # wav / mp3 / ogg
GPT_SOVITS_TEXT_SPLIT_METHOD = _config.get("gpt_sovits_text_split_method", "cut5")  # cut0..cut5

# GPT-SoVITS 启动器（让主程序能从设置里一键拉起 API server）
GPT_SOVITS_INSTALL_DIR = _config.get("gpt_sovits_install_dir", "")    # GPT-SoVITS 整合包根目录
GPT_SOVITS_GPT_MODEL = _config.get("gpt_sovits_gpt_model", "")        # GPT 权重相对路径，如 GPT_weights_v2/xxx.ckpt
GPT_SOVITS_SOVITS_MODEL = _config.get("gpt_sovits_sovits_model", "")  # SoVITS 权重相对路径，如 SoVITS_weights_v2/xxx.pth

# 桌宠动画
PET_ANIMATION_SPEED = _safe_float("pet_animation_speed", 0.5, min_val=0.1, max_val=5.0)  # 1.0=GIF 原速；0.5=慢 2 倍；2.0=快 2 倍

# 通知（Telegram 推送）
_notify_cfg = _config.get("notify", {}) or {}
NOTIFY_ENABLED: bool = _notify_cfg.get("enabled", False)
NOTIFY_LEVELS: list = _notify_cfg.get("levels", ["error", "action_needed", "done"])
NOTIFY_THROTTLE_SECONDS: int = _notify_cfg.get("throttle_seconds", 10)
TELEGRAM_BOT_TOKEN: str = _notify_cfg.get("telegram_bot_token", "")
TELEGRAM_CHAT_ID: str = _notify_cfg.get("telegram_chat_id", "")

# 遥控（Telegram 远程发送消息给桌面端）
_remote_cfg = _config.get("remote_control", {}) or {}
REMOTE_CONTROL: bool = _remote_cfg.get("enabled", False)
# 遥控安全分级（mode 三选一，默认最安全的 chat_only）：
#   chat_only     —— 禁所有工具，纯对话（默认；不懂/不配时最安全，不会意外泄露）
#   safe_readonly —— 可读代码，但敏感文件黑名单拦截；写工具/命令仍禁
#   unrestricted  —— 不设防，全部工具可用（你完全信任环境时）
_mode = (_remote_cfg.get("mode") or "chat_only").lower()
if _mode not in ("chat_only", "safe_readonly", "unrestricted"):
    _mode = "chat_only"
REMOTE_MODE: str = _mode
# safe_readonly 模式下，用户在内置黑名单之外【追加】的敏感文件名/后缀
REMOTE_BLOCKLIST: list = _remote_cfg.get("readonly_blocklist", []) or []

# MCP Servers 配置（字典，key=server 名，value=启动参数）
MCP_SERVERS: dict = _config.get("mcp_servers", {}) or {}
