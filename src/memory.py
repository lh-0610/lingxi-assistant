"""对话历史 JSON 序列化 + 会话管理。

- `_msg_to_dict` / `_dict_to_msg`：LangChain Message ↔ JSON
- `save_session` / `load_session` / `list_sessions` / `delete_session`：会话 CRUD
- `maybe_generate_session_title`：第一轮结束后用 LLM 生成短标题
- `reset_history`：清空当前对话开新会话
- `_build_ai_message`：从 stream 累积块构造 AIMessage（保留 thinking blocks）
"""
import re
import os
import json
import threading
from datetime import datetime

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage

from . import state
from .paths import MEMORY_DIR, MEMORY_INDEX, logger
from .roles import get_system_prompt
from .limits import SESSION_HISTORY_LIMIT


# 串行化 chat_memory/ 下所有文件的读-改-写。
# 用 RLock 是因为同一线程内 save_session() 已经持锁还会再调 _update_index()，
# 普通 Lock 会自死锁。
_LOCK = threading.RLock()


def _ensure_memory_dir():
    with _LOCK:
        os.makedirs(MEMORY_DIR, exist_ok=True)
        if not os.path.exists(MEMORY_INDEX):
            with open(MEMORY_INDEX, "w", encoding="utf-8") as f:
                json.dump([], f)


def _msg_to_dict(msg):
    if msg is None:
        return {"type": "Unknown", "content": ""}
    d = {"type": msg.__class__.__name__}
    # content 可能是 str 或 list（含 thinking blocks 等），直接保留原结构
    d["content"] = msg.content or ""
    if isinstance(msg, AIMessage) and msg.tool_calls:
        d["tool_calls"] = msg.tool_calls
    if isinstance(msg, AIMessage):
        ak = getattr(msg, 'additional_kwargs', None) or {}
        if ak.get('reasoning_content'):
            d["reasoning_content"] = ak['reasoning_content']
    if isinstance(msg, ToolMessage):
        d["tool_call_id"] = msg.tool_call_id
    return d


def _dict_to_msg(d):
    t = d["type"]
    if t == "SystemMessage":
        return SystemMessage(content=d["content"])
    elif t == "HumanMessage":
        return HumanMessage(content=d["content"])
    elif t == "AIMessage":
        ak = {}
        if "reasoning_content" in d:
            ak["reasoning_content"] = d["reasoning_content"]
        msg = AIMessage(
            content=d["content"],
            tool_calls=d.get("tool_calls", []),
            additional_kwargs=ak,
        )
        return msg
    elif t == "ToolMessage":
        return ToolMessage(content=d["content"], tool_call_id=d.get("tool_call_id", ""))
    return HumanMessage(content=d["content"])


def _build_ai_message(gathered, clean_text, tool_calls):
    """从 gathered AIMessageChunk 构造写入 chat_history 的 AIMessage。
    保留 thinking content blocks 和 reasoning_content，让下一轮 API 调用
    能把它们回传给服务端（MiMo / DeepSeek 等要求回传 thinking 上下文）。
    """
    ak = dict(getattr(gathered, 'additional_kwargs', {}) or {}) if gathered else {}

    # content blocks：保留 thinking 块 + 去掉空块
    content_blocks = []
    if gathered is not None and isinstance(gathered.content, list):
        for block in gathered.content:
            if not isinstance(block, dict):
                continue
            btype = block.get('type')
            if btype == 'thinking' and block.get('thinking'):
                content_blocks.append(block)
            elif btype == 'text' and block.get('text'):
                content_blocks.append(block)

    if content_blocks:
        # 有 list 形式的 content blocks（Anthropic 协议），直接用
        return AIMessage(
            content=content_blocks,
            tool_calls=tool_calls or [],
            additional_kwargs=ak,
        )
    else:
        return AIMessage(
            content=clean_text,
            tool_calls=tool_calls or [],
            additional_kwargs=ak,
        )


