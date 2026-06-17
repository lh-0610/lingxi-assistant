# 灵犀 AI 助手 — 待办清单

> 基于实际读代码 + 当前使用场景整理。优先级按"实际影响 / 实现成本"排序，不照搬 ChatGPT/Cursor 的功能列表。

---

## ✅ 已修复 / 已实现

- ~~**生成时无法上下滚动**~~ — 5 处强制 `setValue(maximum())` 改为智能滚动：用户已在底部时跟随，往上翻看时不打扰（[src/ui/chat_window.py `_scroll_guard()`](../src/ui/chat_window.py)）
- ~~**Ctrl+V 粘贴图片**~~ — 早已实现，同时支持直接粘贴截图（QImage）和粘贴文件路径（资源管理器复制的图片文件）
- ~~**#2 `_thinking_history` 内存泄漏**~~ — 新建/切换/重绘会话时会重置渲染状态，清理 `_thinking_history`、`_code_blocks`、消息缓存等（[src/ui/chat_window.py `_reset_render_state()`](../src/ui/chat_window.py)）。**注意 `_image_paths` 仍未接入，见 P0 #4**
- ~~**#5 代码块复制按钮**~~ — Markdown 代码块会生成 `action:copy_code:N` 链接，并由 `_on_link_clicked` 统一处理复制（[src/ui/chat_window.py `_md_to_html()`](../src/ui/chat_window.py)）
- ~~**#6 消息复制 / 重新生成**~~ — AI 消息末尾已加入复制和重新生成 action 链接（[src/ui/chat_window.py `_render_markdown()`](../src/ui/chat_window.py)）
- ~~**#7 Ctrl+F 对话内搜索**~~ — 已实现浮动搜索框、Ctrl+F、F3/Shift+F3 查找（[src/ui/chat_window.py `_toggle_search()`](../src/ui/chat_window.py)）
- ~~**#8 拖拽上传图片/文件**~~ — `DragDropTextBrowser` / `DragDropTextEdit` 和主窗口拖拽事件已支持图片与文本文件拖入（[src/ui/widgets.py](../src/ui/widgets.py)）。**注意拖拽文本被静默截到 50KB，见 P0 #5**
- ~~**#16 Token 用量显示**~~ — `agent.py` 提取 usage，`chat_window.py` footer 显示本轮与累计 token（[src/agent.py](../src/agent.py)，[src/ui/chat_window.py `_update_token_usage()`](../src/ui/chat_window.py)）
- ~~**#18 自动标题生成**~~ — 首条消息后自动生成短标题并写回会话索引和 JSON（[src/memory.py `maybe_generate_session_title()`](../src/memory.py)）
- ~~**#19 工具执行前确认**~~ — `run_command` 调用前弹**内联确认卡**（不是模态弹窗），含命令预览 + 1/2/3 数字快捷键 + Esc 取消；**会话级 allowlist**（"允许并记住"）+ **危险命令检测**（rm -rf / format / sudo / drop table 等不给"记住"选项）。详见 [src/ui/chat_window.py `_build_command_confirm_bar`](../src/ui/chat_window.py) 和 [src/tools.py `run_command`](../src/tools.py)
- ~~**#20 浮动"回到底部"按钮**~~ — 聊天区右下角浮动按钮已实现，离开底部显示，点击回到底部（[src/ui/chat_window.py `_scroll_to_bottom()`](../src/ui/chat_window.py)）
- ~~**`memory.py` 并发写入风险**~~ — `index.json` / 各会话文件的读-改-写原子化（`threading.RLock`），`save_session` / `_update_index` / `delete_session` / `move_sessions_to_no_project` 都串行化（[src/memory.py](../src/memory.py)）
- ~~**项目切换器 + 项目指示条**~~ — 启动恢复上次项目；输入框下方显示当前项目路径（点击弹切换菜单）；新对话沿用当前项目；删项目时把它的会话批量改成"无项目"（[src/ui/chat_window.py `_build_project_indicator`](../src/ui/chat_window.py)）
- ~~**所有文件工具用项目根作为相对路径基准**~~ — `read_file` / `write_file` / `list_directory` / `search_in_file` / `run_command` 都通过 `_project_cwd()` / `_resolve_path()` 解析；不再永远走 D:\langchain（[src/tools.py](../src/tools.py)）- ~~**#11 魔法数字提取为常量**~~ — 新增 [src/limits.py](../src/limits.py)，集中管理会话上限、流式重试、历史裁剪、工具输出截断、搜索分页和 Debug Inspector 预览长度等高频限制值。
- ~~**#15 多轮工具调用 Markdown 渲染**~~ — 发现 tool_calls 后、执行工具前会先渲染当前轮 AI 文本，避免中间轮回复长期停留为纯文本（[src/agent.py `agent_loop`](../src/agent.py)）。
- ~~**#21 多角色卡管理**~~ — 角色按钮菜单会扫描 `roles/*.md` 并直接列出可切换角色，仍保留外部导入和恢复默认（[src/ui/header.py `_load_role_card`](../src/ui/header.py)）。
- ~~**ui.py 4400 行职责过载**~~ — 拆成 `src/ui/` 包：chat_window / theme / widgets / settings_dialog / helpers / prefs / _base
---

