"""运行期可变全局状态。

把状态集中到一个模块，避免模块间循环 import（streaming.py / claude_code.py
需要读写 chat_history / stop_flag 等，从这里取就行）。

agent.py 在启动阶段会初始化 llm / llm_with_tools / chat_history。
其它模块**只读为主，按需写**。
"""
import re

from langchain_core.messages import SystemMessage


# 当前选中的模型在 MODEL_LIST 里的索引
current_model_index = 0

# 是否启用思考模式（reasoning）
reasoning_enabled = True

# 当前 LangChain LLM 实例（启动时由 agent.py 创建）
llm = None
llm_with_tools = None

# 对话历史（启动时由 agent.py 用 SystemMessage 初始化）
chat_history = []

# 当前会话标识
current_session_id = None
current_session_title = None

# 用户停止生成的标志
stop_flag = False

# Token 用量统计（累计 + 上一轮）
session_token_usage = {"input": 0, "output": 0, "total": 0}

# 当前激活的项目根路径；None = 无项目（全局工作区）
# 由侧边栏项目切换器修改，会话列表按这个 filter
current_project = None

# 主 ChatUI 实例引用。tools.py 在 worker 线程里执行命令时，需要通过它弹
# 确认框（必须走 UI 主线程）。None 表示当前是 CLI / 测试环境，无 UI，
# 此时 run_command 会默认放行，不阻塞。
ui_ref = None

# Agent 工作模式：
#   "act"  —— 默认。AI 可以调任何工具直接动手
#   "plan" —— 计划模式。AI 只能调"只读"工具（read/search/list），
#             不能 edit/write/append/run_command/generate_image。
#             目的：让 AI 先调研 + 给出执行方案，用户确认后切回 act 才动手
# 由 ChatUI 顶栏的 Plan/Act 切换按钮修改
agent_mode = "act"

# Telegram 遥控：当前这轮回复是否由远程消息触发（决定推送行为）
remote_session: bool = False

# Telegram 遥控：回复完成后是否自动发 Telegram 通知（可由命令开关）
telegram_stop: bool = False

# run_command 的当前工作目录（None = 用项目根）
# 由纯 cd 命令设置，跨命令留存；新对话 / 切项目时重置为 None
shell_cwd: str | None = None

# 会话历史压缩缓存（滚动压缩避免每轮重复调 LLM）。
# summary: 上次压缩生成的摘要文本；covered_upto: 它已覆盖到 chat_history 的第几条。
compaction = {"summary": "", "covered_upto": 0}

# 当前任务计划（会话级临时状态，不持久化）。由 update_plan 工具维护，
# 每轮注入 system prompt 让模型看到进度，防"做一半就收尾"。
# 每项: {"text": str, "status": "pending"|"in_progress"|"done"}
current_plan: list = []

# 计划状态标记 ↔ 显示符号。放这里（state 无 src 内部依赖）让 tools/roles 都能
# import，避免 tools↔roles 循环 import。
_PLAN_STATUS_MARK = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}

# 解析单行 checklist：可选的 markdown 列表前缀（- / * / + / "1." / "1)"）+ 一个
# checkbox（中括号内允许多/少空格）。group(1)=状态字符，group(2)=步骤文本。
_PLAN_LINE_RE = re.compile(r"^(?:[-*+]\s+|\d+[.)]\s+)?\[\s*([^\]]?)\s*\]\s*(.*)$")

# checkbox 内字符（小写比较）→ 状态。容忍模型写的各种"完成/进行中"变体。
_PLAN_CHAR_STATUS = {
    "": "pending", " ": "pending",
    "x": "done", "✓": "done", "√": "done", "v": "done",
    "~": "in_progress", "-": "in_progress", "/": "in_progress", ">": "in_progress",
}


def parse_plan(plan_text: str) -> list:
    """把多行 checklist 文本解析成 [{'text','status'}, ...]。行首标记决定状态。

    容错：允许行首带 markdown 列表前缀（- / * / 1.）、checkbox 内多/少空格、
    大小写（[X]）、及常见完成/进行中字符（✓ / ~ 等）。没有 checkbox 的行按
    pending 处理、整行作为文本。
    """
    items = []
    for raw in (plan_text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _PLAN_LINE_RE.match(line)
        if m:
            status = _PLAN_CHAR_STATUS.get(m.group(1).lower(), "pending")
            text = m.group(2).strip()
        else:
            status, text = "pending", line
        if text:
            items.append({"text": text, "status": status})
    return items


def render_plan(plan: list) -> str:
    """把 current_plan 渲染回 Markdown checklist 文本。"""
    if not plan:
        return ""
    out = []
    for item in plan:
        mark = _PLAN_STATUS_MARK.get(item.get("status"), "[ ]")
        out.append(f"{mark} {item.get('text', '')}")
    return "\n".join(out)
