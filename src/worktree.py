"""Git Worktree 隔离模式。

让 AI 的文件修改在独立 worktree 中进行，主工作区保持不变。
功能：创建/完成/清理隔离 worktree，以及路径路由。
"""

import os
import re
import shutil
import subprocess
import logging

logger = logging.getLogger(__name__)

# 运行期活跃 worktree 注册表：session_id → {"path": str, "branch": str}
_WORKTREES: dict[str, dict] = {}


# ── helpers ───────────────────────────────────────────────────────────────────


def _cleanup_worktree(path: str) -> None:
    """尝试删除 worktree 目录（best-effort，用于测试 teardown）。"""
    shutil.rmtree(path, ignore_errors=True)


def has_uncommitted_changes(project_path: str) -> bool:
    """主工作区是否存在未提交改动。"""
    if not project_path or not os.path.isdir(project_path):
        return False
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_path, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        return result.returncode == 0 and bool((result.stdout or "").strip())
    except Exception:
        return False


def _worktree_info_from_path(wt_path: str) -> dict | None:
    """从 worktree 的 .git 文件恢复主仓库路径和 worktree 名称。"""
    git_file = os.path.join(wt_path, ".git")
    if not os.path.isfile(git_file):
        return None
    try:
        with open(git_file, encoding="utf-8", errors="replace") as f:
            content = f.read().strip()
    except OSError:
        return None
    if not content.startswith("gitdir: "):
        return None

    git_dir = content[8:].strip()
    git_dir_abs = os.path.realpath(os.path.join(wt_path, git_dir) if not os.path.isabs(git_dir) else git_dir)
    worktrees_dir = os.path.dirname(git_dir_abs)
    git_root = os.path.dirname(worktrees_dir)
    project_path = os.path.dirname(git_root)
    return {
        "git_dir": git_dir_abs,
        "name": os.path.basename(git_dir_abs),
        "project_path": project_path,
    }


def _branch_for_worktree(project_path: str, wt_path: str) -> str | None:
    """读取 worktree 当前分支名。"""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=wt_path, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
    except Exception:
        return None
    branch = (result.stdout or "").strip()
    if branch:
        return branch

    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=project_path, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
    except Exception:
        return None
    current_path = None
    for line in (result.stdout or "").splitlines():
        if line.startswith("worktree "):
            current_path = os.path.realpath(line[len("worktree "):])
        elif current_path == os.path.realpath(wt_path) and line.startswith("branch refs/heads/"):
            return line[len("branch refs/heads/"):]
    return None


def is_git_repo(path) -> bool:
    """判断路径是否在 git 仓库内。"""
    path = str(path)
    if not os.path.isdir(path):
        return False
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path, capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() == "true"
    except Exception:
        return False


def _sanitize_branch(name: str) -> str:
    """把 session_id 转成合法 git 分支名。已合法的名字原样保留。"""
    if not name:
        name = "session"
    original = name
    # 替换所有不合法字符为连字符
    s = re.sub(r"[^a-zA-Z0-9._/-]", "-", name)
    # 去掉连续连字符
    s = re.sub(r"-+", "-", s)
    # 去掉开头的 -
    s = s.lstrip("-")
    # 确保非空
    s = s or f"session-{hash(name) & 0xFFFFFFFF:08x}"
    # 只在名字被修改（含特殊字符）时加 session- 前缀，已合法的名字原样保留
    if s != original and not s.startswith("session-") and not s.startswith("lingxi/"):
        s = f"session-{s}"
    # 截断
    s = s[:100].rstrip("-")
    return s


def _is_within(child, parent) -> bool:
    """判断 *child* 路径是否在 *parent* 之内。

    使用 ``realpath`` + ``commonpath`` 防止 ``..`` / 符号链接越界。
    """
    child_real = os.path.realpath(str(child))
    parent_real = os.path.realpath(str(parent))
    try:
        return os.path.commonpath([child_real, parent_real]) == parent_real
    except ValueError:
        # Windows 上不同盘符会抛 ValueError
        return False


# ── core API ──────────────────────────────────────────────────────────────────


def create(session, project_path: str, session_id: str = None) -> str | None:
    """创建隔离 worktree，返回路径字符串；非 git 仓库返回 ``None``。

    幂等：同一 *session_id* 重复调用返回已有 worktree。
    设置 ``session.worktree`` 并注册到 ``_WORKTREES``。
    """
    if session_id is None:
        session_id = str(id(session))

    # 幂等：已有且目录还在就复用
    if session_id in _WORKTREES:
        info = _WORKTREES[session_id]
        if os.path.isdir(info["path"]):
            session.worktree = info["path"]
            return info["path"]

    project_path = str(project_path)
    if not is_git_repo(project_path):
        return None

    branch = f"lingxi/{_sanitize_branch(session_id)}"
    wt_dir = os.path.join(project_path, ".lingxi-worktrees")
    wt_path = os.path.join(wt_dir, session_id)

    try:
        os.makedirs(wt_dir, exist_ok=True)
        if not _is_within(wt_path, wt_dir):
            raise ValueError("worktree 路径越界")

        # 若分支残留先删（从上次异常退出恢复）
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=project_path, capture_output=True, text=True,
        )

        # 创建 worktree + 分支
        subprocess.run(
            ["git", "worktree", "add", "-b", branch, wt_path, "HEAD"],
            cwd=project_path, capture_output=True, text=True,
            check=True, timeout=30,
        )

        _WORKTREES[session_id] = {"path": wt_path, "branch": branch}
        session.worktree = wt_path
        logger.info(f"已创建隔离 worktree: {wt_path} (branch={branch})")
        return wt_path

    except Exception as e:
        logger.error(f"创建 worktree 失败: {e}")
        _cleanup_worktree(wt_path)
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=project_path, capture_output=True, text=True,
        )
        return None


