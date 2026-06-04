"""jedi 代码导航工具（find_definition / find_references）测试。

jedi 是可选依赖：没装则 TestJediNav 整组跳过，只保留"未装降级"那条。
"""
import builtins

import pytest

from src.tools import find_definition, find_references

try:
    import jedi  # noqa: F401
    _JEDI = True
except ImportError:
    _JEDI = False


@pytest.mark.skipif(not _JEDI, reason="jedi 未安装")
class TestJediNav:
    def _make(self, project_dir):
        (project_dir / "m.py").write_text(
            "def hello(name):\n"
            "    return f'hi {name}'\n"
            "\n"
            "class Greeter:\n"
            "    def greet(self):\n"
            "        return hello('x')\n",
            encoding="utf-8",
        )

    def test_find_definition_in_file(self, project_dir):
        self._make(project_dir)
        out = find_definition.func("hello", "m.py")
        assert "m.py:1" in out and "function" in out

    def test_find_definition_project_search(self, project_dir):
        """path 留空 → 走 Project.search。"""
        self._make(project_dir)
        out = find_definition.func("Greeter")
        assert "m.py:4" in out

    def test_find_definition_first_occurrence_in_comment_falls_back(self, project_dir):
        """符号首次出现在注释里 → goto 解析不到 → fallback Project.search 仍命中定义行。"""
        (project_dir / "c.py").write_text(
            "# target_fn 是个工具函数\n"
            "def target_fn():\n"
            "    return 1\n",
            encoding="utf-8",
        )
        out = find_definition.func("target_fn", "c.py")
        assert "c.py:2" in out   # 命中 def 那行（line 2），不是注释 line 1

    def test_find_references(self, project_dir):
        self._make(project_dir)
        out = find_references.func("hello", "m.py")
        assert "m.py:1" in out and "m.py:6" in out   # 定义 + 调用处都在


def test_degrades_without_jedi(project_dir, monkeypatch):
    """jedi 未装时给降级提示、不抛异常。"""
    real_import = builtins.__import__

    def _fake(name, *a, **k):
        if name == "jedi":
            raise ImportError("simulated: no jedi")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _fake)
    out = find_definition.func("x", "y.py")
    assert "jedi" in out and "search_files" in out
