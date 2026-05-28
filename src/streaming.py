"""全流式调用 + 工具执行。

- `_extract_usage`: 从累积的 AIMessageChunk 提取 token 用量
- `_stream_with_tools`: 边出 chunk 边显示 + 收集 tool_calls + 解析思考过程
- `_execute_tool`: 执行单个工具调用，把结果回写到 chat_history
"""
import re
import os
import time
import threading as _threading

from langchain_core.messages import ToolMessage

from . import state
from . import debug_log
from .paths import logger
from .models import MODEL_LIST, current_model_supports_vision
from .tools import TOOL_MAP, TOOL_DISPLAY_NAMES, get_tool_map
from .limits import (
    HISTORY_KEEP_RECENT,
    HISTORY_TOKEN_BUDGET,
    STREAM_RETRY_ATTEMPTS,
    TOOL_RESULT_PREVIEW_CHARS,
)
from .images import (
    _normalize_image_blocks_for_current_model,
    _strip_images_in_followup_rounds,
    _strip_images_for_text_only_model,
    _strip_reasoning_for_deepseek,
)


# 这些工具在执行过程中会自己把进度/输出 push 到 UI（边跑边显示），
# `_execute_tool` 完成后不再二次 display 工具结果，避免重复
STREAMING_TOOLS = {"run_command"}


# Plan mode 下允许调用的"只读"工具白名单。AI 若试图调其它工具会被 _execute_tool 拦
PLAN_MODE_READONLY_TOOLS = {
    "read_file", "list_directory", "search_in_file", "search_files",
    "remember", "forget",  # 记笔记不该被 Plan 拦
}


# 会话长度滑动窗口阈值。超过这个估算 token 数就裁掉中间的旧消息，只保留：
#   - 首条 SystemMessage（角色卡 / 项目上下文 / .lingxirules 都在这里）
#   - 最近 KEEP_RECENT 条（通常包含当前 user query 和最近几轮工具调用）
# 默认 80K 是 Anthropic 200K / Gemini 128K / DeepSeek 64K 的保守公共下限——
# 单独某模型 context 更大也无所谓，少发一点就少花点钱。
def _history_has_image_blocks(messages) -> bool:
    for msg in messages or []:
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            continue
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") in ("image", "image_url"):
                return True
    return False


def _stream_chunks_with_retry(llm_with_tools, messages, ui=None):
    """Retry transient stream startup failures before any chunk is displayed."""
    for attempt in range(STREAM_RETRY_ATTEMPTS):
        yielded = False
        try:
            for chunk in llm_with_tools.stream(messages):
                yielded = True
                yield chunk
            return
        except Exception:
            if yielded or state.stop_flag or attempt >= STREAM_RETRY_ATTEMPTS - 1:
                raise
            delay = 2 ** attempt
            logger.warning(f"模型流式请求失败，{delay}s 后重试（{attempt + 1}/{STREAM_RETRY_ATTEMPTS}）", exc_info=True)
            if ui is not None:
                try:
                    ui.show_message(f"\n⚠️ 模型请求失败，{delay}s 后自动重试...\n", "tool_result")
                except Exception:
                    pass
            time.sleep(delay)


def _estimate_tokens(messages) -> int:
    """粗估 token 数，不引外部库（tiktoken）。1 字符 ≈ 0.7 token（中英混排经验值）。
    多模态 image block 估 1000 token / 张，其它非 text block 估 200 token。"""
    total = 0
    for msg in messages:
        content = getattr(msg, "content", "") or ""
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict):
                    bt = blk.get("type")
                    if bt == "text":
                        total += len(blk.get("text", "") or "")
                    elif bt in ("image", "image_url"):
                        total += 1000
                    elif bt == "thinking":
                        total += len(blk.get("thinking", "") or "")
                    else:
                        total += 200
        # tool_calls 里的 args
        tcs = getattr(msg, "tool_calls", None) or []
        for tc in tcs:
            if isinstance(tc, dict):
                total += len(str(tc.get("args", {})))
    return int(total * 0.7)


