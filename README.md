# 灵犀 AI 助手

> **桌面 Agent + 桌宠立绘 + 本地中文 TTS** —— 一个 Windows 原生 PySide6 应用里完成"模型对话 / 工具调用 / 看见小姐姐 / 听她说话"。

<!-- 演示 GIF / 截图位 —— 用 ScreenToGif 录一段 20s 的"发消息 → 桌宠思考 → 工具调用确认卡 → AI 朗读" 放这里，效果比文字强 10 倍 -->

## 为什么不用现有工具？

只想聊天 → [chatbox](https://github.com/Bin-Huang/chatbox) / [NextChat](https://github.com/ChatGPTNextWeb/NextChat) / [lobe-chat](https://github.com/lobehub/lobe-chat) 更成熟。
想要 Web 版角色卡 → [SillyTavern](https://github.com/SillyTavern/SillyTavern) 是事实标准。

灵犀做的是**别的项目通常只覆盖其中一两块**的组合：

| 同类项目 | 灵犀的不同 |
|---|---|
| 多模型 chat（Electron / Web） | **Windows 原生 PySide6**，启动 1 秒，不带 Chromium 内核 |
| Live2D 桌宠（仅 chat） | 桌宠 + **完整 Agent 工具调用**（文件 / 命令 / ComfyUI 生图）；命令执行前**输入框上方弹内联确认卡** + 危险命令检测 |
| Edge-TTS / 在线 TTS | **本地 [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) 流式合成**：一键拉子进程 + Job Object 防漏 + RTX 50 系 cu128 踩坑笔记齐 |
| OpenAI / Claude 海外模型 | MiMo / Qwen / DeepSeek-V4 思考块解析 / 本地 **Claude Code CLI** 子进程模式 |
| SillyTavern 角色卡（Web） | 同款 .md 角色卡格式，但**桌面 + 本地 TTS + 桌宠立绘**一体 |

如果你**只**想要其中一项，用更专业的工具更好；如果想要**所有这些**装在一起、Windows 原生、能本地跑，灵犀是个还行的选择。

> 仓库附一份角色卡模板（`roles/example.md`），照着填就能用，也可换成任意 SillyTavern 风格的 .md。

## ✨ 功能一览

### 对话核心
- 🤖 **多模型切换**：MiMo / Qwen / Claude / DeepSeek / 本地 Ollama / 本地 Claude Code CLI，**还可在设置里自填任意 OpenAI / Anthropic 兼容的自定义模型**
- 🖼️ **多模态**：支持图片输入（自动切到视觉模型 / 多模态模型）
- 🔧 **工具调用**：文件读写、命令执行、图片生成等。**run_command 执行前在输入框上方弹内联确认卡**（含命令预览 + 1/2/3 数字快捷键 + Esc 取消），危险命令（`rm -rf` / `format` / `sudo` / `drop table` 等）不给"记住"选项
- 💬 **会话历史**：自动保存、侧边栏切换、智能生成标题
- 🧠 **思考过程显示**：折叠/展开模型的 reasoning 内容
- 📊 **Token 用量统计**：实时显示每轮和会话累计用量
- ⚡ **prompt caching + system prompt 拆分**：画图详细规范按需注入，Anthropic/MiMo 走 `cache_control` 省 token

### 编码能力（对标 Cline）
- 🪄 **`edit_file` 精确替换**：改大文件局部，比全量覆盖安全省 token；**写盘前弹蓝色 diff 预览卡**让你审改动
- 🌐 **`search_files` 跨文件正则搜索**（ripgrep 风格，忽略噪声目录）+ `read_file` 行号分页
- 🧭 **Plan / Act 双模式**：Plan 模式 AI 只调研给方案、不动手（只读工具白名单 + 强制提示双保护）
- ↶ **Checkpoint / 撤销**：edit/write/append 写盘前自动 git stash 快照，顶栏一键撤销 AI 上一轮改动（路径级恢复）
- 📄 **`.lingxirules` 项目级指令**：项目根放一个文件写项目约定，自动注入、优先级最高

### MCP 客户端（可选）
- 🔌 **连外部 MCP server**（filesystem / fetch / context7 文档 / memory 等），远程工具自动注入、跟内置工具一样被 AI 调用，**不改一行代码就能扩展能力**
- config.json 配 `mcp_servers`，支持 stdio / SSE / streamable_http；没装 `mcp` 包则静默跳过

### 长期记忆（跨会话）
- 🧠 **角色"天生记得"你**：AI 用 `remember` 存下你的个人信息/偏好/项目约定，新对话自动注入 system prompt，开口就记得（`forget` 删除）
- 原子写 + 损坏/瞬时错误区分，珍贵记忆抗崩溃

### 项目（工作区）
- 📁 **多项目管理**：把不同的工作目录加为项目，会话按项目分组显示
- 🔄 **启动自动恢复**：上次激活的项目下次打开继续在那
- 📍 **输入框下方实时显示当前项目路径**，点击弹切换菜单
- 🧭 **新对话沿用当前项目**：在 A 项目点+新对话 → 仍在 A；切到 B → 新对话归 B
- 🛠️ **所有文件工具自动用项目根作为相对路径基准**（`read_file`、`run_command` 等）
- 📄 **`.lingxirules` 项目级指令**：项目根放一个 `.lingxirules` 文件（纯文本 / md），里面写项目约定（"测试用 pytest"、"格式化用 black"、"提交信息用 conventional commits"），新对话时自动注入 system prompt 末尾，优先级**高于**默认指令
- 🗑️ **移除项目时把它的会话批量改为"无项目"**，不会残留游离记录

### 角色卡
- 📜 **Markdown 系统提示词**：放进 `roles/` 即可加载
- 🎭 内置一份角色卡模板（`roles/example.md`），照着结构填写即可
- 🔁 启动时自动恢复上次激活的角色

### 桌面宠物
- 🎀 **GIF 动画立绘**：idle 待机 / think 思考 / wave 挥手 三套动作（PIL 预提取成 QPixmap 序列 + QTimer 驱动）
- 🖱️ **拖动**移动位置，**单击**切换主对话窗口显示/隐藏
- 📌 **始终置顶 + 无边框 + 透明** —— Windows 11 DWM 边框已用 ctypes API 关掉
- 🍿 **AI 思考时自动切动画**，回答完恢复 idle（跨线程 Signal 投递，worker 不动 UI 对象）
- ⏯️ **动画排队**：当前动画播完本轮再切，不会半路截断显得抽搐
- 🪟 **系统托盘** + **右键菜单**（含"隐藏桌宠"/"退出"）
- 🎬 **配套 MP4→GIF 转换脚本**（[scripts/mp4_to_pet_gif.py](scripts/mp4_to_pet_gif.py)）：把即梦/Runway 等 AI 视频生成的白/黑/绿背景 MP4 一键抠成透明 GIF

### 语音模块
- 🎤 **语音输入**：本地 [faster-whisper](https://github.com/SYSTRAN/faster-whisper)（GPU 优先，CPU 自动降级）
- 🔊 **语音输出**：本地 [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) 流式合成（边合成边播放）
- 🚀 **一键启动语音模块**：点 🔊 按钮，没启动时弹对话框确认 → 自动拉起 API server → 切换权重 → 启用 TTS
- 🛡️ **Windows Job Object**：父进程 crash / 强杀也能保证子进程不残留
- 🧹 **过滤动作描写 + emoji**：`*轻抚发梢*` 这类不朗读；✨ 这类 GBK 不支持的符号自动过滤

### 图片生成
- 🎨 **ComfyUI 本机**优先（API 模式），支持自定义工作流 JSON
- ☁️ **Pollinations.ai** 在线 fallback（无 ComfyUI 时自动用）
- 🧬 LoRA 链 + FaceDetailer + ModelSamplingDiscrete（vPred 模型）

### 系统级
- ⚙️ **设置弹窗**：所有 API 密钥、模型、语音模块路径等可视化编辑
- ❌ **关闭确认**：X 按钮时弹"最小化到托盘 / 退出软件 / 取消"，可记住选择
- 🎯 **高 DPI 锐利渲染**

## 🎨 截图

（你可以在这里贴一张应用截图）

## 📋 支持的对话模型

| 名称 | 类型 | 模型 ID | 支持图片 |
|------|------|---------|---------|
| MiMo V2.5 Pro | mimo | mimo-v2.5-pro | ❌ |
| MiMo V2.5 | mimo | mimo-v2.5 | ❌ |
| MiMo V2 Pro | mimo | mimo-v2-pro | ❌ |
| MiMo V2 Omni | mimo | mimo-v2-omni | ✅ |
| Claude Code | claude-code | claude (本地 CLI) | ❌ |
| Qwen3.5 本地 | ollama | qwen3.5:latest | ❌ |
| Qwen3.5-Plus 云端 | cloud | qwen3.5-plus | ❌ |
| Qwen-Max / Plus / Turbo | cloud | qwen-* | ❌ |
| Claude Sonnet 4 API | anthropic | claude-sonnet-4-20250514 | ✅ |
| Claude Haiku 3.5 API | anthropic | claude-3-5-haiku-20241022 | ✅ |
| DeepSeek V4 Flash / Pro | deepseek | deepseek-v4-* | ❌ |
| ⚙ 自定义模型 | custom | config.json `custom_models` 自填（OpenAI/Anthropic 协议） | 看配置 |

## 🚀 快速开始

### 1. 安装 Python 依赖

```bash
# 主程序依赖
pip install langchain langchain-ollama langchain-openai langchain-anthropic langchain-google-genai PySide6 markdown requests pillow

# 语音功能依赖（可选，没装则按钮不出现）
pip install faster-whisper sounddevice soundfile av ctranslate2 huggingface-hub

# MCP 客户端依赖（可选，没装则 MCP 功能静默跳过）
pip install mcp
```

> Python 3.14 用户：faster-whisper 自动检测 CUDA，找不到就降到 CPU + int8。GPU 加速需要 cuDNN 9.x for CUDA 12。

### 2. 配置 API 密钥

```bash
cp config.example.json config.json
```

打开 `config.json` 填好你有的 API Key（不必填全，没填的模型在切换时会报错但不影响其他模型）。

### 3. （可选）启动 Ollama

```bash
ollama serve
ollama pull qwen3.5:latest
```

### 4. （可选）配置语音模块

如果想用语音输出：

1. 下载 [GPT-SoVITS 整合包](https://github.com/RVC-Boss/GPT-SoVITS)（B 站 @花儿不哭 出的 Windows 一键版）
2. 下载一套你有权使用的音色模型（社区有大量公开的 GPT-SoVITS 预训练音色，注意版权与授权范围）
3. 解压角色 zip，把 `.ckpt` 放进 `GPT_weights_v2/`，`.pth` 放进 `SoVITS_weights_v2/`，挑一个参考 WAV 记好路径
4. 在主程序 → 设置 → "语音模块（GPT-SoVITS）" 区域填好五个字段：
   - GPT-SoVITS 安装目录
   - GPT 权重相对路径
   - SoVITS 权重相对路径
   - 参考音频文件
   - 参考音频对应文本（必须**一字不差**）
5. 启动时点输入框的 🔊 按钮 → 选「是」自动拉起

**Blackwell GPU 用户（RTX 50 系）注意**：GPT-SoVITS 整合包默认带的 PyTorch 不支持 sm_120，需手动升级：
```cmd
cd <整合包目录>
runtime\python.exe -m pip uninstall -y torch torchvision torchaudio xformers
runtime\python.exe -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
```
另外 `GPT_SoVITS/AR/modules/patched_mha_with_cache.py` 顶部需要解开两行 import 注释：
```python
from torch import Tensor
from typing import Callable, List, Optional, Tuple, Union
```

### 5. 运行

```bash
python main.py
```

预期：桌面右下角弹出灵犀的桌宠立绘 + 主聊天窗口（立绘需自行准备，见 `assets/desktop_pet/README.md`）。

### 6.（可选）配置 MCP 工具扩展

MCP（Model Context Protocol）让灵犀连接**外部工具服务器**，把它们的工具（读写文件 / 抓网页 / 查文档 / GitHub 等）动态加进 AI 的工具箱，**不改一行代码就能扩展能力**。

> **MCP 是高级可选功能，默认关闭。** 不配它灵犀照常用（内置工具已覆盖日常）。MCP 跟 Claude Desktop / Cursor / Cline 同款协议，门槛也一样：**stdio 类型的 server 需要你的机器装 [Node.js](https://nodejs.org/)**。

**配置步骤：**

1. **装 Node.js**（用 stdio 类型 server 才需要；SSE 类型不用）：[nodejs.org](https://nodejs.org/) 下载装好，确认 `npx -v` 能用

2. **在设置里开启 MCP**：主界面 → 设置（齿轮）→ MCP 区域 → 勾选「启用 MCP」

3. **编辑 `config.json` 的 `mcp_servers`**，加你要的 server。两种类型：

   > **config.json 在哪？**
   > - 源码运行：项目根目录 `config.json`
   > - 打包 exe 版：`%APPDATA%\灵犀\config.json`（即 `C:\Users\你\AppData\Roaming\灵犀\`）
   > - **最快**：设置弹窗左下角点「config」按钮，直接打开它所在目录


   ```jsonc
   "mcp_enabled": true,
   "mcp_servers": {
     // stdio 类型：本地拉起 server 子进程，需要 Node.js
     "filesystem": {
       "transport": "stdio",
       "command": "npx",
       "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:/你的目录"],
       "env": {}
     },
     // sse 类型：连一个已经在运行的 HTTP 服务，不需要 Node.js
     "context7": {
       "transport": "sse",
       "url": "http://localhost:8010/sse"
     }
   }
   ```

4. **重启灵犀**。日志（`%APPDATA%\灵犀\logs\` 或项目 `logs/`）里搜 `[MCP]`，看到这个就是连上了：
   ```
   [MCP] ✅ filesystem: 已连接，N 个工具: [...]
   [MCP] 共注册 N 个远程工具: [mcp_filesystem_xxx, ...]
   ```

5. **用**：跟 AI 说人话触发，比如「列一下 D:/你的目录 下的文件」，AI 会调 `mcp_filesystem_*` 工具（调用前弹确认卡）。

**常见问题：**

| 现象 | 原因 / 解决 |
|---|---|
| `[MCP] ❌ xxx: Connection closed` + 手动跑报 `Cannot find module 'ajv'` | npx 缓存损坏（npm 通病）。删 `%LOCALAPPDATA%\npm-cache\_npx` 后重试 |
| `[MCP] ❌ xxx: 启动失败` | 先**手动**在终端跑那条 `npx ...` 命令看真实报错（路径不存在 / 缺 token / 没装 Node） |
| stdio server 路径无效 | filesystem 的目录参数必须是**真实存在**的路径 |
| 想临时关掉 MCP | 设置里取消勾选「启用 MCP」（或 config.json 设 `"mcp_enabled": false`），重启 |

> stdio server 第一次 `npx -y` 会联网下载包，慢一点正常；之后走缓存就快了。

## 📁 文件结构

```
.
├── main.py                 # 主入口（高 DPI 配置 + 启动 Qt App + 桌宠 + 托盘）
├── icon.ico                # 应用图标
├── config.json             # 配置（已 .gitignore）
├── config.example.json     # 配置模板
├── lingxi.spec             # PyInstaller 打包配置（产物 exe 名：灵犀）
│
├── src/                    # 主代码
│   ├── agent.py            # Agent 主循环 + 模块 facade + 启动拉起 MCP
│   ├── streaming.py        # 全流式调用（拆成 prepare/handle_chunk/stream）+ 重试退避 + 工具执行
│   ├── tools.py            # @tool 工具函数（项目根路径解析 + 写盘 diff 确认 + build_all_tools/get_tool_map）
│   ├── models.py           # 内置 + 自定义模型合并 → MODEL_LIST；LLM 工厂（带缓存）
│   ├── limits.py           # 集中的魔法数字常量
│   ├── state.py            # 全局可变状态（含 ui_ref / agent_mode）
│   ├── mcp_client.py       # MCP 客户端（常驻 asyncio loop 连外部 server）
│   ├── memory_store.py     # 长期记忆持久化（原子写 + RLock）
│   ├── memory.py           # 对话历史持久化（RLock 串行化所有读写）
│   ├── checkpoint.py       # git stash 快照 + 撤销
│   ├── projects.py         # 项目（工作区）管理
│   ├── roles.py            # 角色卡加载 + get_system_prompt（拼角色/项目/记忆/画图规范）
│   ├── images.py           # 图片格式归一化
│   ├── debug_log.py        # F12 调试 record 缓冲
│   ├── claude_code.py      # Claude Code CLI 调用
│   ├── config.py           # config.json 解析
│   ├── paths.py            # 路径常量 + logger
│   ├── floating.py         # 桌面悬浮宠物 + 托盘（动画排队 + 跨线程 set_thinking）
│   ├── voice.py            # STT (faster-whisper) + TTS (GPT-SoVITS 流式)
│   ├── gpt_sovits_launcher.py  # subprocess 拉起 api_v2.py + Job Object
│   │
│   └── ui/                 # UI 包（chat_window 用 mixin 拆分）
│       ├── __init__.py     # 导出 ChatUI / SettingsDialog
│       ├── chat_window.py  # ChatUI 主窗口（生命周期/事件/agent 集成/渲染原语）
│       ├── confirm_bars.py # 命令确认卡 + edit diff 预览卡 mixin
│       ├── markdown_render.py # Markdown 渲染 + 思考块 mixin
│       ├── search_overlay.py  # Ctrl+F 搜索浮窗 mixin
│       ├── sidebar.py      # 侧栏 + 会话列表 + 项目管理 mixin
│       ├── header.py       # 顶栏 + 按钮样式 + 角色卡 mixin
│       ├── debug_inspector.py # F12 调试弹窗
│       ├── theme.py        # THEMES 字典 + build_stylesheet + 主题持久化
│       ├── widgets.py      # SignalBridge / DragDrop / HistoryRow / CloseConfirmDialog
│       ├── settings_dialog.py  # 设置弹窗（provider 卡片 API key + 自定义模型 + GPT-SoVITS）
│       ├── helpers.py      # 图标生成 / 图片协议块 / Markdown→TTS 剥离
│       ├── prefs.py        # UI 偏好持久化（关闭按钮选择等）
│       └── _base.py        # 共享 BASE_DIR / CONFIG_PATH 常量
│
├── scripts/                # 一次性工具脚本
│   └── mp4_to_pet_gif.py   # 纯色背景 MP4 → 透明 GIF（桌宠素材转换）
│
├── roles/                  # 角色卡 .md
│   └── example.md          # 角色卡模板（照着填）
│
├── assets/desktop_pet/     # 桌宠资源（GIF + 静态 fallback）
│   ├── idle_desktop_pet_final.gif
│   ├── thinking_desktop_pet_final.gif
│   ├── wave_desktop_pet_final.gif
│   └── lingxi_pet.png      # 静态 fallback（GIF 全坏时用）
│
├── icons/                  # SVG 图标（Lucide 风格）
│   ├── mic_lucide.svg
│   ├── speaker_on_lucide.svg
│   ├── speaker_off_lucide.svg
│   └── ...
│
├── chat_memory/            # 会话 JSON + long_term_memory.json + projects.json + role_config.json + ui_prefs.json + theme_config.json
├── logs/                   # 按日期分文件的日志
├── docs/                   # 项目文档（含 TODO.md）
└── README.md               # 本文件
```

## 🎮 使用说明

### 文本聊天

1. 输入框输入文字 → Enter 发送（Shift+Enter 换行）
2. 顶栏下拉切换模型
3. 部分模型支持"思考模式"开关

### 图片输入

- 拖拽图片到聊天窗口 / 点击输入框左下角 📎 / Ctrl+V 粘贴截图
- 应用会自动切到支持视觉的模型（如 MiMo V2 Omni / Claude）

### 语音输入

1. 点输入框左下角 🎤 → 按钮变红开始录音
2. 再点 🎤 → 录音结束，文本自动填进输入框
3. 编辑后点发送

### 语音输出

1. 点输入框左下角 🔊
2. 若 GPT-SoVITS 服务未启动 → 弹对话框询问是否启动
3. 选「是」→ 后台拉起 api_v2.py（约 30-60 秒，状态实时显示）→ 启动后 🔊 自动变绿
4. 之后 AI 每次回复都会朗读（剥离 markdown / 动作描写 / emoji）

### 桌宠

| 操作 | 反应 |
|------|------|
| 拖动 | 移动位置（释放时保存） |
| 左键单击 | 切换主聊天窗口 + 播一遍挥手动画 |
| 右键 | 菜单（显示对话 / 挥手 / 重置位置 / 隐藏桌宠 / 退出） |
| 系统托盘左键 | 唤起主对话 |
| 系统托盘右键 | 菜单（打开对话 / 显示桌宠 / 退出） |

### 角色卡

- 切换角色：主界面 → 角色按钮 → 选择 `roles/` 下的 .md
- 自定义：把 SillyTavern 的 Character Card V2 json 的 `description / personality / mes_example` 整理成 .md 放进 `roles/`

## 🛠️ 配置项详解（`config.json`）

```json
{
  "ollama_base_url":          "http://127.0.0.1:11434",
  "qwen_api_key":             "sk-...",
  "anthropic_api_key":        "sk-ant-...",
  "mimo_api_key":             "tp-...",
  "deepseek_api_key":         "sk-...",
  "google_api_key":           "AIza...",

  "custom_models": [
    {
      "name": "我的私有模型", "model_id": "xxx",
      "api_key": "sk-...", "base_url": "https://.../v1",
      "protocol": "openai", "supports_vision": false, "supports_thinking": false
    }
  ],
  "mcp_enabled":              true,
  "mcp_servers": {
    "filesystem": { "transport": "stdio", "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:/你的真实目录"] },
    "context7":   { "transport": "sse", "url": "http://localhost:8010/sse" }
  },

  "comfy_base_url":           "http://127.0.0.1:8188",
  "comfy_workflow_path":      "",
  "comfy_checkpoint":         "noobaiXLNAIXL_vPred10Version.safetensors",
  "comfy_vae":                "sdxlVAE_sdxlVAE.safetensors",
  "comfy_loras":              [],
  "comfy_face_detailer":      true,
  "comfy_negative_prompt":    "round face, baby face, ...",

  "voice_stt_model":          "small",
  "voice_stt_language":       "zh",
  "voice_tts_default_enabled": false,

  "gpt_sovits_url":           "http://127.0.0.1:9880",
  "gpt_sovits_install_dir":   "D:/你的/GPT-SoVITS整合包目录",
  "gpt_sovits_gpt_model":     "GPT_weights_v2/你的GPT权重.ckpt",
  "gpt_sovits_sovits_model":  "SoVITS_weights_v2/你的SoVITS权重.pth",
  "gpt_sovits_ref_audio":     "D:/你的/参考音频.wav",
  "gpt_sovits_prompt_text":   "音频对应的文本",
  "gpt_sovits_prompt_lang":   "zh",
  "gpt_sovits_text_lang":     "zh",
  "gpt_sovits_media_type":    "wav",
  "gpt_sovits_text_split_method": "cut5",

  "pet_animation_speed":      0.5
}
```

## 🧰 内置工具

| 工具 | 功能 |
|------|------|
| `read_file` | 读取文件内容（offset/limit 行号分页） |
| `write_file` | 创建/覆盖文件（**写盘前弹 diff 确认卡**） |
| `append_file` | 追加内容到文件末尾（**弹 diff 确认卡**） |
| `edit_file` | 精确字符串替换（**弹 diff 预览卡** + 路径白名单，比 write_file 安全） |
| `list_directory` | 列出目录内容 |
| `run_command` | 执行系统命令（30 秒超时，**执行前弹确认卡**，流式输出） |
| `search_in_file` | 单文件关键词搜索（offset/limit 分页） |
| `search_files` | 跨文件正则搜索（ripgrep 风格） |
| `generate_image` | 调 ComfyUI / Pollinations 生成图片 |
| `remember` / `forget` | 长期记忆存取（本地安全操作，不弹确认） |

> 另外接外部 MCP server 后，远程工具以 `mcp_{server}_{tool}` 形式自动加入（调用前弹确认卡）。

## 📦 打包

```bash
pyinstaller lingxi.spec
```

产物在 `dist/灵犀/` 目录。

## ⚠️ 已知约束

- **Python 3.14** 环境：旧版 LangChain API（如 `ConversationBufferMemory`）不可用，主程序已绕开
- `config.json` 含 API 密钥，**已加入 `.gitignore`**，不要提交
- Windows 高 DPI（125%/150%）下文字渲染由 `QT_ENABLE_HIGHDPI_SCALING` + `PassThrough` 策略处理
- QTextBrowser 不支持 `<style>` 标签，Markdown HTML 必须使用内联样式
- MiMo 模型通过 Anthropic 兼容接口调用

## 📝 License

仅供学习和个人使用。

## 🙏 致谢

- [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) by RVC-Boss / 花儿不哭
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) by SYSTRAN
- [LangChain](https://github.com/langchain-ai/langchain)
- [PySide6 / Qt for Python](https://wiki.qt.io/Qt_for_Python)

## ✍️ 作者寄语

我本身是 Java 开发，对 Python 其实并不算熟。这个项目 99% 的代码，都是在 Claude Code 和 GPT 的帮助下写出来的。

这个项目最初是根据我个人的开发习惯和使用需求做出来的。它不一定适合所有人，但对我来说，它是一次把想法变成真实工具的尝试。

很庆幸自己生活在这个时代，可以借助 AI 把脑子里的想法一点点做成真正能用的作品。

接下来，我会继续完善这个项目，让它在一次次迭代中变得更稳定、更好用，也更接近我心中理想的 AI 助手。
