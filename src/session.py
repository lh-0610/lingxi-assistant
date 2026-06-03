"""会话级运行时状态容器 + 当前会话路由。

把原本散在 state.py 的"会话级"全局变量（chat_history / stop_flag / token 统计 /
compaction / plan / shell_cwd / 会话 id·title / remote 标记）收进 Session 对象，
为多会话并发打地基。

路由规则（current_session）：
- worker 线程跑某个会话的 agent_loop 时，会 bind_thread(session) 把自己绑到该会话；
  该线程里所有 state.X 访问都落到这个会话。
- 主线程（UI）/ 未绑定的线程：返回 active session（= 前台正在显示的会话）。

state.py 通过 property 把会话级字段代理到 current_session()，所以现有几十处
state.X / agent.X 读写代码无需改动，自动按"当前线程的当前会话"工作。这就是
"全局当前会话 → 线程当前会话"重构的核心。

注意：model 选择 / agent_mode(Plan·Act) 的会话级化在后续 Phase 接入，P1 它们仍是
state.py 的全局字段（行为与重构前完全等价）。
"""
import threading


# 会话级字段名 → 默认值工厂。state.py 的代理 property 依赖这份清单（单一事实源）。
# 用工厂（callable）而非字面量，避免可变默认值（list/dict）被多个会话共享同一对象。
_SESSION_FIELDS = {
    "chat_history": list,
    "current_session_id": lambda: None,
    "current_session_title": lambda: None,
    "stop_flag": lambda: False,
    "session_token_usage": lambda: {"input": 0, "output": 0, "total": 0},
    "compaction": lambda: {"summary": "", "covered_upto": 0},
    "current_plan": list,
    "shell_cwd": lambda: None,
    "remote_session": lambda: False,
    # streaming.py 用 state._last_text_only_image_warning 记"本会话本模型是否已就
    # 文本模型收到图片提示过一次"，也是会话级。
    "_last_text_only_image_warning": lambda: None,
}


class Session:
    """一个会话的全部会话级运行时状态。

    会话级字段（_SESSION_FIELDS）通过 state.py 的代理被现有代码以 state.X 访问；
    is_generating / thread 是运行态，由 UI / agent 直接拿 Session 对象访问。
    """

    __slots__ = tuple(_SESSION_FIELDS) + ("is_generating", "thread")

    def __init__(self):
        for name, factory in _SESSION_FIELDS.items():
            setattr(self, name, factory())
        # 运行态（不经 state 代理）
        self.is_generating = False
        self.thread = None


# ── 当前会话路由 ──
_thread_local = threading.local()
_active = None
_lock = threading.RLock()
# 运行期打开的会话注册表（id → Session）。P1 先建好，多会话注册在后续 Phase 用。
sessions = {}


def get_active() -> Session:
    """主线程 / 未绑定线程看到的会话（前台显示的那个）。首次访问惰性创建。"""
    global _active
    with _lock:
        if _active is None:
            _active = Session()
        return _active


def set_active(session: "Session") -> None:
    """切换前台显示的会话。"""
    global _active
    with _lock:
        _active = session


def current_session() -> "Session":
    """当前线程的当前会话：worker 线程 → 它 bind 的会话；否则 → active。"""
    s = getattr(_thread_local, "session", None)
    return s if s is not None else get_active()


def bind_thread(session: "Session") -> None:
    """把当前线程绑定到某会话（worker 线程进 agent_loop 时调）。"""
    _thread_local.session = session


def unbind_thread() -> None:
    """解除当前线程的会话绑定（worker 退出时调）。"""
    _thread_local.session = None
