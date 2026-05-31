"""Telegram Bot API 推送通知。

用 httpx（非 httpagent）POST sendMessage，发送带 level emoji 的消息。
"""
import httpx

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from .paths import logger


# level → emoji 映射
_LEVEL_EMOJI = {
    "error":         "🔴",
    "action_needed": "🟡",
    "done":          "🟢",
    "info":          "🔵",
}


def push(level: str, title: str, message: str) -> bool:
    """向 Telegram 推一条消息。返回是否成功。

    参数:
        level: error / action_needed / done / info
        title: 标题（一行粗体）
        message: 正文（可多行）
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram 未配置 token/chat_id，跳过推送")
        return False

    emoji = _LEVEL_EMOJI.get(level, "⚪")
    # 纯文本发送，不用 parse_mode：回传内容（目录列表/代码/动作描写）常含未配对的
    # * _ [ ` 等 Markdown 特殊字符，用 parse_mode=Markdown 会 400（can't parse
    # entities）。纯文本最稳，标题靠 emoji + 换行区分。
    text = f"{emoji} {title}\n{message}"

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = httpx.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
            },
            timeout=10,
        )
        if r.status_code == 200:
            logger.info(f"Telegram 推送成功: {title}")
            return True
        else:
            logger.warning(f"Telegram 推送失败 [{r.status_code}]: {r.text[:200]}")
            return False
    except Exception as e:
        logger.warning(f"Telegram 推送异常: {e}")
        return False


def push_long(level: str, title: str, message: str, chunk_size: int = 3500) -> bool:
    """长消息分段发送（Telegram 单条上限 4096，按 chunk_size 留余量切）。

    用于遥控回复回传——AI 回复可能很长，截断到 200 字会丢内容。
    """
    message = message or "(无内容)"
    chunks = [message[i:i + chunk_size] for i in range(0, len(message), chunk_size)]
    ok = True
    for idx, chunk in enumerate(chunks):
        t = title if idx == 0 else f"{title}（续 {idx + 1}/{len(chunks)}）"
        ok = push(level, t, chunk) and ok
    return ok
