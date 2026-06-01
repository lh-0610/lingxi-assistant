"""code_map 工具测试：符号提取（py/js）/ 跳噪声目录 / 边界。

code_map 用 state.current_project 作项目根（project_dir fixture 已设），用 .func 直调。
"""
from src.tools import code_map


class TestCodeMap:
    def test_extracts_py_symbols(self, project_dir):
        (project_dir / "foo.py").write_text(
            "class Bar:\n"
            "    def baz(self):\n"
            "        pass\n"
            "\n"
            "def top():\n"
            "    pass\n",
            encoding="utf-8",
        )
        out = code_map.func("")
        assert "foo.py" in out
        assert "class Bar" in out
        assert "def baz" in out          # 类内方法也提取
        assert "def top" in out

    def test_has_line_numbers(self, project_dir):
        (project_dir / "f.py").write_text("def alpha():\n    pass\n", encoding="utf-8")
        out = code_map.func("")
        assert "L1" in out and "alpha" in out

    def test_skips_noise_dirs(self, project_dir):
        (project_dir / ".git").mkdir()
        (project_dir / ".git" / "hook.py").write_text("def hooked(): pass", encoding="utf-8")
        (project_dir / "real.py").write_text("def real_fn(): pass", encoding="utf-8")
        out = code_map.func("")
        assert "real_fn" in out
        assert "hooked" not in out       # .git 下的不扫

    def test_js_ts_symbols(self, project_dir):
        (project_dir / "a.ts").write_text(
            "export class Foo {}\n"
            "function bar() {}\n"
            "export async function baz() {}\n",
            encoding="utf-8",
        )
        out = code_map.func("")
        assert "class Foo" in out
        assert "function bar" in out
        assert "function baz" in out

    def test_nonexistent_path(self, project_dir):
        assert "不存在" in code_map.func("nope_dir")

    def test_no_source_files(self, project_dir):
        (project_dir / "readme.txt").write_text("hi", encoding="utf-8")
        out = code_map.func("")
        assert "未找到" in out

    def test_subdir_scope(self, project_dir):
        (project_dir / "src").mkdir()
        (project_dir / "src" / "mod.py").write_text("def in_src(): pass", encoding="utf-8")
        (project_dir / "other.py").write_text("def in_root(): pass", encoding="utf-8")
        out = code_map.func("src")
        assert "in_src" in out
        assert "in_root" not in out      # 限定 src 子目录，不含项目根的

    def test_rejects_path_escape(self, project_dir):
        # 安全：.. 不能逃出项目根
        out = code_map.func("../")
        assert "不允许" in out or "项目范围" in out or "不存在" in out

    def test_skips_node_modules(self, project_dir):
        (project_dir / "node_modules").mkdir()
        (project_dir / "node_modules" / "bad.ts").write_text("function noisy() {}", encoding="utf-8")
        (project_dir / "app.ts").write_text("function real() {}", encoding="utf-8")
        out = code_map.func("")
        assert "real" in out
        assert "noisy" not in out

    def test_extracts_async_py_functions(self, project_dir):
        (project_dir / "async_mod.py").write_text("async def fetch_data():\n    pass\n", encoding="utf-8")
        out = code_map.func("")
        assert "fetch_data" in out
