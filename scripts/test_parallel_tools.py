"""并行工具调用：_can_parallel 判定 + _parallel_invoke 执行 / 失败日志。

守护 Codex review 3 的两个 P3：
- P3①：空参数（{}）的只读工具（list_directory/code_map/git_log…带默认参数）应能并行，
  此前判定多了个 `and tc.get("args")`，空参被错误退回串行。
- P3②：并行工具失败时绕过了串行 _execute_tool 的错误日志，应仍留 ERROR 日志可排查。
"""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import state
from src.streaming import _can_parallel, _parallel_invoke


# ── _can_parallel 判定 ──────────────────────────────────
class TestCanParallel:
    def test_empty_args_readonly_tools_can_parallel(self):
        """P3① 守护：list_directory / code_map 等空参数只读工具，{} 调也应能并行。"""
        tcs = [{"name": "list_directory", "args": {}},
               {"name": "code_map", "args": {}}]
        assert _can_parallel(tcs) is True

    def test_multiple_readonly_with_args(self):
        tcs = [{"name": "read_file", "args": {"path": "a.py"}},
               {"name": "search_files", "args": {"pattern": "x"}}]
        assert _can_parallel(tcs) is True

    def test_single_tool_not_parallel(self):
        assert _can_parallel([{"name": "read_file", "args": {"path": "a"}}]) is False

    def test_write_tool_blocks_parallel(self):
        """混入写类工具 → 整批退回串行（保序、避免并发写）。"""
        tcs = [{"name": "read_file", "args": {"path": "a"}},
               {"name": "edit_file", "args": {"path": "a", "old_string": "x", "new_string": "y"}}]
        assert _can_parallel(tcs) is False

    def test_plan_mode_blocks_parallel(self):
        tcs = [{"name": "read_file", "args": {}}, {"name": "list_directory", "args": {}}]
        state.agent_mode = "plan"
        try:
            assert _can_parallel(tcs) is False
        finally:
            state.agent_mode = "act"

    def test_remote_session_blocks_parallel(self):
        tcs = [{"name": "read_file", "args": {}}, {"name": "list_directory", "args": {}}]
        state.remote_session = True
        try:
            assert _can_parallel(tcs) is False
        finally:
            state.remote_session = False


# ── _parallel_invoke 执行 + 失败日志 ────────────────────
class TestParallelInvoke:
    def test_empty_args_tool_executes(self, project_dir):
        """空参数只读工具并行执行能拿到真实结果（list_directory 默认列项目根）。"""
        (project_dir / "marker.txt").write_text("x", encoding="utf-8")
        res = _parallel_invoke([{"name": "list_directory", "args": {}},
                                {"name": "list_directory", "args": {}}])
        assert len(res) == 2
        assert "marker.txt" in res[0] and "marker.txt" in res[1]

    def test_failure_is_logged(self, project_dir, caplog):
        """P3② 守护：并行工具失败 → 返回失败串 + 留 ERROR 日志（绕过串行路径也别丢日志）。"""
        with caplog.at_level(logging.ERROR):
            res = _parallel_invoke([{"name": "read_file", "args": {}}])  # 缺必填 path → invoke 抛
        assert "失败" in res[0]
        assert any("执行失败" in rec.getMessage() for rec in caplog.records), \
            "并行工具失败必须留 ERROR 日志"
