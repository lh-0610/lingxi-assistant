"""共用 fixtures，供 scripts/ 下各测试脚本使用。

使用方法：在测试脚本里加
    from conftest import project_dir, isolated_memory, ...
或直接 pytest scripts/test_xxx.py（pytest 会自动发现同目录 conftest）
"""
import os
import sys
import pytest

# 确保能 import src
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture()
def project_dir(tmp_path):
    """创建临时项目目录，并注入 state.current_project / state.ui_ref。

    用法：def test_xxx(project_dir): ...
    测试结束后自动恢复原 state。
    """
    from src import state

    proj = tmp_path / "myproject"
    proj.mkdir()
    old_project = state.current_project
    old_ui = state.ui_ref
    state.current_project = str(proj)
    state.ui_ref = None  # 无 UI，写文件自动放行

    yield proj

    state.current_project = old_project
    state.ui_ref = old_ui


@pytest.fixture()
def isolated_memory(tmp_path, monkeypatch):
    """将 MEMORY_DIR 等路径全部重定向到 tmp_path，不污染真实数据。

    路径常量在 import 时就绑定了本地引用，所以不仅要 patch 源模块（paths），
    还要 patch 所有 `from .paths import MEMORY_DIR` 的消费模块。

    用法：def test_xxx(isolated_memory): ...
    """
    mem_dir = tmp_path / "chat_memory"
    mem_dir.mkdir()
    mem_dir_str = str(mem_dir)
    index_str = str(mem_dir / "index.json")
    role_str = str(mem_dir / "role_config.json")
    ltm_str = str(mem_dir / "long_term_memory.json")
    proj_str = str(mem_dir / "projects.json")

    # 1) 源模块
    import src.paths as _paths
    monkeypatch.setattr(_paths, "MEMORY_DIR", mem_dir_str)
    monkeypatch.setattr(_paths, "MEMORY_INDEX", index_str)
    monkeypatch.setattr(_paths, "ROLE_CONFIG", role_str)

    # 2) memory_store（`from .paths import MEMORY_DIR` → 本地副本 + _MEMORY_FILE）
    import src.memory_store as _ms
    monkeypatch.setattr(_ms, "MEMORY_DIR", mem_dir_str)
    monkeypatch.setattr(_ms, "_MEMORY_FILE", ltm_str)

    # 3) memory（`from .paths import MEMORY_DIR, MEMORY_INDEX`）
    import src.memory as _mem
    monkeypatch.setattr(_mem, "MEMORY_DIR", mem_dir_str)
    monkeypatch.setattr(_mem, "MEMORY_INDEX", index_str)

    # 4) projects
    import src.projects as _pj
    monkeypatch.setattr(_pj, "PROJECTS_FILE", proj_str)

    return mem_dir


@pytest.fixture()
def sample_py_file(project_dir):
    """在 project_dir 下创建一个示例 Python 文件，返回 Path。"""
    content = (
        "import os\n"
        "import sys\n"
        "\n"
        "def hello():\n"
        '    print("hello world")\n'
        "\n"
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "class Greeter:\n"
        "    def greet(self, name):\n"
        '        return f"Hello, {name}!"\n'
    )
    fpath = project_dir / "sample.py"
    fpath.write_text(content, encoding="utf-8")
    return fpath


@pytest.fixture()
def clean_state():
    """临时重置全局 state，测试结束后恢复。"""
    from src import state

    old = {
        "current_model_index": state.current_model_index,
        "reasoning_enabled": state.reasoning_enabled,
        "current_session_id": state.current_session_id,
        "current_session_title": state.current_session_title,
        "agent_mode": state.agent_mode,
        "chat_history": state.chat_history[:],
        "session_token_usage": dict(state.session_token_usage),
    }

    state.current_session_id = None
    state.current_session_title = None
    state.chat_history = []
    state.session_token_usage = {"input": 0, "output": 0, "total": 0}
    state.agent_mode = "act"

    yield state

    for k, v in old.items():
        setattr(state, k, v)
