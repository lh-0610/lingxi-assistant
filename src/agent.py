"""主入口 + 启动初始化 + agent_loop 主循环。

这个模块既是 src/ 的"facade"，也持有 agent_loop 主体。

设计要点：
- 全局可变状态（current_model_index / chat_history / stop_flag 等）真身在 state.py
- 通过模块级 `__getattr__` 把读取代理到 state，让 ui.py 继续用 `agent.X` 不报错
- 写入 state 必须用 `state.X = ...`（不要 `agent.X = ...`，那只会污染 agent 模块本身）
"""
import re

from langchain_core.messages import SystemMessage

from . import state as _state
from . import state  # 公开给 ui.py 直接用：ui 里所有"写入"改成 src.state.X = ...
from .paths import logger
from .models import (
    MODEL_LIST,
    check_ollama,
    _create_llm,
    get_model_config_issues,
    current_model_supports_vision,
    get_vision_model_index,
    describe_images_with_vision,
)
from .roles import (
    SYSTEM_PROMPT,
    get_system_prompt as _get_system_prompt,  # 内部用
    get_current_role_name,
    get_current_role_path,
    set_role_card,
    clear_role_card,
    load_saved_role_card,
)
from .memory import (
    save_session,
    load_session,
    list_sessions,
    delete_session,
    reset_history,
    maybe_generate_session_title,
    move_sessions_to_no_project,
    _build_ai_message,
)
from .tools import ALL_TOOLS, build_all_tools
from .streaming import _stream_with_tools, _execute_tool
from .claude_code import claude_code_loop as _claude_code_loop

_BOUND_LLM_CACHE = {}