def _maybe_trim_history(messages, budget=HISTORY_TOKEN_BUDGET, keep_recent=HISTORY_KEEP_RECENT):
    """估算 token 超阈值就裁中段，保留 system + 最近 keep_recent 条。

    返回 (新 messages, dropped_count)。dropped_count > 0 表示真的裁了。
    被裁掉的中段会被替换成一条 SystemMessage 占位 "[已自动裁剪 N 条旧消息]"，
    让 AI 知道历史里有空白，不会因为缺失上下文困惑。
    """
    from langchain_core.messages import SystemMessage as _SM
    est = _estimate_tokens(messages)
    if est <= budget:
        return messages, 0
    if len(messages) <= keep_recent + 1:
        return messages, 0  # 实在裁不动了

    has_system = bool(messages) and isinstance(messages[0], _SM)
    head = messages[:1] if has_system else []
    tail = messages[-keep_recent:]
    # 去重：head 可能跟 tail 头一条重合（极端短历史）
    if head and head[0] in tail:
        tail = [m for m in tail if m is not head[0]]
    dropped = len(messages) - len(head) - len(tail)
    if dropped <= 0:
        return messages, 0
    placeholder = _SM(
        content=f"[历史已自动裁剪：跳过中间 {dropped} 条消息以控制上下文长度。"
        f"如需查阅，请在 UI 上滚动查看完整对话。]"
    )
    return head + [placeholder] + tail, dropped


def _wrap_system_for_cache(messages, fresh_system_text: str, provider: str):
    """生成发送用的 history。第一条 SystemMessage 用 `fresh_system_text` 替换掉
    （让 .lingxirules / 画图按需注入等"最新状态"生效）；对 Anthropic / MiMo 走
    content block + cache_control 形态开启 prompt caching。

    OpenAI 兼容协议（DeepSeek / Qwen 等）的 SDK 期望 content 是字符串，**不能**
    传 content block——它们要么自动 cache（DeepSeek 是），要么不支持 cache（多
    数兼容接口）；这里直接保持纯字符串形态。
    """
    from langchain_core.messages import SystemMessage as _SM
    if not messages:
        return messages
    head = messages[0]
    if not isinstance(head, _SM):
        return messages

    # 判断是否能用 cache_control：内置 anthropic/mimo 直接进；custom 类型要看
    # 用户配的 protocol 是不是 anthropic
    use_anthropic_cache = provider in ("anthropic", "mimo")
    if provider == "custom":
        from .models import MODEL_LIST as _ML, _lookup_custom_model
        model_id = _ML[state.current_model_index][2]
        cm = _lookup_custom_model(model_id) or {}
        use_anthropic_cache = (cm.get("protocol") or "openai").lower() == "anthropic"

    if use_anthropic_cache:
        # Anthropic prompt caching：content 写成 content blocks，给那一块标
        # `cache_control: {"type": "ephemeral"}`。命中缓存后该部分按 ~10% 计费。
        # 注意：完整 prompt 必须超过模型最小缓存阈值（Sonnet 是 1024 token，
        # 一般 system prompt 都够）才会真的进缓存。
        new_head = _SM(content=[
            {
                "type": "text",
                "text": fresh_system_text,
                "cache_control": {"type": "ephemeral"},
            }
        ])
    else:
        # 其它 provider：纯字符串（兼容 OpenAI / Ollama / DeepSeek 等）
        new_head = _SM(content=fresh_system_text)

    return [new_head] + list(messages[1:])


def _llm_endpoint() -> str:
    """从当前 LLM 实例抓出可见的接口地址（给 Debug Inspector 看）。
    Anthropic / OpenAI / Ollama 等都有 base_url 字段；抓不到就返回空字符串。"""
    llm = getattr(state, "llm", None)
    if llm is None:
        return ""
    for attr in ("anthropic_api_url", "openai_api_base", "base_url", "endpoint_url"):
        url = getattr(llm, attr, None)
        if isinstance(url, str) and url:
            return url
    return ""


