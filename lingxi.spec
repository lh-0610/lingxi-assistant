# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# MCP 客户端（可选功能）：mcp SDK 的子模块大多是函数内懒导入
# （如 from mcp.client.sse import sse_client），PyInstaller 静态分析抓不到，
# 这里全量收集子模块；jsonschema_specifications 还带 JSON 数据文件要一起打进去。
# 没装 mcp 时 collect_* 返回空，不影响打包。
try:
    _mcp_hiddenimports = collect_submodules('mcp')
    _mcp_datas = collect_data_files('jsonschema_specifications')
except Exception:
    _mcp_hiddenimports = []
    _mcp_datas = []


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('icon.ico', '.'),                  # 应用图标
        ('icons', 'icons'),                 # SVG 按钮图标（顶栏/设置/搜索等，走 BASE_DIR/_MEIPASS 读）
        ('assets', 'assets'),               # 桌宠 GIF + 静态立绘（走 RESOURCE_DIR/_MEIPASS 读）
        ('roles', 'roles'),                 # 默认角色卡目录
        ('config.example.json', '.'),       # 配置模板，首次启动时复制成 config.json
    ] + _mcp_datas,
    hiddenimports=[
        # LangChain 各 provider 包，PyInstaller 静态分析有时识别不到
        'langchain_anthropic',
        'langchain_openai',
        'langchain_ollama',
        'langchain_google_genai',
        'langchain_core',
        'markdown',
        # MCP 及其依赖（懒导入 + 第三方传输库，静态分析容易漏）
        'mcp',
        'mcp.client.sse',
        'mcp.client.stdio',
        'mcp.client.streamable_http',
        'mcp.client.session',
        'sse_starlette',
        'httpx_sse',
        'python_multipart',
        'jsonschema',
        'jsonschema_specifications',
        'referencing',
        'rpds',
    ] + _mcp_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='灵犀',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
