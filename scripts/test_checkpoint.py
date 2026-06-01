"""checkpoint 的真实 git 仓库回归测试。"""
import shutil
import subprocess

import pytest

import src.checkpoint as checkpoint


pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="git 未安装")


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


@pytest.fixture()
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "LingXi Tests")
    tracked = repo / "tracked.txt"
    tracked.write_text("original\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")

    checkpoint.clear_all_checkpoints()
    checkpoint._is_git_cache.clear()
    yield repo
    checkpoint.clear_all_checkpoints()
    checkpoint._is_git_cache.clear()


class TestCheckpointUndo:
    def test_clean_tracked_file_restores_head(self, git_repo):
        path = git_repo / "tracked.txt"
        ref = checkpoint.make_checkpoint(str(git_repo), "edit_file", str(path))
        path.write_text("ai edit\n", encoding="utf-8")

        ok, message = checkpoint.undo_last_checkpoint()

        assert ref.startswith("__HEAD__")
        assert ok is True
        assert "恢复到 HEAD" in message
        assert path.read_text(encoding="utf-8") == "original\n"

    def test_new_file_is_removed(self, git_repo):
        path = git_repo / "created.txt"
        checkpoint.make_checkpoint(str(git_repo), "write_file", str(path))
        path.write_text("created by ai\n", encoding="utf-8")

        ok, message = checkpoint.undo_last_checkpoint()

        assert ok is True
        assert "撤销新建文件" in message
        assert not path.exists()

    def test_dirty_tracked_file_restores_pre_ai_content(self, git_repo):
        path = git_repo / "tracked.txt"
        path.write_text("user edit\n", encoding="utf-8")
        ref = checkpoint.make_checkpoint(str(git_repo), "edit_file", str(path))
        path.write_text("ai edit\n", encoding="utf-8")

        ok, _ = checkpoint.undo_last_checkpoint()

        assert ref == "stash@{0}"
        assert ok is True
        assert path.read_text(encoding="utf-8") == "user edit\n"

    def test_untracked_file_restores_pre_ai_content(self, git_repo):
        path = git_repo / "draft.txt"
        path.write_text("user draft\n", encoding="utf-8")
        ref = checkpoint.make_checkpoint(str(git_repo), "edit_file", str(path))
        path.write_text("ai edit\n", encoding="utf-8")

        ok, _ = checkpoint.undo_last_checkpoint()

        assert ref == "stash@{0}"
        assert ok is True
        assert path.read_text(encoding="utf-8") == "user draft\n"

    def test_new_file_outside_project_is_not_deleted(self, git_repo, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("keep me\n", encoding="utf-8")
        checkpoint._push_stack(
            str(git_repo), "__HEAD__:00:00:00", "write_file", str(outside),
            existed=False, tracked=False,
        )

        ok, message = checkpoint.undo_last_checkpoint()

        assert ok is False
        assert "超出项目范围" in message
        assert outside.read_text(encoding="utf-8") == "keep me\n"
