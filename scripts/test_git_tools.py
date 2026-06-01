"""git_diff / git_log 工具测试：非 git 仓库降级 + 路径逃逸防护。

需要 git 才有意义，没装则整文件跳过。临时项目（project_dir）默认不是 git 仓库。
"""
import shutil

import pytest

from src.tools import git_diff, git_log

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="git 未安装")


class TestGitTools:
    def test_diff_not_a_repo(self, project_dir):
        assert "不是 git 仓库" in git_diff.func("")

    def test_log_not_a_repo(self, project_dir):
        assert "不是 git 仓库" in git_log.func("")

    def test_diff_path_escape_rejected(self, project_dir):
        assert "不允许" in git_diff.func("../")

    def test_log_path_escape_rejected(self, project_dir):
        assert "不允许" in git_log.func("../")
