"""Claude Code 模式：通过 subprocess 调用本地 `claude` CLI。

跟其它模型走 langchain stream 不一样——Claude Code 不能多轮，要手动把
之前的对话拼成大 prompt，再让 CLI 一次性吐 stream-json 事件回来解析。
"""
import json as _json
import subprocess
import threading as _threading
import time

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from . import state
from .paths import logger
from .config import CLAUDE_CODE_MODEL
from .memory import save_session, maybe_generate_session_title
from .roles import get_system_prompt, get_current_role_name, get_role_card_content


def claude_code_loop(ui):
    """通过 Claude Code CLI 处理消息"""

    # 构造带历史的 prompt（claude -p 不支持多轮，要手动拼）
    def _msg_text(msg):
        c = msg.content
        if isinstance(c, list):
            texts = [p["text"] for p in c if isinstance(p, dict) and p.get("type") == "text"]
            return texts[0] if texts else ""
        return c

    def _msg_has_images(msg):
        c = msg.content
        if isinstance(c, list):
            return any(isinstance(p, dict) and p.get("type") in ("image_url", "image") for p in c)
        return False

    history_parts = []
    last_user_msg = ""
    for msg in state.chat_history:
        if isinstance(msg, SystemMessage):
            continue
        if isinstance(msg, HumanMessage):
            text = _msg_text(msg)
            history_parts.append(f"[用户]: {text}")
            last_user_msg = text
        elif isinstance(msg, AIMessage) and msg.content:
            history_parts.append(f"[你之前的回复]: {msg.content}")

    if not last_user_msg:
        return

    # 检查最新用户消息是否包含图片
    has_images = False
    for msg in reversed(state.chat_history):
        if isinstance(msg, HumanMessage):
            has_images = _msg_has_images(msg)
            break

    # 如果有多轮历史，把之前的对话作为上下文拼进去
    if len(history_parts) > 1:
        context = "\n\n".join(history_parts[:-1])
        full_prompt = f"以下是我们之前的对话：\n\n{context}\n\n现在请继续回复用户的最新消息：\n\n{last_user_msg}"
    else:
        full_prompt = last_user_msg

    display_name = get_current_role_name() or "Claude Code"
    ui.show_message("\n", "spacer")
    ui.show_message(f"{display_name}\n", "ai_label")
    ui.show_message(f"等待{display_name}回复...\n", "thinking_indicator")

    logger.info(f"Claude Code 调用: {last_user_msg[:100]}")

    # 心跳计时（提前定义，避免 Popen 异常时 NameError）
    heartbeat_stop = _threading.Event()
    heartbeat_started = False
    try:
        cmd = [
            "claude", "-p",
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
            "--verbose",
        ]
        if CLAUDE_CODE_MODEL:
            cmd += ["--model", CLAUDE_CODE_MODEL]
        # 角色卡作为系统提示词
        system_prompt = get_system_prompt()
        if get_role_card_content():
            cmd += ["--system-prompt", system_prompt]
        # 如果消息包含图片，使用 stdin 传递并启用图片支持
        if has_images:
            cmd += ["--stdin-format", "text"]
            stdin_data = full_prompt
        else:
            stdin_data = None
            cmd.append(full_prompt)

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if stdin_data else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        if stdin_data:
            proc.stdin.write(stdin_data)
            proc.stdin.close()

        stream_start = time.time()

        def _heartbeat():
            while not heartbeat_stop.is_set():
                elapsed = int(time.time() - stream_start)
                ui.update_thinking_indicator(f"{display_name}思考中... ({elapsed}s)\n")
                heartbeat_stop.wait(1)

        hb = _threading.Thread(target=_heartbeat, daemon=True)
        hb.start()
        heartbeat_started = True

        # 解析 stream-json 事件
        full_text = ""
        indicator_removed = False
        for line in proc.stdout:
            if state.stop_flag:
                proc.kill()
                break
            line = line.strip()
            if not line:
                continue
            try:
                event = _json.loads(line)
            except _json.JSONDecodeError:
                continue

            etype = event.get("type")

            # 首次事件就移除等待指示
            if not indicator_removed:
                indicator_removed = True
                heartbeat_stop.set()
                ui.remove_thinking_indicator()

            if etype == "assistant":
                # 助手消息（包含文本/工具调用）
                msg = event.get("message", {})
                for block in msg.get("content", []):
                    btype = block.get("type")
                    if btype == "text":
                        text = block.get("text", "")
                        full_text += text
                        ui.show_message(text, "ai_msg")
                    elif btype == "thinking":
                        # 思考内容
                        thinking = block.get("thinking", "")
                        if thinking:
                            ui.show_message(f"{thinking}\n", "think_msg")
                    elif btype == "tool_use":
                        # 工具调用
                        tool_name = block.get("name", "?")
                        tool_input = block.get("input", {})
                        input_preview = _json.dumps(tool_input, ensure_ascii=False)[:80]
                        ui.show_message(f"\n🔧 {tool_name}  {input_preview}\n", "tool_tag")
            elif etype == "user":
                # 工具结果
                msg = event.get("message", {})
                for block in msg.get("content", []):
                    if block.get("type") == "tool_result":
                        result = block.get("content", "")
                        if isinstance(result, list):
                            result = " ".join(r.get("text", "") for r in result if isinstance(r, dict))
                        preview = str(result)[:200]
                        ui.show_message(f"{preview}\n", "tool_result")
            elif etype == "result":
                # 最终结果
                # 提取 Claude Code 返回的 token 用量
                usage_data = event.get('usage', {})
                input_t = usage_data.get('input_tokens', 0) or 0
                output_t = usage_data.get('output_tokens', 0) or 0
                total_t = input_t + output_t
                if total_t > 0:
                    round_usage = {'input': input_t, 'output': output_t, 'total': total_t}
                    state.session_token_usage['input'] += input_t
                    state.session_token_usage['output'] += output_t
                    state.session_token_usage['total'] += total_t
                    ui.show_token_usage(state.session_token_usage.copy(), round_usage)
                    logger.info(f"Token 用量 - 输入: {input_t}, 输出: {output_t}, 总计: {total_t}")

        heartbeat_stop.set()
        if not indicator_removed:
            ui.remove_thinking_indicator()
        proc.wait()

        if proc.returncode != 0 and not state.stop_flag:
            err = proc.stderr.read()
            logger.error(f"Claude Code 错误: {err}")
            ui.show_message(f"\n⚠️ {err}\n", "ai_msg")

        clean_text = full_text.strip()
        if clean_text:
            state.chat_history.append(AIMessage(content=clean_text))
            logger.info(f"Claude Code 回复完成: {clean_text[:100]}...")

        save_session()
        maybe_generate_session_title()

    except FileNotFoundError:
        heartbeat_stop.set()
        ui.remove_thinking_indicator()
        ui.show_message("\n⚠️ 未找到 claude 命令，请确认 Claude Code CLI 已安装\n", "ai_msg")
        logger.error("claude 命令未找到")
    except Exception as e:
        if heartbeat_started:
            heartbeat_stop.set()
        ui.remove_thinking_indicator()
        logger.error(f"Claude Code 异常: {e}", exc_info=True)
        ui.show_retry(str(e)[:100])