def save_session():
    _ensure_memory_dir()
    if len(state.chat_history) <= 1:
        return

    if not state.current_session_id:
        state.current_session_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    title = state.current_session_title or "新对话"
    for msg in state.chat_history:
        if isinstance(msg, HumanMessage):
            c = msg.content
            if isinstance(c, list):
                # 多模态消息，取第一个 text 部分
                texts = [p["text"] for p in c if isinstance(p, dict) and p.get("type") == "text"]
                c = texts[0] if texts else "[图片]"
            if not state.current_session_title:
                title = c[:30].replace("\n", " ")
            break

    session_file = os.path.join(MEMORY_DIR, f"{state.current_session_id}.json")
    data = {
        "id": state.current_session_id,
        "title": title,
        "updated": datetime.now().isoformat(),
        "project": state.current_project,   # 当前所属项目（None = 无项目/全局）
        "messages": [_msg_to_dict(m) for m in state.chat_history],
    }
    # 会话文件 + 索引文件作为一个原子事务，避免中途被 delete_session 删掉
    with _LOCK:
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        _update_index(state.current_session_id, title, state.current_project)
    logger.info(f"会话已保存: {state.current_session_id} - {title}")


def _first_user_text():
    """返回当前会话第一条用户文本，用于生成标题。"""
    for msg in state.chat_history:
        if isinstance(msg, HumanMessage):
            c = msg.content
            if isinstance(c, list):
                texts = [
                    p.get("text", "")
                    for p in c
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                return (texts[0] if texts else "[图片]").strip()
            return str(c).strip()
    return ""


def _extract_text_content(resp):
    """从 LLM 响应里取纯文本。

    OpenAI 协议：resp.content 是字符串，直接用。
    Anthropic / MiMo（尤其开思考时）：resp.content 是 content block 列表
    （thinking 块 + text 块），要拼接其中的 text 块，否则把 list 丢给
    re.sub 会 TypeError、退回丑截断。
    """
    content = getattr(resp, "content", None)
    if content is None:
        return str(resp)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                # 只取正文 text，跳过 thinking / 其它块
                if block.get("type") == "text" and block.get("text"):
                    parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content)


def _sanitize_title(title):
    title = re.sub(r"[\r\n\t]+", " ", title or "").strip()
    title = title.strip("「」『』《》\"'`*#：:，,。. ")
    if not title:
        return ""
    return title[:16]


