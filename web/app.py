"""灵犀 Web 前端 —— HTTP/JSON + NDJSON 流式聊天。

与桌面 PySide6 共用 src/ 全部核心(agent_loop / 工具 / 记忆 / 多会话),不依赖 Qt。
不侵入 src/:通过 session.Session + state.ui_ref 正规接入。

外部依赖:fastapi, uvicorn(仅本模块用;不进桌面打包)。
"""

import asyncio
import json
import logging
import os
import queue
import secrets
import sys
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _ensure_project_root_on_path() -> None:
    """从任意工作目录启动时,保证项目根在 sys.path,src 包可被 import。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)


_ensure_project_root_on_path()


class Busy(Exception):
    """当前会话正在生成,拒绝并发请求(对应 HTTP 409)。"""


# ── HeadlessWebUI ───────────────────────────────────────────────────────────────
class HeadlessWebUI:
    """无 Qt 的 UI 适配器:把 agent_loop / streaming / tools 的渲染调用转成事件,
    经 asyncio.Queue 交给 HTTP 流式响应消费。

    方法签名与桌面 ChatUI 对 agent 暴露的接口**一一对应**(漏一个 agent_loop 就 AttributeError)。
    确认类一律返回 (False, 理由)——Web 远程没有 worktree 物理隔离,不能放行写工具/命令。
    """

    def __init__(self) -> None:
        self._queue: Optional[asyncio.Queue] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.bridge = None  # streaming/agent 有 getattr(ui, "bridge", None) 的防御读

    def set_queue(self, q: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
        """每个请求开始前注入新队列 + 其所属事件循环(供后台线程安全入队)。"""
        self._queue = q
        self._loop = loop

    def _emit(self, type_: str, **kw: Any) -> None:
        q, loop = self._queue, self._loop
        if q is None or loop is None:
            return
        event = {"type": type_, **kw}
        try:
            # asyncio.Queue 非线程安全;agent 在后台线程,必须经 call_soon_threadsafe 入队
            loop.call_soon_threadsafe(q.put_nowait, event)
        except Exception:
            pass

    # ── 渲染表面(与真核心调用点一一对应,见 grep src/ 的 ui.<method>)──

    def show_message(self, text: str, tag: str = "ai_msg") -> None:
        self._emit("msg", tag=str(tag), text="" if text is None else str(text))

    def render_final_markdown(self, md_text: str, speak: bool = True) -> None:
        # speak 形参必须有:agent.py 中间轮以 speak=False 调用
        self._emit("md", text=str(md_text or ""))

    def update_thinking_indicator(self, text: str) -> None:
        self._emit("msg", tag="thinking_indicator", text=str(text or ""))

    def remove_thinking_indicator(self) -> None:
        self._emit("msg", tag="remove_thinking", text="")

    def show_token_usage(self, total_usage: dict, round_usage: Optional[dict] = None) -> None:
        self._emit("usage", total=total_usage or {}, round=round_usage or {})

    def show_retry(self, error_text: str) -> None:
        self._emit("retry", text=str(error_text or ""))

    def show_plan(self, items: Any) -> None:
        self._emit("plan", items=items if isinstance(items, list) else [])

    # ── 确认类:Web 远程一律拒绝(安全红线;state.ui_ref=None 时 tools 会直接放行写盘)──

    def confirm_command(self, command: str) -> tuple[bool, str]:
        return False, "Web 端 M1 不支持远程执行命令(请在桌面端操作,或保持只读模式)。"

    def confirm_edit(self, full: str, diff_text: str) -> tuple[bool, Optional[str]]:
        return False, "Web 端 M1 不支持远程写文件(请在桌面端操作)。"


# ── ChatService ───────────────────────────────────────────────────────────────
class ChatService:
    """持有**一个常驻会话**(对标 telegram 遥控:同时只跑一个任务),与桌面共用核心。"""

    def __init__(self, project: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self._inited = False
        self._fixed_project = project
        self.ui = HeadlessWebUI()
        self.sess = None

    def _init(self) -> None:
        """惰性初始化常驻会话 + 全局接线(首次请求时,确保 src 环境就绪)。"""
        with self._lock:
            if self._inited:
                return
            from src import state, session as _session, agent as _agent  # noqa: F401
            from src.roles import get_system_prompt
            from langchain_core.messages import SystemMessage

            sess = _session.Session()
            sess.remote_session = True            # ★ 启用 _execute_tool 的遥控安全分级
            if self._fixed_project:
                sess.project = self._fixed_project
                state.current_project = self._fixed_project
            # 像桌面一样用 SystemMessage 起头(agent/streaming 依赖 history[0] 是 system)
            sess.chat_history = [SystemMessage(content=get_system_prompt())]
            _session.register(sess)
            _session.set_active(sess)             # 主线程兜底路由到它
            state.ui_ref = self.ui                # ★ tools 无 UI 时会直接放行写盘,必须设
            self.sess = sess
            self._inited = True

    def stop(self) -> None:
        if self.sess is not None:
            self.sess.stop_flag = True

    def is_generating(self) -> bool:
        return bool(self.sess and self.sess.is_generating)

    def history(self) -> list[dict]:
        """把会话历史序列化成展示列表(system 跳过)。"""
        from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
        out = []
        for m in list(self.sess.chat_history if self.sess else []):
            if isinstance(m, HumanMessage):
                c = m.content
                if isinstance(c, list):
                    texts = [p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text"]
                    c = texts[0] if texts else "[图片]"
                out.append({"role": "user", "text": c})
            elif isinstance(m, AIMessage):
                c = m.content
                if isinstance(c, list):
                    c = "".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
                tools_ = [tc.get("name") for tc in (getattr(m, "tool_calls", None) or []) if isinstance(tc, dict)]
                out.append({"role": "assistant", "text": c, "tools": tools_})
            elif isinstance(m, ToolMessage):
                out.append({"role": "tool", "text": str(m.content or "")[:300]})
        return out

    def start(self, message: str) -> asyncio.Queue:
        """追加用户消息并在后台线程跑 agent_loop;返回本轮事件队列。"""
        self._init()
        if self.is_generating():
            raise Busy()

        from src import session as _session, agent as _agent
        from langchain_core.messages import HumanMessage

        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        self.ui.set_queue(q, loop)

        self.sess.chat_history.append(HumanMessage(content=message))
        self.sess.stop_flag = False
        self.sess.is_generating = True
        sess = self.sess
        ui = self.ui

        def _worker() -> None:
            _session.bind_thread(sess)
            try:
                _agent.agent_loop(ui)            # 收尾自带 save_session + 标题生成
            except Exception as exc:             # noqa: BLE001
                logger.error("agent_loop 异常: %s", exc, exc_info=True)
                ui._emit("error", message=f"{type(exc).__name__}: {exc}")
            finally:
                sess.is_generating = False
                _session.unbind_thread()
                ui._emit("done")

        threading.Thread(target=_worker, name="web-agent", daemon=True).start()
        return q


# ── FastAPI 工厂 ──────────────────────────────────────────────────────────────
def _resolve_token(explicit: Optional[str]) -> tuple[str, bool]:
    """解析鉴权 token。优先级:显式 > 环境变量 > config.web.token > 自动生成并持久化。

    返回 (token, generated)。token 始终非空(默认安全:不允许裸奔)。
    """
    tok = explicit or os.environ.get("LINGXI_WEB_TOKEN") or os.environ.get("WEB_AUTH_TOKEN")
    if tok:
        return tok, False
    try:
        from src import config as _cfg
        tok = getattr(_cfg, "WEB_AUTH_TOKEN", None)
    except Exception:
        tok = None
    if tok:
        return tok, False
    # 自动生成并持久化(下次启动复用同一个,链接不变)
    try:
        from src.paths import MEMORY_DIR
        path = os.path.join(MEMORY_DIR, "web_token.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                saved = json.load(f).get("token")
            if saved:
                return saved, False
        tok = secrets.token_urlsafe(24)
        os.makedirs(MEMORY_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"token": tok}, f)
        return tok, True
    except Exception:
        return secrets.token_urlsafe(24), True


def create_app(*, project: Optional[str] = None, auth_token: Optional[str] = None) -> Any:
    """创建 FastAPI 应用。鉴权 token 必有(默认安全),通过 app.state.auth_token 暴露给 serve.py 打印。"""
    try:
        import fastapi  # noqa: F401
        from fastapi import FastAPI, Request, HTTPException
        from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, StreamingResponse
    except ImportError as e:
        raise ImportError(
            "Web 前端需要额外依赖:\n  pip install fastapi uvicorn\n"
            f"缺失模块: {getattr(e, 'name', e)}"
        ) from e

    token, generated = _resolve_token(auth_token)

    app = FastAPI(title="灵犀 Web")
    app.state.auth_token = token
    app.state.token_generated = generated

    svc = ChatService(project=project)
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

    def _check(req: "Request") -> None:
        # header X-Auth-Token / Authorization: Bearer,或 query ?token=(首次扫码/链接进入)
        supplied = (
            req.headers.get("X-Auth-Token")
            or (req.headers.get("Authorization", "")[7:] if req.headers.get("Authorization", "").startswith("Bearer ") else "")
            or req.query_params.get("token", "")
        )
        if not secrets.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="未授权")

    # ── 静态资源(不鉴权;页面本身无数据)──
    @app.get("/", response_class=HTMLResponse)
    async def _index():
        return FileResponse(os.path.join(static_dir, "index.html"), media_type="text/html")

    @app.get("/manifest.json")
    async def _manifest():
        return FileResponse(os.path.join(static_dir, "manifest.json"), media_type="application/json")

    @app.get("/icon-192.png")
    async def _icon192():
        return FileResponse(os.path.join(static_dir, "icon-192.png"), media_type="image/png")

    @app.get("/icon-512.png")
    async def _icon512():
        return FileResponse(os.path.join(static_dir, "icon-512.png"), media_type="image/png")

    # ── API ──
    @app.get("/api/status")
    async def _status(request: Request):
        _check(request)
        try:
            from src import agent as _agent
            models = [m[0] for m in getattr(_agent, "MODEL_LIST", [])]
            idx = getattr(_agent, "current_model_index", 0)
            model = models[idx] if 0 <= idx < len(models) else ""
        except Exception:
            models, idx, model = [], 0, ""
        try:
            from src.config import REMOTE_MODE
        except Exception:
            REMOTE_MODE = "chat_only"
        return {
            "generating": svc.is_generating(),
            "model": model,
            "model_index": idx,
            "models": models,
            "project": project,
            "tool_mode": REMOTE_MODE,
        }

    @app.post("/api/chat")
    async def _chat(request: Request):
        _check(request)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        message = (body.get("message") or "").strip()
        if not message:
            return JSONResponse({"error": "empty message"}, status_code=400)
        try:
            q = svc.start(message)
        except Busy:
            return JSONResponse({"error": "正在生成中,请稍候"}, status_code=409)

        async def _ndjson():
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield json.dumps({"type": "ping"}, ensure_ascii=False) + "\n"
                    continue
                yield json.dumps(event, ensure_ascii=False) + "\n"
                if event.get("type") == "done":
                    break

        return StreamingResponse(
            _ndjson(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/stop")
    async def _stop(request: Request):
        _check(request)
        svc.stop()
        return {"ok": True}

    @app.get("/api/history")
    async def _history(request: Request):
        _check(request)
        return {"messages": svc.history()}

    return app