def _extract_usage(gathered):
    """从累加的 AIMessageChunk 提取 token 用量"""
    usage = {"input": 0, "output": 0, "total": 0}
    if gathered is None:
        return usage

    try:
        # LangChain >= 0.2 的 usage_metadata（Anthropic / OpenAI 均支持）
        um = getattr(gathered, 'usage_metadata', None)
        if um and isinstance(um, dict):
            usage["input"] = um.get("input_tokens", 0) or 0
            usage["output"] = um.get("output_tokens", 0) or 0
            usage["total"] = um.get("total_tokens", 0) or 0
            if usage["total"] == 0 and (usage["input"] or usage["output"]):
                usage["total"] = usage["input"] + usage["output"]
            return usage

        # 回退: response_metadata（OpenAI 兼容协议）
        rm = getattr(gathered, 'response_metadata', None) or {}
        tu = rm.get('token_usage', rm.get('usage', {}))
        if tu and isinstance(tu, dict):
            usage["input"] = tu.get("prompt_tokens", tu.get("input_tokens", 0)) or 0
            usage["output"] = tu.get("completion_tokens", tu.get("output_tokens", 0)) or 0
            usage["total"] = tu.get("total_tokens", 0) or 0
            if usage["total"] == 0 and (usage["input"] or usage["output"]):
                usage["total"] = usage["input"] + usage["output"]
            return usage

        # 最后尝试从 additional_kwargs 提取
        ak = getattr(gathered, 'additional_kwargs', None) or {}
        tu = ak.get('token_usage', ak.get('usage', {}))
        if tu and isinstance(tu, dict):
            usage["input"] = tu.get("prompt_tokens", tu.get("input_tokens", 0)) or 0
            usage["output"] = tu.get("completion_tokens", tu.get("output_tokens", 0)) or 0
            usage["total"] = tu.get("total_tokens", 0) or 0
            if usage["total"] == 0 and (usage["input"] or usage["output"]):
                usage["total"] = usage["input"] + usage["output"]
            return usage

    except Exception as e:
        logger.warning(f"提取 token 用量失败: {e}")
        return usage

    return usage


class _StreamState:
    """一轮流式调用跨 chunk 共享的可变状态。

    把原来散在 _stream_with_tools 里的一堆局部变量收进来，方便 _handle_stream_chunk
    原地改、主循环只管编排。
    """
    __slots__ = ("raw_text", "in_think", "think_started", "think_mode",
                 "think_done", "first_token", "gathered",
                 "tool_call_start", "tool_call_last")

    def __init__(self):
        self.raw_text = ""
        self.in_think = False
        self.think_started = False
        self.think_mode = None  # None / "reasoning" / "tag"
        self.think_done = False
        self.first_token = True
        self.gathered = None
        # 工具调用参数流式生成期的指示器状态（None = 没在生成工具调用）
        self.tool_call_start = None   # 开始生成工具调用的时间戳
        self.tool_call_last = 0.0     # 上次刷新指示器的时间戳（节流用）