def finish(session, *, apply_changes: bool = False) -> tuple[bool, str]:
    """结束会话的 worktree。

    ``apply_changes=True`` 时先把隔离区相对 HEAD 的改动应用回主项目，成功后再清理；
    否则只丢弃隔离区并清理。返回 ``(success, message)``。
    """
    wt_path = session.worktree
    if not wt_path:
        return True, "没有活跃的 worktree。"

    # 从注册表反查 session_id 和 branch
    sid_found = None
    for sid, info in list(_WORKTREES.items()):
        if info["path"] == wt_path:
            sid_found = sid
            branch = info["branch"]
            break

    if sid_found is not None:
        _WORKTREES.pop(sid_found, None)
    else:
        branch = None

    info = _worktree_info_from_path(wt_path)
    project_path = info["project_path"] if info else None
    branch = branch or (_branch_for_worktree(project_path, wt_path) if project_path else None)

    if apply_changes:
        ok, msg = _apply_changes_to_project(wt_path, project_path)
        if not ok:
            if sid_found is not None:
                _WORKTREES[sid_found] = {"path": wt_path, "branch": branch}
            return False, msg

    if project_path and branch:
        _remove_worktree(wt_path, branch)
    else:
        _cleanup_worktree(wt_path)

    session.worktree = None
    if apply_changes:
        return True, "隔离区改动已应用回主项目，并已清理 worktree。"
    return True, "隔离区已丢弃并清理。"


def cleanup_all() -> None:
    """清理所有注册的 worktree。调用时机：程序退出。"""
    for sid, info in list(_WORKTREES.items()):
        try:
            _remove_worktree(info["path"], info["branch"])
        except Exception as e:
            logger.warning(f"清理 worktree {sid} 失败: {e}")
            _cleanup_worktree(info["path"])
    _WORKTREES.clear()


# ── 内部 ──────────────────────────────────────────────────────────────────────


def _remove_worktree(wt_path: str, branch: str) -> None:
    """通过 ``git worktree remove`` 移除 worktree + 分支。"""
    info = _worktree_info_from_path(wt_path)
    project_path = info["project_path"] if info else None

    if project_path and os.path.isdir(project_path):
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", wt_path],
                cwd=project_path, capture_output=True, text=True, timeout=30,
            )
        except Exception:
            _cleanup_worktree(wt_path)

        try:
            subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=project_path, capture_output=True, text=True, timeout=10,
            )
        except Exception:
            pass
    else:
        _cleanup_worktree(wt_path)


def _apply_changes_to_project(wt_path: str, project_path: str | None) -> tuple[bool, str]:
    """把 worktree 相对 HEAD 的所有改动应用到主项目工作区。"""
    try:
        if not project_path or not os.path.isdir(project_path):
            return False, "无法定位主项目，已保留隔离区未清理。"
        if not os.path.isdir(wt_path):
            return False, "隔离区目录不存在，无法恢复改动。"

        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=wt_path, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        if status.returncode != 0:
            return False, f"读取隔离区状态失败：{(status.stderr or '').strip() or '未知错误'}"
        if not (status.stdout or "").strip():
            return True, "隔离区没有需要恢复的改动。"

        add = subprocess.run(
            ["git", "add", "-A"],
            cwd=wt_path, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        if add.returncode != 0:
            return False, f"暂存隔离区改动失败：{(add.stderr or '').strip() or '未知错误'}"

        diff = subprocess.run(
            ["git", "diff", "--cached", "--binary", "HEAD"],
            cwd=wt_path, capture_output=True,
            timeout=30,
        )
        if diff.returncode != 0:
            stderr = diff.stderr.decode("utf-8", errors="replace") if diff.stderr else ""
            return False, f"生成隔离区补丁失败：{stderr.strip() or '未知错误'}"
        if not diff.stdout:
            return True, "隔离区没有需要恢复的改动。"

        apply = subprocess.run(
            ["git", "apply", "--3way", "--binary"],
            cwd=project_path, input=diff.stdout, capture_output=True,
            timeout=30,
        )
        if apply.returncode != 0:
            stderr = apply.stderr.decode("utf-8", errors="replace") if apply.stderr else ""
            return False, (
                "恢复隔离区改动失败，已保留 worktree。"
                f"\n{stderr.strip() or '请检查主项目是否有冲突或未提交改动。'}"
            )
        return True, "隔离区改动已应用到主项目工作区。"
    except subprocess.TimeoutExpired:
        return False, "恢复隔离区改动超时，已保留 worktree。"
    except Exception as e:
        return False, f"恢复隔离区改动异常，已保留 worktree：{e}"
