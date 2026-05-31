"""全量 Python 语法检查"""
import py_compile, os, sys

errors = []
count = 0
skip = {'.git', '__pycache__', '.venv', 'venv', 'build', 'dist', 'node_modules', '.idea', '.vscode'}

for root, dirs, files in os.walk(os.path.join(os.path.dirname(__file__), "..")):
    dirs[:] = [d for d in dirs if d not in skip]
    for f in files:
        if f.endswith(".py"):
            path = os.path.join(root, f)
            count += 1
            try:
                py_compile.compile(path, doraise=True)
            except py_compile.PyCompileError as e:
                errors.append(str(e))

print(f"语法检查: {count} 个 .py 文件")
if errors:
    for e in errors:
        print(f"  [FAIL] {e}")
    sys.exit(1)
else:
    print("  全部通过！")