def _prepare_stream_history(ui):
    """构造本轮真正发给 LLM 的 history + 建 Debug record。

    依次：归一化图片 → 剥跟随轮图片 / 文本模型图片 / DeepSeek reasoning →
    （文本模型有图时提示一次）→ 按需重渲染 system prompt（画图按需注入 +
    .lingxirules + Anthropic 缓存）→ 滑动窗口裁剪 → 开 Debug record。

    返回 (history_for_send, debug_rec)。
    """
    history_for_send = _normalize_image_blocks_for_current_model(state.chat_history)
    text_only_image_warning = (
        not current_model_supports_vision()
        and _history_has_image_blocks(history_for_send)
    )
    history_for_send = _strip_images_in_followup_rounds(history_for_send)
    history_for_send = _strip_images_for_text_only_model(history_for_send)
    history_for_send = _strip_reasoning_for_deepseek(history_for_send)
    if text_only_image_warning:
        warning_key = (state.current_session_id, state.current_model_index)
        if getattr(state, "_last_text_only_image_warning", None) != warning_key:
            state._last_text_only_image_warning = warning_key
            try:
                ui.show_message(
                    "\n⚠️ 当前模型不支持视觉，历史图片已转为文本占位发送；如需让模型看图，请切换到支持图片的模型。\n",
                    "tool_result",
                )
            except Exception:
                pass

    # ── 按需重渲染 system prompt ──
    # 1. 检测最近几轮里有没有画图意图，没有就不带 PAINTING_GUIDE（省 ~3500 字 token）
    # 2. 同时拿到最新的 .lingxirules / 项目上下文（用户中途改这些文件也立刻生效）
    # 3. 对 Anthropic / MiMo 把 system message 转成 content block + cache_control，
    #    开启 prompt caching（缓存命中后该部分按 ~10% 价计费）
    from .roles import get_system_prompt as _get_system_prompt, _detect_painting_intent
    if history_for_send and history_for_send[0].__class__.__name__ == "SystemMessage":
        need_painting = _detect_painting_intent(history_for_send)
        fresh_system = _get_system_prompt(include_painting=need_painting)
        history_for_send = _wrap_system_for_cache(
            history_for_send, fresh_system, provider=MODEL_LIST[state.current_model_index][1],
        )

    # 滑动窗口：超过预算就裁掉中段。state.chat_history 本身不改，UI 上保留完整历史，
    # 只是本次发给 LLM 的 history_for_send 被压缩了。
    history_for_send, _trimmed = _maybe_trim_history(history_for_send)
    if _trimmed > 0:
        logger.info(f"会话历史超阈值，本轮裁剪 {_trimmed} 条旧消息")
        try:
            ui.show_message(
                f"\n⚠️ 对话历史过长，本轮自动裁掉中间 {_trimmed} 条（保留首条 system 提示 + 最近"
                f" {HISTORY_KEEP_RECENT} 条）。UI 上仍保留完整。\n",
                "tool_result",
            )
        except Exception:
            pass

    # ── Debug Inspector：开始一条 record（即使用户没打开 F12 也照收）──
    # 把首条 SystemMessage 单拿出来当 system_prompt 字段，messages 列表里不再重复
    # （否则 Inspector 上同样的提示词会显示两次）
    _model_name, _provider = MODEL_LIST[state.current_model_index][:2]
    _system_prompt = ""
    _messages_for_record = history_for_send
    if history_for_send and history_for_send[0].__class__.__name__ == "SystemMessage":
        _system_prompt = str(history_for_send[0].content or "")
        _messages_for_record = history_for_send[1:]
    debug_rec = debug_log.make_record(
        model=_model_name,
        provider=_provider,
        endpoint=_llm_endpoint(),
        messages=_messages_for_record,
        tools=list(get_tool_map().keys()),
        system_prompt=_system_prompt,
        max_tokens=getattr(state.llm, "max_tokens", None),
    )
    return history_for_send, debug_rec


def _chunk_has_visible(chunk) -> bool:
    """这个 chunk 有没有可显示的内容（reasoning / text / thinking）。
    用来区分"纯工具调用参数 chunk"和"带正文的 chunk"。"""
    if getattr(chunk, 'additional_kwargs', {}).get('reasoning_content'):
        return True
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return bool(content)
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") in ("text", "thinking"):
                if b.get("text") or b.get("thinking"):
                    return True
    return False