def _write_session_title(session_id, title):
    """更新当前会话文件中的 title 字段。"""
    session_file = os.path.join(MEMORY_DIR, f"{session_id}.json")
    with _LOCK:
        if not os.path.exists(session_file):
            return
        with open(session_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["title"] = title
        data["updated"] = datetime.now().isoformat()
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def maybe_generate_session_title():
    """新会话首轮结束后自动生成短标题。失败时保留首句标题。"""
    if state.current_session_title or not state.current_session_id:
        return

    first_text = _first_user_text()
    if not first_text:
        return

    # 太短的问候直接作为标题，不额外花一次模型调用。
    if len(first_text) <= 8:
        title = _sanitize_title(first_text)
    else:
        try:
            # 延迟 import 避免循环依赖（models.py 不依赖 memory）
            from .models import _create_llm
            # 标题任务强制关思考：1) 又快又省 token；2) 开思考时 Anthropic/MiMo
            # 的 resp.content 是 content block 列表，会让下面的提取出错退回截断
            title_llm = _create_llm(reasoning=False)
            prompt = (
                "请为下面这段对话生成一个简短中文标题。"
                "要求：不超过10个汉字，不要标点，不要解释，只输出标题。\n\n"
                f"用户：{first_text[:500]}"
            )
            resp = title_llm.invoke([
                SystemMessage(content="你只负责生成聊天标题。"),
                HumanMessage(content=prompt),
            ])
            title = _sanitize_title(_extract_text_content(resp))
        except Exception as e:
            logger.warning(f"自动生成标题失败: {e}，使用首句截断作为标题")
            title = ""

    # 降级方案：LLM 生成失败时，使用首句截断作为标题
    if not title:
        title = _sanitize_title(first_text)
        if not title:
            title = "新对话"

    state.current_session_title = title
    _ensure_memory_dir()
    _update_index(state.current_session_id, title, state.current_project)
    _write_session_title(state.current_session_id, title)
    logger.info(f"自动标题已生成: {state.current_session_id} - {title}")


def _update_index(session_id, title, project=None):
    with _LOCK:
        with open(MEMORY_INDEX, "r", encoding="utf-8") as f:
            index = json.load(f)

        for item in index:
            if item["id"] == session_id:
                item["title"] = title
                item["updated"] = datetime.now().isoformat()
                item["project"] = project
                break
        else:
            index.insert(0, {
                "id": session_id,
                "title": title,
                "updated": datetime.now().isoformat(),
                "project": project,
            })

        kept_ids = {item["id"] for item in index[:SESSION_HISTORY_LIMIT]}
        dropped_ids = [item["id"] for item in index[SESSION_HISTORY_LIMIT:]]
        index = index[:SESSION_HISTORY_LIMIT]
        with open(MEMORY_INDEX, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
        for old_id in dropped_ids:
            if old_id in kept_ids or old_id == state.current_session_id:
                continue
            old_file = os.path.join(MEMORY_DIR, f"{old_id}.json")
            try:
                if os.path.exists(old_file):
                    os.remove(old_file)
            except Exception as e:
                logger.warning(f"删除旧会话文件失败 {old_id}: {e}")


def load_session(session_id):
    state.session_token_usage = {"input": 0, "output": 0, "total": 0}
    session_file = os.path.join(MEMORY_DIR, f"{session_id}.json")
    with _LOCK:
        if not os.path.exists(session_file):
            return False
        with open(session_file, "r", encoding="utf-8") as f:
            data = json.load(f)

    state.chat_history.clear()
    for d in data["messages"]:
        state.chat_history.append(_dict_to_msg(d))

    state.current_session_id = session_id
    state.current_session_title = data.get("title")
    logger.info(f"会话已加载: {session_id}")
    return True


def list_sessions(project_filter="__current__"):
    """读取索引并按项目过滤。
    project_filter:
      - "__current__"（默认）：按 state.current_project 过滤
      - None：仅返回无项目的会话
      - "<path>"：返回该项目的会话
      - "__all__"：不过滤，返回全部
    """
    _ensure_memory_dir()
    with _LOCK:
        if not os.path.exists(MEMORY_INDEX):
            return []
        with open(MEMORY_INDEX, "r", encoding="utf-8") as f:
            index = json.load(f)

    if project_filter == "__all__":
        return index
    if project_filter == "__current__":
        project_filter = state.current_project
    # None 和具体路径都用同样的相等判断（旧会话没 project 字段 → 默认 None → 归"无项目"）
    return [s for s in index if s.get("project") == project_filter]


def move_sessions_to_no_project(old_path):
    """把所有 project==old_path 的会话改成"无项目（全局）"。
    用于：用户从列表移除一个项目时，把该项目下的历史会话也一起转到无项目，
    避免它们以"游离项目"的形式继续显示在侧栏。

    同时改 index.json 里的索引项 和 每个 <id>.json 里的 project 字段——
    后者保证下次重启或重新载入会话时也是 None。
    """
    if not old_path:
        return 0
    moved = 0
    with _LOCK:
        if not os.path.exists(MEMORY_INDEX):
            return 0
        with open(MEMORY_INDEX, "r", encoding="utf-8") as f:
            index = json.load(f)

        affected_ids = []
        for item in index:
            if item.get("project") == old_path:
                item["project"] = None
                affected_ids.append(item["id"])
                moved += 1

        if moved:
            with open(MEMORY_INDEX, "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False, indent=2)

            # 同步改每个会话文件里的 project 字段
            for sid in affected_ids:
                session_file = os.path.join(MEMORY_DIR, f"{sid}.json")
                if not os.path.exists(session_file):
                    continue
                try:
                    with open(session_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    data["project"] = None
                    with open(session_file, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                except Exception as e:
                    logger.warning(f"改写会话 {sid} project 字段失败: {e}")

    if moved:
        logger.info(f"已把 {moved} 个会话从 {old_path} 转到无项目")
    return moved


def delete_session(session_id):
    session_file = os.path.join(MEMORY_DIR, f"{session_id}.json")
    with _LOCK:
        if os.path.exists(session_file):
            os.remove(session_file)

        if os.path.exists(MEMORY_INDEX):
            with open(MEMORY_INDEX, "r", encoding="utf-8") as f:
                index = json.load(f)
            index = [i for i in index if i["id"] != session_id]
            with open(MEMORY_INDEX, "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False, indent=2)
    logger.info(f"会话已删除: {session_id}")


def reset_history():
    state.session_token_usage = {"input": 0, "output": 0, "total": 0}
    save_session()
    state.chat_history.clear()
    state.chat_history.append(SystemMessage(content=get_system_prompt()))
    state.current_session_id = None
    state.current_session_title = None