# ══════════════════════════════════════
# 模块级读代理：保持 `agent.stop_flag` 等读取兼容
# ══════════════════════════════════════
# ui.py 里大量 `agent.current_model_index` / `agent.chat_history` / `agent.stop_flag`
# 通过这个 __getattr__ 自动从 state 模块取最新值，无需到处改 ui.py。
# 但**写入**仍然要写 `state.X = ...`（agent.X = ... 不会影响 state）。
def __getattr__(name):
    if hasattr(_state, name):
        return getattr(_state, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ══════════════════════════════════════
# 模型切换
# ══════════════════════════════════════

def _activate_llm():
    """Reuse LLM and tool-bound LLM objects for the active model."""
    _, mtype, model_id, supports_think = MODEL_LIST[state.current_model_index]
    effective_reasoning = bool(state.reasoning_enabled and supports_think)
    key = (state.current_model_index, mtype, model_id, effective_reasoning)
    if key not in _BOUND_LLM_CACHE:
        llm = _create_llm()
        _BOUND_LLM_CACHE[key] = (llm, llm.bind_tools(build_all_tools()))
    state.llm, state.llm_with_tools = _BOUND_LLM_CACHE[key]


def switch_model(index):
    """切换模型"""
    state.current_model_index = index
    _activate_llm()
    name = MODEL_LIST[index][0]
    logger.info(f"切换模型: {name}")


def set_reasoning(enabled):
    """切换思考模式"""
    state.reasoning_enabled = enabled
    _activate_llm()
    logger.info(f"思考模式: {'开启' if enabled else '关闭'}")


# ══════════════════════════════════════
# 启动初始化
# ══════════════════════════════════════

# 1. 默认模型 + LLM
#    启动默认模型由 config 的 default_model_id 决定（默认 mimo-v2.5-pro）。
#    按 model_id 匹配而非写死 index——BUILTIN 顺序随 config 变，写死 index 不稳。
#    找不到该 model_id（如用户删了它）时退回列表第一个。
def _resolve_default_model_index():
    from .config import DEFAULT_MODEL_ID
    if DEFAULT_MODEL_ID:
        for i, (_, _, mid, _) in enumerate(MODEL_LIST):
            if mid == DEFAULT_MODEL_ID:
                return i
    return 0


state.current_model_index = _resolve_default_model_index()
_activate_llm()

# 1.5 MCP 守护线程（后台启动，不影响 UI 等待）
import threading as _threading


def _init_mcp_bg():
    try:
        from .mcp_client import init_mcp
        init_mcp()
        # 清掉旧的（启动时绑的、不含 MCP 的）bound 缓存，并**立即重新绑定**当前模型。
        # 注意：agent_loop 直接用 state.llm_with_tools、不会自己调 _activate_llm，
        # 所以光 clear 缓存不够——必须主动 _activate_llm() 把 state.llm_with_tools
        # 换成带 MCP 工具的版本，否则模型一直看不到 MCP 工具（除非用户手动切一次模型）。
        _BOUND_LLM_CACHE.clear()
        _activate_llm()
        logger.info("MCP 工具已就绪")
    except Exception as e:
        logger.warning(f"MCP 初始化失败: {e}", exc_info=True)


_threading.Thread(target=_init_mcp_bg, daemon=True).start()

# 2. 对话历史（先用纯 SYSTEM_PROMPT 占位）
state.chat_history = [SystemMessage(content=SYSTEM_PROMPT)]
state.current_session_id = None
state.current_session_title = None

# 3. 启动时恢复角色卡 + 当前项目，合并到系统提示词
load_saved_role_card()
from . import projects as _projects
state.current_project = _projects.get_current()
if isinstance(state.chat_history[0], SystemMessage):
    # 不管有没有角色卡和项目，统一让 get_system_prompt 拼好返回
    state.chat_history[0] = SystemMessage(content=_get_system_prompt())


# ══════════════════════════════════════
# Agent 循环（全流式）
# ══════════════════════════════════════

def agent_loop(ui):
    try:
        mtype = MODEL_LIST[state.current_model_index][1]
        model_name = MODEL_LIST[state.current_model_index][0]

        # Claude Code 模式：直接调 CLI
        if mtype == "claude-code":
            _claude_code_loop(ui)
            return

        # 本地模型需要检测 Ollama 服务
        if mtype == "ollama" and not check_ollama():
            ui.show_message("\n⚠️ 无法连接 Ollama 服务，请先运行 ollama serve\n", "ai_msg")
            from .config import OLLAMA_BASE_URL
            logger.error(f"Ollama 服务不可用: {OLLAMA_BASE_URL}")
            return

        round_i = -1
        # 角色卡存在时用角色名替代模型名
        display_name = get_current_role_name() or model_name
        while True:
            round_i += 1

            if state.stop_flag:
                logger.info("用户停止生成")
                break

            # 只在第一轮显示标签
            if round_i == 0:
                ui.show_message("\n", "spacer")
                ui.show_message(f"{display_name}\n", "ai_label")

            logger.info(f"第 {round_i+1} 轮流式调用...")

            # 全流式调用，实时显示思考过程 + 收集 tool_calls（Ollama 解析错误自动重试）
            retries = 0
            while True:
                try:
                    raw_text, tool_calls, round_usage, gathered = _stream_with_tools(ui)
                    break
                except Exception as stream_err:
                    retries += 1
                    err_msg = str(stream_err)
                    if retries <= 2 and ("XML syntax error" in err_msg or "ResponseError" in err_msg):
                        logger.warning(f"Ollama 解析错误，第 {retries} 次重试: {err_msg[:100]}")
                        ui.show_message(f"\n⚠️ 模型输出格式异常，正在重试({retries}/2)...\n", "tool_result")
                    else:
                        raise

            # 累计本轮 token 用量并通知 UI
            if round_usage and round_usage['total'] > 0:
                state.session_token_usage['input'] += round_usage['input']
                state.session_token_usage['output'] += round_usage['output']
                state.session_token_usage['total'] += round_usage['total']
                ui.show_token_usage(state.session_token_usage.copy(), round_usage)
                logger.info(f"Token 用量 - 输入: {round_usage['input']}, 输出: {round_usage['output']}, 总计: {round_usage['total']}")

            if state.stop_flag:
                # 被中断，保存已有内容（保留 thinking blocks 以便回传）
                clean = re.sub(r"<think>.*?</think>|<thought>.*?</thought>", "", raw_text, flags=re.DOTALL).strip()
                if clean or (gathered is not None and isinstance(gathered.content, list)):
                    state.chat_history.append(_build_ai_message(gathered, clean, []))
                break

            clean_text = re.sub(r"<think>.*?</think>|<thought>.*?</thought>", "", raw_text, flags=re.DOTALL).strip()

            if tool_calls:
                logger.info(f"工具调用: {[tc['name'] for tc in tool_calls]}")
                # 用 _build_ai_message 构造 AIMessage，保留 thinking blocks 供下轮回传
                ai_msg = _build_ai_message(gathered, clean_text, tool_calls)
                state.chat_history.append(ai_msg)
                if clean_text:
                    # 中间轮：只渲染 markdown，不朗读（朗读留给最终回复）
                    ui.render_final_markdown(clean_text, speak=False)

                for tc in tool_calls:
                    if state.stop_flag:
                        break
                    _execute_tool(tc, ui)
                continue
            else:
                # 纯文本回复，流式显示完 → 渲染 Markdown
                if not clean_text and not raw_text:
                    # 流静默结束：服务端 / 代理在思考中切断了连接，没有任何 chunk 到达
                    ui.show_message(
                        "\n⚠️ 连接被中断（服务端或代理在思考期间关闭了连接）。"
                        "请重试，或换一个模型。\n",
                        "tool_result",
                    )
                    logger.warning(
                        f"第 {round_i+1} 轮流结束但未收到任何内容，"
                        f"疑似服务端 idle timeout 中断"
                    )
                    break
                state.chat_history.append(_build_ai_message(gathered, clean_text, []))
                if clean_text:
                    ui.render_final_markdown(clean_text)
                logger.info(f"回复完成: {clean_text[:100]}...")
                break

        # save_session 是本地写、很快，留在主流程；标题生成是一次 LLM 调用（可能几十秒），
        # **绝不能**在这里同步跑——否则它会拖在 finished 信号之前，让 is_generating 一直
        # 为 True、UI 卡在"生成中"点不动。挪到后台线程，完事再发信号刷新侧栏标题。
        try:
            save_session()
        except Exception as save_err:
            logger.error(f"保存会话失败: {save_err}", exc_info=True)

        # Telegram 通知：任务完成——不分端都把【完整】回复发回手机（长则分段不截断）。
        # 走 notify_long（尊重 NOTIFY 开关 / 分级 / 节流，用户可在设置里关 done 通知）。
        try:
            from .notify import notify_long as _notify_long
            _notify_long("done", "灵犀回复", clean_text or "(无文本回复)", "agent_done")
        except Exception:
            pass

        def _gen_title_bg():
            try:
                maybe_generate_session_title()
                bridge = getattr(ui, "bridge", None)
                if bridge is not None:
                    bridge.sessions_refresh.emit()  # 标题出来后刷新侧栏（线程安全）
            except Exception as e:
                logger.error(f"自动生成标题失败: {e}", exc_info=True)

        _threading.Thread(target=_gen_title_bg, daemon=True).start()

    except Exception as e:
        ui.remove_thinking_indicator()
        logger.error(f"agent_loop 异常: {e}", exc_info=True)
        # Telegram 通知：agent 异常
        try:
            from .notify import notify as _notify
            _notify("error", "Agent 异常", str(e)[:300], "agent_error")
        except Exception:
            pass
        # 简化错误信息显示
        err_msg = str(e)
        if "XML syntax error" in err_msg or "ResponseError" in err_msg:
            display_err = "Ollama 模型输出格式异常"
        elif "Connection" in err_msg or "refused" in err_msg:
            display_err = "无法连接 Ollama 服务"
        else:
            display_err = err_msg[:100]
        ui.show_retry(display_err)