def _current_tool_name(gathered) -> str:
    """从累加结果里取当前正在生成的工具名（拿不到就返回'工具'）。"""
    if gathered is None:
        return "工具"
    tcs = getattr(gathered, "tool_calls", None) or []
    if tcs and tcs[-1].get("name"):
        return tcs[-1]["name"]
    for c in reversed(getattr(gathered, "tool_call_chunks", None) or []):
        if c.get("name"):
            return c["name"]
    return "工具"


def _refresh_tool_call_indicator(st, ui, heartbeat_stop):
    """工具调用参数流式生成期的实时指示器（节流到每秒一次）。

    首次：停掉等待/思考心跳、清掉残留指示器、挂一条 "🔧 正在生成工具调用 X... (0s)"。
    之后：每秒原地刷新计时。收尾在 _stream_with_tools 末尾统一 remove。
    """
    now = time.time()
    if st.tool_call_start is None:
        st.tool_call_start = now
        st.tool_call_last = now
        st.first_token = False
        heartbeat_stop.set()            # 接管指示器，停掉等待/思考心跳
        ui.remove_thinking_indicator()  # 清掉残留的"等待响应/思考中"指示器
        name = _current_tool_name(st.gathered)
        ui.show_message(f"🔧 正在生成工具调用 {name}... (0s)\n", "thinking_indicator")
        return
    if now - st.tool_call_last >= 1.0:
        st.tool_call_last = now
        elapsed = int(now - st.tool_call_start)
        name = _current_tool_name(st.gathered)
        ui.update_thinking_indicator(f"🔧 正在生成工具调用 {name}... ({elapsed}s)\n")


def _handle_stream_chunk(st, chunk, ui, heartbeat_stop, heartbeat_phase):
    """处理单个 chunk：累加 gathered + 解析 reasoning/thinking/正文 + 推送到 UI。

    原地改 st。原 _stream_with_tools 主循环体逐行搬来，`continue` → `return`
    （外层 chunk 循环用；内层 block 循环的 continue 保持不变）。
    """
    # 累加 chunk —— LangChain 自动合并 content 和 tool_call_chunks
    st.gathered = chunk if st.gathered is None else st.gathered + chunk

    # 工具调用参数流式生成期：chunk 带 tool_call_chunks 但没有可显示正文。
    # 这段可能很长（比如生成大文件 write_file 的 content 参数，要几十秒），
    # 期间没有任何可见输出，UI 看着像卡死。挂一个实时指示器。
    if getattr(chunk, "tool_call_chunks", None) and not _chunk_has_visible(chunk):
        _refresh_tool_call_indicator(st, ui, heartbeat_stop)
        return

    # 提取 reasoning_content（思考过程）
    reasoning = getattr(chunk, 'additional_kwargs', {}).get('reasoning_content', '')
    if reasoning:
        if not st.think_started:
            st.think_started = True
            st.in_think = True
            st.think_mode = "reasoning"
            heartbeat_stop.set()
            if st.first_token:
                st.first_token = False
            ui.remove_thinking_indicator()
            ui.show_message("Thinking...\n", "think_header")
        ui.show_message(reasoning, "think_msg")
        return
    elif not chunk.content and st.first_token:
        heartbeat_phase[0] = "thinking"

    # Anthropic 协议（MiMo / Claude Sonnet 等）：content 是 list of content blocks
    if isinstance(chunk.content, list):
        for block in chunk.content:
            if not isinstance(block, dict):
                continue
            btype = block.get('type')
            if btype == 'thinking':
                r = block.get('thinking', '')
                if r:
                    if not st.think_started:
                        st.think_started = True
                        st.in_think = True
                        st.think_mode = "reasoning"
                        heartbeat_stop.set()
                        if st.first_token:
                            st.first_token = False
                        ui.remove_thinking_indicator()
                        ui.show_message("Thinking...\n", "think_header")
                    ui.show_message(r, "think_msg")
            elif btype == 'text':
                t = block.get('text', '')
                if t:
                    if st.first_token:
                        st.first_token = False
                        ui.remove_thinking_indicator()
                    if st.in_think and st.think_started:
                        st.in_think = False
                        st.think_mode = None
                        st.think_done = True
                        heartbeat_stop.set()
                        ui.show_message("", "think_collapse")
                        ui.show_message("\n\n", "spacer")
                    st.raw_text += t
                    ui.show_message(t, "ai_msg")
        return

    # OpenAI 协议：content 是字符串
    token = chunk.content
    if not token:
        return
    st.raw_text += token

    if st.first_token:
        st.first_token = False
        ui.remove_thinking_indicator()

    # reasoning_content 模式下思考结束，切换到正文。
    # 显式 <think>...</think> 标签需要等到 </think> 再折叠，否则正文会被误塞进思考块。
    if st.in_think and st.think_started and st.think_mode == "reasoning":
        st.in_think = False
        st.think_mode = None
        st.think_done = True
        heartbeat_stop.set()
        ui.show_message("", "think_collapse")
        ui.show_message("\n\n", "spacer")

    # 解析 <think> 标签（兼容非 reasoning 模式）
    if "<think>" in st.raw_text and not st.think_started:
        st.think_started = True
        st.in_think = True
        st.think_mode = "tag"
        heartbeat_phase[0] = "thinking"
        ui.show_message("Thinking...\n", "think_header")
        display = token.split("<think>", 1)[-1] if "<think>" in token else st.raw_text.split("<think>", 1)[-1]
        if "</think>" in display:
            before, after = display.split("</think>", 1)
            st.in_think = False
            st.think_mode = None
            st.think_done = True
            heartbeat_stop.set()
            if before:
                ui.show_message(before, "think_msg")
            ui.show_message("", "think_collapse")
            ui.show_message("\n\n", "spacer")
            if after:
                ui.show_message(after, "ai_msg")
            return
    elif "</think>" in token:
        before, after = token.split("</think>", 1)
        st.in_think = False
        st.think_mode = None
        st.think_done = True
        heartbeat_stop.set()
        if before:
            ui.show_message(before, "think_msg")
        ui.show_message("", "think_collapse")
        ui.show_message("\n\n", "spacer")
        if after:
            ui.show_message(after, "ai_msg")
        return
    else:
        display = token
        if not st.in_think and st.think_done:
            heartbeat_stop.set()

    if display:
        if st.in_think:
            ui.show_message(display, "think_msg")
        else:
            ui.show_message(display, "ai_msg")


