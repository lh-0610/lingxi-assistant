"""运行期可变全局状态。

把状态集中到一个模块，避免模块间循环 import（streaming.py / claude_code.py
需要读写 chat_history / stop_flag 等，从这里取就行）。

agent.py 在启动阶段会初始化 llm / llm_with_tools / chat_history。
其它模块**只读为主，按需写**。
"""
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