## 🔴 P0 — 必须修（潜在 bug / 数据风险）

会真实造成问题，越早修越好。

### ✅ 22. `generate_image` 硬编码到他人路径（已完成）
**实现**：[src/tools.py:927-931](../src/tools.py#L927) 改成：有项目走 `<项目根>/outputs/`，无项目走 `chat_memory/generated/`。

### ✅ 23. closeEvent 不清理待决的命令确认 → worker 卡 5 分钟（已完成）
**实现**：[src/ui/chat_window.py `closeEvent`](../src/ui/chat_window.py) 已在退出前调用 `_release_pending_confirm()` + `_release_pending_edit()`，把挂起的 result 写 False 并 `set()` 唤醒 worker。同样的释放也加进了 `_on_send_click` 暂停按钮路径（避免点暂停时按钮不响应）。

### ✅ 24. 拖拽文本文件 **静默截到 50KB**（已完成）
**实现**：[src/ui/chat_window.py `dropEvent`](../src/ui/chat_window.py) 读取上限提到 20 万字符，超过时插入 `[文件过长，仅插入前 200K 字符]` 提示。
**审查修正**：Codex 原版用 `os.path.getsize`（字节）跟字符上限比较，中文文件会误报截断。改为多读 1 个字符 `f.read(limit+1)` 用 `len(content) > limit` 判断是否真截断，单位一致。

### ✅ 25. `_is_destructive_command` 正则可被绕过（已完成，已审查）
**问题**：[src/ui/chat_window.py `_is_destructive_command`](../src/ui/chat_window.py) 危险命令检测的几个 bypass：
  - `rm -i -r foo` ❌ 未识别（`-i` 在中间打断了正则前向）
  - PowerShell `Remove-Item -Force tmp/`（没 -Recurse）❌
  - SQL `DROP /*! */ TABLE users` ❌（注释绕过）

**影响**：危险命令会显示"允许并记住"选项，被用户错点后 AI 可在本次会话内无限执行该类命令。
**修法**：把正则改成两段式 `\brm\b.*?(?:-r\w*|--recursive|--force)`（命令首词 + 任意位置出现 flag）；SQL 用 `re.sub` 先去掉 `/*...*/` 再判断。
**工作量**：20 分钟
**实现**：rm 改成 lookahead `\brm\b(?=.*(?:\s|^)(?:-\w*r\w*|-\w*f\w*|--recursive|--force)\b)` 抓分散 flag；SQL 先 `re.sub` 去掉 `/*...*/` 注释再判 drop/truncate table。审查实测 9/9 用例通过（含 `rm -i -r foo`、`DROP /*! */ TABLE`、`truncate table` 命中，`rm foo`/`git status` 不误判）。

### ✅ 26. `_image_paths` + QPixmap addResource 资源累积（已完成，已审查）
**实现**：`_reset_render_state` / `_redraw_chat` 都清 `_image_paths`；`_reset_render_state` 调 `chat_area.document().clear()` 真正释放 addResource 注册的 QPixmap（比 `chat_area.clear()` 更彻底）。审查确认正确。

### ✅ 27. 切到文本模型时图片被**静默剥成占位符**（已完成，已审查）
**实现**：[src/streaming.py](../src/streaming.py) 在剥图前检测 `_history_has_image_blocks`，按 `(session_id, model_index)` dedup 只在聊天区提示一次"当前模型不支持视觉，历史图片已转为文本占位发送"。审查确认 dedup 逻辑正确（不会每轮刷屏）。

### ✅ 28. `search_in_file` 静默丢匹配（已完成，已审查）
**实现**：[src/tools.py:807](../src/tools.py#L807) `search_in_file(path, keyword, offset=0, limit=50)` 加分页参数（offset≥0 / limit 1-200 夹紧），返回本次显示范围 `X-Y / 总数`、剩余匹配数、下次继续查看的 `offset=N`；offset 越界给明确提示。审查确认正确。

### ✅ 1. chat_history 无长度限制（已完成）
**实现**：[src/streaming.py:75](../src/streaming.py#L75) `_maybe_trim_history` 滑动窗口：估算 token 数（中英混排经验值 0.7 字符/token，图片块按 1000 token 计）超过 80K budget 就裁中段，保留首条 SystemMessage + 最近 20 条。被裁的中段用 SystemMessage 占位 `[历史已自动裁剪：跳过中间 N 条]` 让 AI 知情。UI 仍显示完整。

### ✅ 2. `_thinking_history` 内存泄漏
**问题**：[src/ui/chat_window.py](../src/ui/chat_window.py) 的 `_thinking_history` dict 永不清理，每次思考都加新条目。长会话/多次画图后会累积。
**修法**：在 `_on_finished` 或 `_new_chat` 时清空旧条目（保留当前会话的）。
**工作量**：5 分钟
**状态**：已完成。见上方“已修复 / 已实现”。

### ✅ 3. config 启动无校验（已完成，已审查）
**实现**：[src/models.py `get_model_config_issues`](../src/models.py) 检测当前模型所需 key 缺失/占位符（`_looks_like_placeholder` 认 `xxxx`/`your-api-key`/`sk-` 等）；启动后（QTimer 延迟 300ms）和切换模型时在聊天区 + toast 提示，按 `(model_index, issues)` dedup 不重复刷。审查确认正确。

### 🟡 4. UI 线程与 agent 线程竞争 chat_history（部分缓解，已审查）
**问题**：切换会话时 `_redraw_chat` 在 UI 线程遍历 `chat_history`，agent 线程同时可能 `append`。理论上能崩。
**已做**：`_redraw_chat` 先 `list(agent.chat_history)` 拷快照再遍历，挡掉"遍历时列表 size 变化"的崩溃。审查确认有效。
**未做**：消息对象本身的并发修改、全局写锁还没加（实际触发概率低，留着）。

---

## 🟡 P1 — 高 ROI 功能（日常使用频次高 × 实现简单）

每天会用到，且半小时内可以搞定的。

### ✅ 5. 代码块复制按钮
**价值**：让灵犀生成代码后直接复制，避免手动选中。
**实现**：在 `_md_to_html` 渲染 `<pre>` 时插入一个浮动按钮（带 `action:copy_code:N` 链接）。
**工作量**：1 小时
**状态**：已完成。见上方“已修复 / 已实现”。

### ✅ 6. 消息复制 / 重新生成
**价值**：AI 回复后想复用文本 / 不满意重来。
**实现**：每条 AI 消息末尾插入两个 action 链接（复制 / 重生成），跟现有"重试"按钮模式一致。
**工作量**：2 小时
**状态**：已完成。见上方“已修复 / 已实现”。

### ✅ 7. Ctrl+F 对话内搜索
**价值**：长对话翻不到要找的内容。
**实现**：QTextBrowser 自带 `find()` 方法，加个浮动输入框 + Ctrl+F 快捷键。
**工作量**：1 小时
**状态**：已完成。见上方“已修复 / 已实现”。

### ✅ 8. 拖拽上传图片/文件
**价值**：跟粘贴一样高频。
**实现**：在 `entry` 上重写 `dragEnterEvent` / `dropEvent`，把图片走 `_pending_images`，文本文件直接读内容塞到输入框。
**工作量**：30 分钟
**状态**：已完成。见上方“已修复 / 已实现”。

---

## 🟢 P2 — 工程质量优化（值得做，不急）

中长期会越来越重要。

### ✅ 9. read_file 改分页
**问题**：原 8000 字硬截断，大文件直接被截断丢内容。
**实现**：`read_file(path, offset=1, limit=2000)` 行号前缀（`cat -n` 风格）+ 截断时提示"还有 N 行未读——继续读用 offset=X"
**状态**：已完成（与 edit_file / search_files 同期上线，构成 coding 能力三件套）

### ✅ 10. LLM 实例缓存（已完成，已审查）
**实现**：models 层 `_LLM_CACHE` 缓存 `_create_llm()` 实例，agent 层 `_BOUND_LLM_CACHE` 缓存 `bind_tools(ALL_TOOLS)` 结果（贵的是 bind）。缓存 key 含 model_index/provider/model_id/effective_reasoning + custom 连接参数。审查确认：双层缓存各有用途（`describe_images_with_vision` 也吃到底层缓存），config 改动需重启所以缓存不会持有过期连接参数。

### ✅ 11. 魔法数字提取为常量（已完成）
**问题**：`50`(会话上限)、`8000`(read 截断)、`500`(工具结果截断)、`480`(缩略图)、`30`(命令超时) 散在各处。
**修法**：抽到统一常量模块集中管理。
**工作量**：30 分钟
**实现**：新增 [src/limits.py](../src/limits.py)，先集中会话上限、历史裁剪、流式重试、read/search 默认分页、命令超时/截断、工具结果预览和 Debug Inspector 预览长度。UI 像素尺寸暂不强抽，避免过度工程。

### ✅ 12. 错误重试 + 指数退避（已完成，已审查）
**实现**：[src/streaming.py `_stream_chunks_with_retry`](../src/streaming.py) 最多 3 次指数退避（1s/2s）。**关键**：只在首个 chunk 之前失败才重试，一旦 `yielded=True`（已输出）就 re-raise，避免重复输出；也尊重 `state.stop_flag`。审查确认设计正确，正好绕开重复输出问题。

### ✅ 13. logs / chat_memory 自动清理（已完成，已审查）
**实现**：[src/paths.py `_cleanup_old_logs`](../src/paths.py) 启动时按 mtime 删 30 天前 `.log`；[src/memory.py `_update_index`](../src/memory.py) 裁到 50 条时同步删被挤出的会话 JSON（跳过当前激活会话，防误删）。审查确认正确。

### ✅ 14. `_stream_with_tools` 函数拆分（已完成）
**问题**：单函数 200+ 行，含心跳/读取/思考解析/工具收集多重职责。
**实现**：拆成 4 块——`_prepare_stream_history(ui)`（归一化图片/裁剪/system prompt/建 Debug record）、`_handle_stream_chunk(st, chunk, ...)`（单 chunk 派发，用 `_StreamState` 持跨 chunk 状态）、`_collect_tool_calls(gathered)`、`_extract_thinking(gathered)`。`_stream_with_tools` 退化为编排。行为零变化（逐行搬，`continue`→`return`），公开签名 `(ui)` 不变。

### ✅ 15. Markdown 渲染只覆盖最后一轮（已完成，已审查）
**问题**：多轮工具调用时，前几轮的 AI 文本永远是纯文本（没 markdown 渲染）。
**实现**：当本轮产生 tool_calls 时，在把 AIMessage 写入历史后、执行工具前先调用 `render_final_markdown(clean_text)`。此时 `_ai_reply_start` 仍指向当前轮 AI 正文，随后工具输出会重置下一轮起点。
**审查修正**：原版中间轮也会触发 TTS，把"我先看下文件"之类的过程话都念出来。给 `render_final_markdown` 加 `speak` 参数，中间轮传 `speak=False` 只渲染不朗读，最终轮保持朗读。

### ✅ 29. Anthropic / MiMo prompt caching + SYSTEM_PROMPT 拆分（已完成）
**问题**：原 system prompt 4000+ 字（基础工具 + 画图详细规范），每轮全部重发。
**实现**：
1. **SYSTEM_PROMPT 拆分**：基础 prompt 砍到 ~1500 字；画图详细规范（~2500 字）抽成
   `PAINTING_GUIDE` 单独常量，**按需注入**——`_detect_painting_intent` 关键词匹配
   （"画/图片/壁纸/draw/show me/..."）+ 历史里调过 generate_image 才拼上
2. **Anthropic / MiMo 缓存**：`_wrap_system_for_cache` 在 stream 前把 system message
   包成 content block + `cache_control: {"type": "ephemeral"}`；其它 provider 保持
   纯字符串
3. **同时让 .lingxirules / 项目上下文每轮重新读**——文件改了立刻生效
**实测节省**：普通对话每轮省 ~1720 tokens（PAINTING_GUIDE 占基础 prompt 60%）；
100 轮非画图对话省 ~172k tokens。Anthropic / MiMo 用户额外吃到 ~10x 缓存折扣。
**验证**：用 Debug Inspector 看 usage 里 `cache_creation_input_tokens` / `cache_read_input_tokens` 字段是否非零。

---

## 🔵 P3 — 中等价值（看心情做）

### ✅ 16. Token 用量显示
每次回复后显示 input/output tokens，对话总计。LangChain 的 `ChatXxx` 都返回 usage 信息。
**状态**：已完成。见上方“已修复 / 已实现”。

### ✅ 18. 自动标题生成
用户发完第一条消息后异步调一次 LLM 生成 10 字内标题，替换"前 30 字"的粗暴方案。
**状态**：已完成。见上方“已修复 / 已实现”。

### ✅ 19. 工具执行前确认（敏感操作）（已完成）
**实现**：四个会改东西的工具现在都有写盘前确认 —— `run_command`（命令确认卡）、`edit_file` / `write_file` / `append_file`（diff 预览卡）。后三者共用新抽的 [src/tools.py `_confirm_file_write`](../src/tools.py)：worker 线程算 unified diff → 投到 UI 弹卡 → 阻塞等审批，`_session_edit_path_allowlist` 支持"信任此文件后续修改"。`write_file` 全量覆盖前显示 旧→新 diff（之前只有 checkpoint 没确认，比 edit_file 还危险），`append_file` 显示 旧→旧+追加 diff。无 UI（CLI/测试）时直接放行。

### ✅ 20. 浮动"回到底部"按钮
长对话往上翻看时浮个 ↓ 按钮。
**状态**：已完成。见上方“已修复 / 已实现”。

### ✅ 21. 多角色卡管理（已完成）
当前只能加载 1 个。改成 `roles/` 目录扫描所有 .md，下拉切换。
**实现**：角色卡按钮菜单自动扫描 `roles/*.md`，可直接切换目录内角色；仍支持外部导入 `.md/.txt` 和恢复默认角色。

---

## ⚪ 不做或缓做（成本不划算）

| 项 | 原因 |
|---|---|
| 深色模式 | 整个 STYLESHEET 字符串重构成本太高，个人用价值低 |
| 单元测试 | 个人项目工作量大，UI 难测，靠手测够用 |
| 拆分 ui.py / agent.py | 50KB / 35KB 远没到必拆程度，强拆出来一堆相互引用反而难维护 |
| API key 加密（keyring/AES） | 单机自用，明文 + .gitignore 已足够 |
| RAG 知识库 | 重，需要向量库，且现在 read_file/search 已经能解决多数场景 |
| 多语言 UI | 自用工具，不需要 |
| 插件系统 / 角色卡市场 | 过度工程 |
| 自动更新 | 自用工具，git pull 即可 |
| MCP Server | 接入复杂，目前工具够用 |
| 多窗口分屏 | 跟一个对话窗口的初衷冲突 |

---

## 🚀 下一阶段路线（让 coding 能力追上 Cline）

`edit_file` + `search_files` + `read_file` 分页已构成"找文件 → 看上下文 → 精确改"三件套。下一波目标是让 agent 真能在中等项目里干活，按 ROI 排：

### ✅ A. 项目级自定义指令 `.lingxirules`（已完成）
**做什么**：项目根放 `.lingxirules` 文件（纯文本 / md），启动时自动 append 到 system prompt
**收益**：让 AI 立刻"懂你的项目"——比如自动按 "用 black 格式化" / "测试用 pytest" / "import 顺序按 isort" 这些项目约定干活，不用每次对话开头都写一遍
**实现**：`src/roles.py:get_system_prompt()` 读项目根的 `.lingxirules`，追加到 system prompt 末尾（优先级高于通用指令）。20000 字上限。新对话 / 切项目 / 删当前会话 / 加载历史会话时都自动重读。顺手补了 `_delete_session` 里之前用裸 SYSTEM_PROMPT 丢角色卡 / 项目上下文的小 bug

### ✅ B. `run_command` 流式输出（已完成）
**问题**：原来跑 pytest / npm test / 长 build 必须等命令跑完才看输出。
**实现**：`subprocess.Popen` + reader 子线程读 stdout（stderr 合并），按行 push 到 UI（走 `state.ui_ref.show_message`，跨线程 Signal 安全）。stop_flag 触发后用 `taskkill /F /T /PID` 杀进程树（Windows 下 `shell=True` 必须杀树才能终止 child）。30s 硬超时同样走 taskkill。AI 端拿到完整输出（5000 字截断）；UI 上看到的是全量流式输出。`streaming.py:_execute_tool` 对 `STREAMING_TOOLS` 跳过最终 display，避免重复
**关键修复**：Windows `proc.kill()` 只杀 cmd.exe shell 父进程，child 继续跑（中断 ≈ 没效果）。改用 `taskkill /F /T`，中断耗时从 5s 降到 0.6s

### ✅ C. per-tool 自动批准白名单（已完成）
**问题**：旧版"允许并记住"只对**完全相同的字符串**生效，`git status` 和 `git status --short` 要分别授权
**实现**：把第 2 项改为**前缀白名单**——从命令里抽出 base（首词 + path basename），加入 `_session_command_prefix_allowlist`。下次该 base 开头的任意命令都自动放行
**关键防御**：危险命令（rm -rf / format / sudo / drop table 等）**永远不被前缀白名单绕过**——比如即使预先信任了 `cd`，`cd foo && rm -rf /` 仍然要弹卡片，因为 destructive 检查在前缀检查之前
**例**：用户对 `git status` 选"信任所有 `git` 类命令" → 之后 `git diff` / `git log` / `git fetch` 全部秒过
**UI**：第 2 项文案按当前命令的 base 动态拼，例如 `信任所有 \`git\` 类命令（本次会话不再询问）`

### ✅ D. Plan / Act mode（已完成）
**实现**：state 加 `agent_mode`（"act" / "plan"）；顶栏加切换按钮 `_toggle_agent_mode`；
get_system_prompt 在 plan 模式追加强制提示（只读、列方案）；_execute_tool 在 plan 模式
对非只读工具硬拦截，把拒绝信息回灌给 AI（双层保护：软提示 + 硬拦截）
**只读白名单**：`read_file` / `list_directory` / `search_in_file` / `search_files`

### ✅ E. Diff preview before edit（已完成）
**问题**：原 edit_file 直接写盘，AI 改坏了用户事后才发现
**实现**：worker 线程在 edit_file 内部算 unified diff → 通过 SignalBridge.edit_confirm_request
投到 UI 主线程弹**新内联卡片**（蓝色调，跟红色命令卡视觉区分）→ 用户审 diff 后选
"允许此次 / 信任此文件后续 / 拒绝"。会话级 `_session_edit_path_allowlist` 让"信任此文件"后
后续 edit 秒过

### ✅ F. Checkpoint / Undo（已完成）
**实现**：新建 `src/checkpoint.py` 模块。edit_file / write_file / append_file 写盘前自动
`git stash push -u + apply` 打快照（保留 stash ref + 不影响工作区）；非 git 项目静默 fallback。
顶栏加 `↶ 撤销` 按钮，调 `undo_last_checkpoint()` → `git checkout stash@{N} -- <path>`
做**路径级**恢复（不影响 AI 改的其它文件 / 用户后续手动改动）。栈最多保留 50 个 checkpoint
**已知限制**：非 git 项目无 checkpoint；git checkout 路径级恢复在 stash 期间该文件被删的极端
场景下可能失败

### G. MCP server 支持（1-2 天，⭐ 长期，暂缓）
重大工程，Cline 的护城河之一。但当前内置工具（read/write/edit/search/run_command/
generate_image）已覆盖日常 coding，MCP 接入成本高、收益边际，放到有明确需求时再做。

> A–F 已全部完成（见上方各 ✅ 条目）。coding 三件套 + Plan/Act + diff 确认 + Checkpoint
> + prompt caching 都已上线，灵犀已能在中等项目里实际改代码。

---

## 🚢 发布前清单（v1.0 之前必须）

- [ ] 装 PyInstaller：`python -m pip install pyinstaller`
- [ ] 跑一次打包：`python -m PyInstaller lingxi.spec`，确认 `dist/灵犀/灵犀.exe` 能正常启动
- [ ] 手动测主流程（前面 audit 里"我还没真正跑过"那条）：
  - 启动 → 主窗口出现
  - 切角色卡 → 系统提示词正确切换
  - 切项目 → 输入框下方指示条更新
  - 发消息 → AI 正常流式回复
  - 让 AI 跑 `dir` → 内联确认卡出现 → 点允许 → 输出在项目目录里
  - 让 AI 改文件 → 用 `edit_file` 精确替换
  - 按 F12 → Debug Inspector 出现并显示请求/响应
  - 关窗 → 确认对话框出现 → 选最小化 → 系统托盘还在、双击可唤起
- [ ] 跑剩下 P0 里的 #24/#25/#26/#27（拖拽截断 / 危险正则 / 内存累积 / 切模型剥图）—— 可选，介意才修
- [ ] `git add` 全部改动 + `git tag v1.0.0`
- [ ] 推 Release 时附上 `assets/screenshots/demo.gif`（按 README 顶部注释录一段 20s 演示）

---

## 📝 事实纠正

之前灵犀写的几份文档（已删）里有几个**事实错误**（凭印象写的，没真去读代码）：

1. **"停止后丢弃文本" — 错的**。看 [src/agent.py](../src/agent.py)，已经会保存中断时的 raw_text。只是没有"继续生成"按钮（见 P3 #17）。
2. **"模型参数全部硬编码" — 部分错**。`max_tokens=8192` 是有，但 temperature 等参数 LangChain 会用模型默认值。
3. **重复列同一项功能**（消息复制在三份文档里出现了 3 次）。
4. **Ctrl+V 粘贴图片其实早已实现**（我自己也犯了同样的错没核实就列上去），见 [src/ui/chat_window.py](../src/ui/chat_window.py) 的 `eventFilter`。