def _collect_tool_calls(gathered):
    """从累加结果提取合法 tool_calls（args 已由 LangChain 自动 JSON 解析为 dict）。"""
    valid_tool_calls = []
    if gathered is None:
        return valid_tool_calls
    for tc in (gathered.tool_calls or []):
        name = tc.get("name", "")
        if name and name in get_tool_map():
            valid_tool_calls.append({
                "name": name,
                "args": tc.get("args") or {},
                "id": tc.get("id") or name,
            })
    # 兼容 args JSON 解析失败的工具调用：保持原 fail-open 行为（args={}）
    for tc in (getattr(gathered, 'invalid_tool_calls', None) or []):
        name = tc.get("name", "") or ""
        if name and name in get_tool_map():
            valid_tool_calls.append({
                "name": name,
                "args": {},
                "id": tc.get("id") or name,
            })
    return valid_tool_calls


def _extract_thinking(gathered):
    """从累加结果提取思考文本（Anthropic thinking block / reasoning_content），供 Debug record。"""
    _thinking = ""
    try:
        if gathered is not None and isinstance(gathered.content, list):
            _thinking = "\n".join(
                b.get("thinking", "") for b in gathered.content
                if isinstance(b, dict) and b.get("type") == "thinking"
            )
        if not _thinking and gathered is not None:
            _thinking = (getattr(gathered, "additional_kwargs", {}) or {}).get("reasoning_content", "") or ""
    except Exception:
        pass
    return _thinking


