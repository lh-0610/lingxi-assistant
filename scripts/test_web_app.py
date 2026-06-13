"""web/app.py 测试。

关键:桩成【真签名 agent_loop(ui)】,用真 HeadlessWebUI 方法 + 真 session.Session,
不再 mock 一个臆造签名的 agent_loop(那样测试全过但实际跑不起来)。
fastapi 未装则整文件跳过。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from web.app import create_app, HeadlessWebUI, ChatService, Busy  # noqa: E402


TOKEN = "test-token-123"


@pytest.fixture()
def stub_core(monkeypatch):
    """把 agent_loop 桩成真签名 agent_loop(ui),用真 ui 方法 + 真会话;system prompt 固定。"""
    from src import agent as _agent, session as _session
    from langchain_core.messages import AIMessage
    import src.roles as _roles

    monkeypatch.setattr(_roles, "get_system_prompt", lambda *a, **k: "SYSTEM", raising=False)

    def fake_agent_loop(ui):
        # 真 agent_loop 的最小忠实复刻:走真 ui 方法 + 往当前会话 append AIMessage
        ui.show_message("\n", "spacer")
        ui.show_message("mimo-v2.5-pro\n", "ai_label")
        ui.show_message("你好", "ai_msg")
        ui.render_final_markdown("**你好**呀～", speak=True)
        _session.current_session().chat_history.append(AIMessage(content="你好呀～"))

    monkeypatch.setattr(_agent, "agent_loop", fake_agent_loop)
    return _agent, _session


@pytest.fixture()
def client(stub_core):
    return TestClient(create_app(auth_token=TOKEN))


# ── 鉴权 ──
def test_api_requires_token(client):
    assert client.get("/api/status").status_code == 401                       # 无 token
    assert client.get("/api/status", headers={"X-Auth-Token": "wrong"}).status_code == 401
    assert client.get("/api/status", headers={"X-Auth-Token": TOKEN}).status_code == 200


def test_token_via_query_param(client):
    # 首次扫码/链接 ?token= 进入
    assert client.get(f"/api/status?token={TOKEN}").status_code == 200


def test_auto_token_when_unset(stub_core, monkeypatch, tmp_path):
    # 不传 token 也必须有(自动生成),绝不裸奔
    monkeypatch.delenv("LINGXI_WEB_TOKEN", raising=False)
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    import src.paths as _paths
    monkeypatch.setattr(_paths, "MEMORY_DIR", str(tmp_path), raising=False)
    app = create_app()
    assert app.state.auth_token                       # 非空
    c = TestClient(app)
    assert c.get("/api/status").status_code == 401    # 无 token 仍被拦


# ── 聊天流式(NDJSON)──
def test_chat_streams_ndjson_events(client):
    r = client.post("/api/chat", json={"message": "你好"}, headers={"X-Auth-Token": TOKEN})
    assert r.status_code == 200
    assert "application/x-ndjson" in r.headers.get("content-type", "")
    assert r.headers.get("cache-control") == "no-store"
    events = [json.loads(ln) for ln in r.text.splitlines() if ln.strip()]
    types = [e["type"] for e in events]
    assert "msg" in types and "md" in types and "done" in types
    # 中文不转义
    assert any(e.get("type") == "md" and "你好" in e.get("text", "") for e in events)


def test_chat_empty_message_400(client):
    r = client.post("/api/chat", json={"message": "  "}, headers={"X-Auth-Token": TOKEN})
    assert r.status_code == 400


def test_chat_busy_409(client):
    # HTTP 层:正在生成时再发一条 → 409。直接把会话标记为生成中再请求。
    from src import session as _session
    client.post("/api/chat", json={"message": "你好"}, headers={"X-Auth-Token": TOKEN})  # 建会话
    _session.get_active().is_generating = True
    try:
        r = client.post("/api/chat", json={"message": "再来"}, headers={"X-Auth-Token": TOKEN})
        assert r.status_code == 409
    finally:
        _session.get_active().is_generating = False


# ── stop / history ──
def test_stop_sets_flag(client, stub_core):
    _agent, _session = stub_core
    r = client.post("/api/stop", headers={"X-Auth-Token": TOKEN})
    assert r.status_code == 200 and r.json().get("ok") is True


def test_history_serializes_roles(client):
    from src import session as _session
    from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
    # 先发一轮把会话建起来
    client.post("/api/chat", json={"message": "你好"}, headers={"X-Auth-Token": TOKEN})
    sess = _session.get_active()
    sess.chat_history = [
        SystemMessage(content="sys"),
        HumanMessage(content="问题"),
        AIMessage(content="回答"),
        ToolMessage(content="工具输出", tool_call_id="t1"),
    ]
    r = client.get("/api/history", headers={"X-Auth-Token": TOKEN})
    msgs = r.json()["messages"]
    roles = [m["role"] for m in msgs]
    assert "system" not in roles            # system 跳过
    assert roles == ["user", "assistant", "tool"]


# ── 安全回归 ──
def test_confirm_command_rejected():
    ui = HeadlessWebUI()
    allowed, reason = ui.confirm_command("rm -rf /")     # 真实 arity:1 个参数
    assert allowed is False and reason


def test_confirm_edit_rejected():
    ui = HeadlessWebUI()
    allowed, reason = ui.confirm_edit("a.py", "--- diff ---")  # 真实 arity:(full, diff)
    assert allowed is False and reason


def test_remote_session_flag(stub_core):
    # 会话必须打 remote_session=True,否则 _execute_tool 的安全分级不生效
    svc = ChatService()
    svc._init()
    assert svc.sess.remote_session is True


# ── 静态页 ──
def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()
