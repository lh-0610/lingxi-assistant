"""web/app.py 测试(多用户版)。

关键:桩成【真签名 agent_loop(ui)】,用真 HeadlessWebUI 方法 + 真 session.Session,
不再 mock 一个臆造签名的 agent_loop(那样测试全过但实际跑不起来)。
鉴权改为注册/登录拿 token;数据根隔离到 tmp(APP_DIR 指 tmp,每用户子目录)。
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

from web.app import create_app, HeadlessWebUI, ChatService  # noqa: E402


USER = "tester"
PASS = "pw1234"


@pytest.fixture()
def stub_core(monkeypatch):
    """把 agent_loop 桩成真签名 agent_loop(ui),用真 ui 方法 + 真会话;system prompt 固定。"""
    from src import agent as _agent, session as _session
    from langchain_core.messages import AIMessage
    import src.roles as _roles

    monkeypatch.setattr(_roles, "get_system_prompt", lambda *a, **k: "SYSTEM", raising=False)

    def fake_agent_loop(ui):
        ui.show_message("\n", "spacer")
        ui.show_message("mimo-v2.5-pro\n", "ai_label")
        ui.show_message("你好", "ai_msg")
        ui.render_final_markdown("**你好**呀～", speak=True)
        _session.current_session().chat_history.append(AIMessage(content="你好呀～"))

    monkeypatch.setattr(_agent, "agent_loop", fake_agent_loop)
    return _agent, _session


@pytest.fixture()
def app_ctx(stub_core, monkeypatch, tmp_path):
    """数据根隔离到 tmp(APP_DIR→tmp),建 app,注册一个测试用户。

    返回 (client, app, token, username);client 默认带该用户 token 头。
    """
    import src.paths as _paths
    monkeypatch.setattr(_paths, "APP_DIR", str(tmp_path), raising=False)
    app = create_app()
    c = TestClient(app)
    token = c.post("/api/register", json={"username": USER, "password": PASS}).json()["token"]
    c.headers.update({"X-Auth-Token": token})   # 之后所有请求默认带 token
    return c, app, token, USER


@pytest.fixture()
def client(app_ctx):
    return app_ctx[0]


def _sess(app_ctx):
    """该测试用户的常驻会话对象。"""
    c, app, token, user = app_ctx
    svc = app.state.svc_for(user)
    svc._init()
    return svc.sess


# ── 账号:注册 / 登录 / 登出 ──
def test_register_login_logout(app_ctx):
    c, app, token, user = app_ctx
    # 重复注册 → 400
    assert c.post("/api/register", json={"username": USER, "password": "x"}).status_code == 400
    # 错误密码 → 401
    assert c.post("/api/login", json={"username": USER, "password": "nope"}).status_code == 401
    # 正确登录 → 拿到新 token
    r = c.post("/api/login", json={"username": USER, "password": PASS})
    assert r.status_code == 200 and r.json()["token"]
    # 非法用户名 → 400
    assert c.post("/api/register", json={"username": "a/b", "password": "1234"}).status_code == 400
    # 短密码 → 400
    assert c.post("/api/register", json={"username": "u2", "password": "1"}).status_code == 400


def test_me_and_auth_required(app_ctx):
    c, app, token, user = app_ctx
    assert c.get("/api/me").json()["username"] == USER
    # 无 token → 401
    raw = TestClient(app)
    assert raw.get("/api/me").status_code == 401
    assert raw.get("/api/status", headers={"X-Auth-Token": "wrong"}).status_code == 401


def test_token_via_query_param(app_ctx):
    c, app, token, user = app_ctx
    raw = TestClient(app)
    assert raw.get(f"/api/status?token={token}").status_code == 200


# ── 数据隔离 ──
def test_user_isolation(app_ctx):
    """两个用户各自独立会话/历史,互不可见。"""
    c, app, token_a, user_a = app_ctx
    # 注册第二个用户
    raw = TestClient(app)
    tb = raw.post("/api/register", json={"username": "other", "password": "pw1234"}).json()["token"]
    raw.headers.update({"X-Auth-Token": tb})

    c.post("/api/chat", json={"message": "甲的消息"})
    raw.post("/api/chat", json={"message": "乙的消息"})

    ha = c.get("/api/history").json()["messages"]
    hb = raw.get("/api/history").json()["messages"]
    a_texts = " ".join(m.get("text", "") for m in ha)
    b_texts = " ".join(m.get("text", "") for m in hb)
    assert "甲的消息" in a_texts and "乙的消息" not in a_texts
    assert "乙的消息" in b_texts and "甲的消息" not in b_texts


# ── 聊天流式(NDJSON)──
def test_chat_streams_ndjson_events(client):
    r = client.post("/api/chat", json={"message": "你好"})
    assert r.status_code == 200
    assert "application/x-ndjson" in r.headers.get("content-type", "")
    assert r.headers.get("cache-control") == "no-store"
    events = [json.loads(ln) for ln in r.text.splitlines() if ln.strip()]
    types = [e["type"] for e in events]
    assert "msg" in types and "md" in types and "done" in types
    assert any(e.get("type") == "md" and "你好" in e.get("text", "") for e in events)


def test_chat_empty_message_400(client):
    assert client.post("/api/chat", json={"message": "  "}).status_code == 400


def test_chat_busy_409(app_ctx):
    c, app, token, user = app_ctx
    c.post("/api/chat", json={"message": "你好"})   # 建会话
    _sess(app_ctx).is_generating = True
    try:
        assert c.post("/api/chat", json={"message": "再来"}).status_code == 409
    finally:
        _sess(app_ctx).is_generating = False


# ── stop / history ──
def test_stop_sets_flag(client):
    r = client.post("/api/stop")
    assert r.status_code == 200 and r.json().get("ok") is True


def test_history_serializes_roles(app_ctx):
    c, app, token, user = app_ctx
    from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
    c.post("/api/chat", json={"message": "你好"})       # 建会话
    sess = _sess(app_ctx)
    sess.chat_history = [
        SystemMessage(content="sys"),
        HumanMessage(content="问题"),
        AIMessage(content="回答"),
        ToolMessage(content="工具输出", tool_call_id="t1"),
    ]
    msgs = c.get("/api/history").json()["messages"]
    roles = [m["role"] for m in msgs]
    assert "system" not in roles
    assert roles == ["user", "assistant", "tool"]


def test_new_chat_resets(app_ctx):
    c, app, token, user = app_ctx
    from langchain_core.messages import HumanMessage
    c.post("/api/chat", json={"message": "你好"})        # 建会话
    _sess(app_ctx).chat_history.append(HumanMessage(content="多一条"))
    r = c.post("/api/new")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert c.get("/api/history").json()["messages"] == []


# ── 安全回归 ──
def test_confirm_command_rejected():
    allowed, reason = HeadlessWebUI().confirm_command("rm -rf /")
    assert allowed is False and reason


def test_confirm_edit_rejected():
    allowed, reason = HeadlessWebUI().confirm_edit("a.py", "--- diff ---")
    assert allowed is False and reason


def test_remote_session_flag(stub_core, tmp_path):
    svc = ChatService(data_dir=str(tmp_path / "u"))
    svc._init()
    assert svc.sess.remote_session is True


# ── 模型选择 ──
def test_model_switch(client):
    from src import agent
    target = 2 if len(agent.MODEL_LIST) > 2 else 0
    r = client.post("/api/model", json={"index": target})
    assert r.status_code == 200
    assert r.json()["model"] == agent.MODEL_LIST[target][0]
    s = client.get("/api/status").json()
    assert s["model_index"] == target
    assert client.post("/api/model", json={"index": 99999}).status_code == 400


def test_fixed_model_arg(stub_core, monkeypatch, tmp_path):
    from src import agent
    if len(agent.MODEL_LIST) < 2:
        pytest.skip("模型数不足")
    import src.paths as _paths
    monkeypatch.setattr(_paths, "APP_DIR", str(tmp_path), raising=False)
    name = agent.MODEL_LIST[1][0]
    c = TestClient(create_app(model=name))
    tok = c.post("/api/register", json={"username": "fm", "password": "pw1234"}).json()["token"]
    c.headers.update({"X-Auth-Token": tok})
    s = c.get("/api/status").json()
    assert s["model"] == name and s["model_index"] == 1


def test_resolve_model_index():
    from web.app import _resolve_model_index
    ml = [("Claude Code",), ("mimo-v2.5-pro",), ("deepseek-v4-pro",)]
    assert _resolve_model_index("mimo-v2.5-pro", ml) == 1
    assert _resolve_model_index("deepseek", ml) == 2
    assert _resolve_model_index(2, ml) == 2
    assert _resolve_model_index("不存在的", ml) is None
    assert _resolve_model_index(None, ml) is None


# ── 网页端系统提示:只做联网检索 + 开关指令 ──
def test_web_system_prompt_search_only(isolated_memory):
    from src import session as _session, roles
    s = _session.Session()
    s.remote_session = True                  # 模拟网页端会话
    _session.bind_thread(s)
    try:
        sp_on = roles.get_system_prompt(web_search=True)
        sp_off = roles.get_system_prompt(web_search=False)
        # 桌面端(非远程)对照
        d = _session.Session()
        _session.bind_thread(d)
        sp_desktop = roles.get_system_prompt()
    finally:
        _session.unbind_thread()
    # 网页端用检索基底,强调联网
    assert "联网检索助手" in sp_on
    assert "web_search" in sp_on
    # 开关指令
    assert "主动用 web_search" in sp_on
    assert "不要调用联网工具" in sp_off
    # 桌面端不是检索基底(用全功能 SYSTEM_PROMPT,内容与网页端不同)
    assert "联网检索助手" not in sp_desktop


# ── 静态页 ──
def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()