def _stream_with_tools(ui):
    """
    全流式调用：实时显示思考过程和回复，同时收集 tool_calls。
    返回 (raw_text, tool_calls, usage, gathered)

    编排：准备 history → 起心跳线程 → 逐 chunk 派发给 _handle_stream_chunk →
    收尾（折叠思考 / 收 tool_calls / 提 usage / finalize Debug record）。
    """
    st = _StreamState()
    stream_start = time.time()

    ui.show_message("等待响应...\n", "thinking_indicator")

    # 心跳线程：持续更新计时，直到收到第一个文本 token
    heartbeat_stop = _threading.Event()
    heartbeat_phase = ["waiting"]  # "waiting" → "thinking"

    def _heartbeat():
        """心跳线程：更新等待/思考计时指示器。UI 无响应时静默退出。"""
        while not heartbeat_stop.is_set():
            try:
                elapsed = int(time.time() - stream_start)
                if heartbeat_phase[0] == "thinking":
                    ui.update_thinking_indicator(f"模型思考中... ({elapsed}s)\n")
                else:
                    ui.update_thinking_indicator(f"等待响应... ({elapsed}s)\n")
            except Exception as e:
                logger.warning(f"心跳线程 UI 更新失败: {e}")
                break
            heartbeat_stop.wait(1)

    hb_thread = _threading.Thread(target=_heartbeat, daemon=True)
    hb_thread.start()

    history_for_send, _debug_rec = _prepare_stream_history(ui)

    try:
        for chunk in _stream_chunks_with_retry(state.llm_with_tools, history_for_send, ui):
            if state.stop_flag:
                break
            _handle_stream_chunk(st, chunk, ui, heartbeat_stop, heartbeat_phase)
    except Exception as _err:
        heartbeat_stop.set()
        # 记录失败：把异常信息也写进 record 再 raise，让 Inspector 能看到错误
        try:
            import traceback as _tb
            debug_log.finalize_record(
                _debug_rec, text=st.raw_text,
                error=f"{type(_err).__name__}: {_err}\n{_tb.format_exc(limit=5)}",
            )
        except Exception:
            pass
        raise

    heartbeat_stop.set()

    if st.first_token:
        ui.remove_thinking_indicator()

    # 收尾移除工具调用指示器（"🔧 正在生成工具调用..."），随后 _execute_tool 会显示实际工具标签
    if st.tool_call_start is not None:
        ui.remove_thinking_indicator()

    # 兜底：思考过但没在流中折叠（例如思考后直接调工具，没有正文）
    if st.think_started and st.in_think:
        ui.show_message("", "think_collapse")
        ui.show_message("\n", "spacer")

    valid_tool_calls = _collect_tool_calls(st.gathered)
    usage = _extract_usage(st.gathered)
    _thinking = _extract_thinking(st.gathered)

    # 正常路径 finalize 一条完整 record（含 reasoning / tool_calls / usage）
    try:
        debug_log.finalize_record(
            _debug_rec, text=st.raw_text,
            tool_calls=valid_tool_calls, usage=usage, thinking=_thinking,
        )
    except Exception:
        pass

    return st.raw_text, valid_tool_calls, usage, st.gathered


