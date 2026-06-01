"""run_tests 工具测试：pytest 输出解析（_parse_pytest_output）+ 路径逃逸防护。

核心解析是纯函数，喂假 pytest 文本验证；不嵌套真跑 pytest（端到端已手动验过）。
"""
from src.tools import _parse_pytest_output, run_tests


class TestParsePytestOutput:
    def test_all_passed(self):
        out = _parse_pytest_output("....\n=== 5 passed in 1.20s ===")
        assert "5 passed" in out
        assert "✅" in out or "全部通过" in out

    def test_failed_with_cases(self):
        text = (
            "FAILED scripts/test_x.py::test_foo - AssertionError: assert 1 == 2\n"
            "FAILED scripts/test_y.py::test_bar - KeyError: 'k'\n"
            "=== 2 failed, 3 passed in 0.50s ==="
        )
        out = _parse_pytest_output(text)
        assert "2 failed" in out and "3 passed" in out
        assert "test_foo" in out and "test_bar" in out      # 失败用例都列出
        assert "AssertionError" in out                       # 错误摘要带上

    def test_error_counted_as_failed(self):
        out = _parse_pytest_output("=== 1 error in 0.10s ===")
        assert "1 failed" in out      # error 计入失败数

    def test_unparseable_falls_back_not_empty(self):
        out = _parse_pytest_output("collection blew up: some weird traceback xyz123")
        assert out.strip()             # 非空
        assert "xyz123" in out         # 退回原始输出尾部

    def test_summary_picks_tail_failed_block(self):
        # FAILED 块之前有别的输出，解析只取末尾连续的 FAILED 行
        text = (
            "some test progress dots ....F..\n"
            "=== FAILURES ===\n"
            "lots of traceback ...\n"
            "=== short test summary info ===\n"
            "FAILED a.py::t1 - ValueError\n"
            "=== 1 failed, 2 passed in 0.3s ==="
        )
        out = _parse_pytest_output(text)
        assert "1 failed" in out and "2 passed" in out
        assert "t1" in out


class TestRunTestsGuard:
    def test_path_escape_rejected(self, project_dir):
        # ../ 逃出项目根 → 直接拒绝，不跑 pytest
        out = run_tests.func("../")
        assert "不允许" in out or "项目范围" in out
