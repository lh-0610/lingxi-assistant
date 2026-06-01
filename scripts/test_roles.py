"""角色提示词、画图意图与项目规则注入测试。"""
from langchain_core.messages import AIMessage, HumanMessage

import src.roles as roles
from src import state


class TestPaintingIntent:
    def test_detects_recent_user_keyword(self):
        assert roles._detect_painting_intent([HumanMessage(content="帮我画一张头像")]) is True

    def test_detects_recent_generate_image_tool_call(self):
        history = [AIMessage(
            content="",
            tool_calls=[{"name": "generate_image", "args": {"prompt": "x"}, "id": "1"}],
        )]

        assert roles._detect_painting_intent(history) is True

    def test_ignores_keywords_older_than_recent_window(self):
        history = [HumanMessage(content="画一张图")]
        history.extend(HumanMessage(content=f"普通消息 {i}") for i in range(6))

        assert roles._detect_painting_intent(history) is False


class TestLingxiRules:
    def test_missing_file_returns_empty(self, tmp_path):
        assert roles._load_lingxirules(str(tmp_path)) == ""

    def test_truncates_oversized_file(self, tmp_path):
        (tmp_path / ".lingxirules").write_text("x" * 20001, encoding="utf-8")

        result = roles._load_lingxirules(str(tmp_path))

        assert result.startswith("x" * 20000)
        assert "已截断至前 20000 字" in result


class TestRoleNames:
    def test_extracts_name_from_heading_suffix(self):
        assert roles._extract_character_name("# 灵犀助手 · 小夏", "fallback") == "小夏"

    def test_extracts_name_from_brackets(self):
        assert roles._extract_character_name("角色名：「小夏」", "fallback") == "小夏"

    def test_falls_back_when_name_is_missing(self):
        assert roles._extract_character_name("普通角色说明", "fallback") == "fallback"


class TestSystemPrompt:
    def test_injects_project_rules_plan_mode_and_painting_guide(
        self, isolated_memory, tmp_path, monkeypatch,
    ):
        (tmp_path / ".lingxirules").write_text("必须运行 pytest", encoding="utf-8")
        monkeypatch.setattr(state, "current_project", str(tmp_path))
        monkeypatch.setattr(state, "agent_mode", "plan")

        result = roles.get_system_prompt(include_painting=True)

        assert f"`{tmp_path}`" in result
        assert "必须运行 pytest" in result
        assert "当前是 Plan" in result
        assert roles.PAINTING_GUIDE in result