def _execute_tool(tc, ui):
    name = tc.get("name", "") if isinstance(tc, dict) else tc["name"]
    args = tc.get("args", {}) if isinstance(tc, dict) else tc["args"]
    call_id = tc.get("id", name) if isinstance(tc, dict) else tc["id"]

    if name not in get_tool_map():
        ui.show_message(f"\n⚠️ 未知工具: {name}\n", "tool_tag")
        state.chat_history.append(ToolMessage(content=f"未知工具: {name}", tool_call_id=call_id))
        logger.warning(f"未知工具: {name}")
        return

    # Plan 模式硬拦截：AI 不听话非要调写工具时，挡住并把拒绝信息回灌给 AI
    if getattr(state, "agent_mode", "act") == "plan" and name not in PLAN_MODE_READONLY_TOOLS:
        ui.show_message(f"\n⛔ Plan 模式拒绝调用 {name}（只允许调研类工具）\n", "tool_tag")
        state.chat_history.append(ToolMessage(
            content=(
                f"已拒绝执行 `{name}`：当前是 **Plan 模式**，只能用只读工具（"
                f"{', '.join(sorted(PLAN_MODE_READONLY_TOOLS))}）。"
                "请先给用户一个完整方案，让用户切回 Act 模式后再实际执行。"
            ),
            tool_call_id=call_id,
        ))
        logger.info(f"Plan 模式拒绝调用 {name}")
        return

    display_name = TOOL_DISPLAY_NAMES.get(name, f"🔧 {name}")

    if name in ("read_file", "write_file", "append_file"):
        detail = args.get("path", "")
    elif name == "list_directory":
        detail = args.get("path", ".")
    elif name == "run_command":
        detail = args.get("command", "")
    elif name == "search_in_file":
        detail = f"{args.get('path', '')} → '{args.get('keyword', '')}'"
    elif name == "generate_image":
        detail = args.get("prompt", "")[:80]
    else:
        detail = str(args)

    ui.show_message(f"\n{display_name}", "tool_tag")
    ui.show_message(f"  {detail}\n", "tool_detail")
    logger.info(f"执行工具: {name}({detail})")

    # ── MCP 工具执行前确认（方案阶段 3.1）──
    if name.startswith("mcp_"):
        _ui = getattr(state, "ui_ref", None)
        if _ui is not None:
            import json as _json
            _display = TOOL_DISPLAY_NAMES.get(name, name)
            _msg = f"将调用 MCP 工具 {_display}，参数: {_json.dumps(args, ensure_ascii=False)}"
            if not _ui.confirm_command(_msg):
                logger.info(f"用户拒绝执行 MCP 工具: {name}")
                state.chat_history.append(ToolMessage(
                    content="已拒绝：用户不允许执行此 MCP 工具。",
                    tool_call_id=call_id,
                ))
                return

    try:
        result = get_tool_map()[name].invoke(args)
    except Exception as e:
        result = f"工具执行失败: {e}"
        logger.error(f"工具 {name} 执行失败: {e}")

    # 流式工具（run_command）执行过程中已经把每行 stdout 实时 push 到 UI 了；
    # 这里若再 push 一次 result，会把所有输出在末尾**重复显示一遍**。
    # 所以对流式工具跳过 UI display；AI 那边仍然拿到完整 result。
    if name not in STREAMING_TOOLS:
        display_result = str(result)
        if len(display_result) > TOOL_RESULT_PREVIEW_CHARS:
            display_result = display_result[:TOOL_RESULT_PREVIEW_CHARS] + "\n... [结果已截断]"
        ui.show_message(f"{display_result}\n", "tool_result")
    logger.info(f"工具结果: {str(result)[:200]}...")

    # generate_image 成功后把图片插到聊天区
    # 工具返回格式可能是:
    #   "已生成图片: D:\...\xxx.png ..."（旧）
    #   "已生成图片 (本机 ComfyUI): D:\...\xxx.png ..."（新）
    #   "已生成图片 (Pollinations 回退): D:\...\xxx.png ..."（新）
    if name == "generate_image" and "已生成图片" in str(result):
        m = re.search(r"已生成图片[^:]*:\s*(.+?\.(?:png|jpg|jpeg|webp|gif))", str(result), re.IGNORECASE)
        if m:
            img_path = m.group(1).strip()
            if os.path.exists(img_path):
                ui.show_message(img_path, "ai_image")
            else:
                logger.warning(f"图片路径不存在: {img_path}")

    # 工具结束后重置 AI 回复跟踪，避免最终 markdown 渲染把工具结果和图片一起覆盖
    ui.show_message("", "reset_ai_reply")

    state.chat_history.append(ToolMessage(content=str(result), tool_call_id=call_id))
