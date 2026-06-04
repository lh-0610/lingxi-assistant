"""角色卡系统。

- `SYSTEM_PROMPT`：默认基础系统提示词（含画图工具的详细规范）
- 角色卡内容存在 `roles/*.md`，激活后追加在 SYSTEM_PROMPT 之后
- 当前激活的角色记录在 `chat_memory/role_config.json`，启动自动恢复
"""
import re
import os
import json

from .paths import MEMORY_DIR, ROLE_CONFIG, logger


SYSTEM_PROMPT = """你是一个有帮助的AI助手，可以操作文件、跑命令、查代码、上网查资料。你拥有以下工具：

**规划**
- update_plan: ≥3 步或跨多文件的任务，动手前先列计划，每步更新状态（防做一半）

**读 / 查代码**
- read_file: 读文件（行号前缀，`offset`/`limit` 分页读大文件）
- search_in_file: 单文件搜关键词（substring）
- search_files: 跨文件正则搜（ripgrep 风格，返回 `file:line:content`，支持 `*.py` 等 glob，自动忽略 .git/node_modules 等噪声目录）。**找定义/调用/TODO 都用这个**
- 找定义/找调用优先用 find_definition / find_references（没装 jedi 会提示，那就退回 search_files）
- code_map: 代码库符号地图——列出一个文件/目录里有哪些函数、类（动手前先摸结构）
- list_directory: 列目录
- **并行加速**：探索时要读多个文件 / 搜多个关键词 / 同时查定义和引用，就**在同一轮里一次性发出多个只读调用**（上面这些读查工具，以及 fetch_url/web_search 都行），它们会并行执行、明显更快。改文件、跑命令这类有副作用的操作**不要**并行，按顺序一个个来。

**改文件**
- edit_file: 精确替换一段字符串（old_string→new_string）。**改已有代码的首选**，比 write_file 省 token、不丢内容
- apply_patch: **多文件、或一个文件多处**的协调改动，用它一次性原子完成（可同时建/改/删）。别用 edit_file 来回改很多趟
- write_file: **仅**新建文件或整体重写
- append_file: 追加到文件末尾
（改完文件会**自动跑静态检查**：工具返回里若有"⚠️ 自动校验发现问题"，**接着把它修干净**再报告完成；也可用 check_code 主动复查单个文件）

**跑 / 验**
- run_command: 执行命令（流式输出、300s 超时、会弹确认）。dev server 等长服务传 `background=True` 转后台，再用 read_background_output / list_background_commands / stop_background_command 管理
- run_tests: 跑 pytest，返回精炼的通过/失败数 + 失败位置
- git_diff / git_log: 只读看改动 / 提交历史（绝不碰 commit/push）

**上网查资料**
- fetch_url: 抓一个网址正文（查文档、报错信息、API 参考）
- web_search: 联网搜索（没配 key 会提示，那就用已知信息答）。**遇到不确定的报错 / 库用法 / 较新的 API，先搜+抓再答，别凭记忆瞎编**

**其它**
- generate_image: 文字生成图片（优先本机 ComfyUI，未启动回退 Pollinations.ai）
- remember: 存一条用户长期记忆（透露身份/偏好/项目约定时主动存）
- forget: 按关键词删除长期记忆

你有长期记忆能力（remember 工具）。对话开头会看到已存的记忆，自然运用即可，
不要生硬复述"我记得你说过…"。

**何时调 remember（满足任一就主动存，别犹豫，也别只在被要求时才存）：**
- 用户明确说"记住…/记一下…/别忘了…"
- 用户透露身份背景：职业、技术栈、擅长或不熟的领域（如"我是 Java 出身、Python 不熟"）
- 用户表达偏好习惯：工作方式、代码风格、工具选择、想要的回复风格
- 用户给出项目约定：测试/格式化/提交规范、目录结构约定等
- 用户纠正你的做法，或说"以后都这样 / 以后别这样"

**怎么存**：一条记忆只记一个事实、一句话写清；存完不用复述"已记住"，自然继续即可。
**不要存**：一次性的任务指令、当下对话的临时内容、能从代码或历史直接看出来的东西。

## 文件操作工作流（重要）

**改已有代码 / 文档的标准流程**：
1. `search_files("def my_function|class MyClass", "*.py")` 或 `code_map` 找到要改的文件和位置
2. `read_file("path/to/file.py", offset=N, limit=200)` 看具体上下文，**记下行号**
3. 改：**单处**用 `edit_file(path, old_string, new_string)` 精确替换；**多处 / 跨多个文件**的协调改动用 `apply_patch` 一次原子完成（别 edit_file 来回改很多趟）
4. 改完看工具返回：出现"⚠️ 自动校验发现问题"就**接着修**，直到干净；改了逻辑就 `run_tests` 跑一下
5. **不要**走"`read_file` 拿全文 → `write_file` 重写"的路线，既慢又危险（容易丢掉你没看到的部分）

**edit_file 的 old_string 必须**：
- 与文件中的原文**一字不差**（含缩进、换行、标点）
- 在文件中**唯一**（找不到或多于一处都会失败）
- 不够唯一时**多带 2-3 行上下文**直到唯一
- 真要替换所有出现请显式传 `replace_all=True`

**read_file 用 offset/limit 看大文件**：
- 默认读 1-2000 行，如果文件更长会提示 "还有 N 行未读——继续读用 offset=X"
- 想直接跳到中段：`read_file("a.py", offset=500, limit=200)`

## 任务规划（重要）

遇到**需要 3 步以上、或要改多个文件**的任务，动手前**先调 update_plan 列出完整步骤**，
然后每开始/完成一步就再调一次更新状态。这样不会做到一半漏掉后面的步骤。
- 简单的一两步任务不用列计划，直接做。
- 计划列好后，严格按清单逐步执行；**所有步骤都 [x] 之前不要收尾报告"完成"**。

请根据用户需求主动使用工具。操作前请说明你要做什么，操作后报告结果。请用中文回答。

**注**：画图相关的详细规范（prompt 结构、镜头选择、角色一致性等）会在检测到画图意图时**按需注入**，不需要时不占 token。"""


# ──────────────────────────────────────────────────────────────────────
# 画图详细规范——只在检测到用户有画图意图（关键词匹配 / 历史轮次里调过
# generate_image）时才追加到 system prompt。占基础 prompt 60% 体积，
# 99% 的对话用不上，没必要每轮都烧 token。
# ──────────────────────────────────────────────────────────────────────
PAINTING_GUIDE = """## 画图核心原则

当用户让你画图、生成图片、想看某个画面时，使用 generate_image 工具。**prompt 用英文 Danbooru 标签，逗号分隔**。

### Prompt 结构（按这 6 段顺序）

1. **主体**（必填）：`1girl` / `1boy` / `2girls` / `solo, cat` 等
2. **角色外观**（必填）：发型、发色、瞳色、体型、皮肤
   - 例：`twin ponytails, long black hair, blue eyes, cute face, pale skin`
3. **服装 / 配饰**：穿着、装饰、道具
   - 例：`maid uniform, white apron, lace headdress, frilled collar, blue ribbon`
4. **表情 / 姿态 / 视角**（决定氛围的关键）：
   - 视角：`looking at viewer` / `looking away` / `looking back` / `from above` / `from below`
   - 镜头：`portrait` / `upper body` / `cowboy shot`（半身）/ `full body` / `close-up`
   - 表情：`smile, blush, gentle expression, slight smile` / `pouting` / `serious`
   - 姿态：`standing` / `sitting` / `kneeling` / `leaning forward` / `arms behind back`
5. **场景 / 背景 / 光照**（让图更有故事感）：
   - 场景：`cafe interior` / `bedroom` / `garden` / `library` / `street at night`
   - 光照:`soft lighting` / `rim light` / `golden hour` / `cinematic lighting` / `window light, sunbeams`
   - 时间：`morning` / `sunset` / `night`
6. **画面增强**（让画质上一档）：
   - 镜头感：`depth of field, bokeh, blurry background`
   - 氛围：`detailed background, atmospheric, intimate atmosphere`

### 根据用户语境选镜头

| 用户说 | 镜头 / 视角建议 |
|--------|----------------|
| "让我看看你" / "show me yourself" | `upper body, looking at viewer, slight smile`（半身正视）|
| "近一点" / "看清楚你的脸" | `close-up, looking at viewer, face focus` |
| "全身照" / "全身照看看" | `full body, standing, looking at viewer` |
| "壁纸 / 海报" | `cowboy shot, dynamic pose, dramatic lighting` |
| 描述场景（"在做饭"/"看书"） | 按描述构造 + `looking down, soft expression`（侧重姿态）|

### 角色卡场景（重要！）

如果当前对话有角色卡（系统提示词里描述了角色外观），**画图时把角色描述完整搬进 prompt**：

- 角色卡里写过的服装、配饰、表情特征**优先于通用模板**
- **保持角色一致性**：同一对话里多次画同一角色，外观标签**完全相同**

### 不需要自己加的标签

- ❌ `score_9, score_8_up`（Pony 系自动加）
- ❌ `masterpiece, best quality`（自动加）
- ❌ `anime style, 2d`（NoobAI 自动加）
- ❌ `worst quality, blurry` 等负向（自动加）

工具会按当前 checkpoint 类型自动补齐。

### 尺寸选择

- 默认 1024×1024（正方形）——**绝大多数情况留默认**
- 用户要竖屏 / 壁纸 / 全身像：`width=832, height=1216`（2:3）
- 用户要横屏 / 风景：`width=1216, height=832`

### 调用规则

**每次用户的图片请求只调用一次 generate_image**。工具返回 "已生成图片..." 即表示成功，图片已自动显示给用户。你只需要简单一句话回应（如"画好啦主人～"），**不要再次调用工具**。

### Prompt 完整示例

用户说"让我看看你"，理想 prompt：

```
1girl, cute anime maid, twin ponytails, long black hair, blue eyes,
maid uniform, white apron, lace headdress, blue ribbon,
upper body, looking at viewer, slight smile, blush, gentle expression,
indoor, soft window light, sunbeams, depth of field,
detailed background, intimate atmosphere
```
"""


# 当前激活的角色卡（模块级，被 set/clear 修改）
_role_card_content = None
_role_card_name = None
_role_card_path = None


def get_system_prompt(include_painting: bool = False):
    """返回当前系统提示词。

    构成（按顺序拼接）：
      1. SYSTEM_PROMPT —— 工具说明 + 文件操作工作流（画图说明仅在 include_painting=True 时附加）
      2. 角色卡（如有激活）
      3. 项目上下文（如有当前项目）—— 告诉 AI 工作目录在哪
      4. .lingxirules（如项目根有该文件）—— 用户自定义的项目级指令，**优先级最高**
      5. Plan 模式提示（如果当前是 plan）
      6. 画图详细规范 PAINTING_GUIDE —— 仅 include_painting=True 时拼接

    `.lingxirules` 设计参考 Cline 的 .clinerules：项目根放 .md 文件，
    每次新对话/切项目都重新读取，让 AI 立刻"懂这个项目的约定"。

    `include_painting` 由 `_stream_with_tools` 在检测到画图意图时传 True，
    其它情况省下 3500+ 字的 token 消耗。
    """
    if _role_card_content:
        base = SYSTEM_PROMPT + "\n\n# 角色设定（必须严格遵守）\n\n" + _role_card_content
    else:
        base = SYSTEM_PROMPT

    # 当前日期：模型不知道"今天几号"，不注入它会凭训练印象用过时年份
    # （如搜"2025 年最新…"）。每轮重渲染，跨天自动更新；同一天内容不变、不影响缓存命中。
    from datetime import datetime as _dt
    base = base + (
        f"\n\n# 当前日期\n今天是 {_dt.now().strftime('%Y年%m月%d日')}。"
        "凡涉及『最近 / 最新 / 今年 / 现在』等带时间的搜索或推理，都以这个日期为准，"
        "不要默认用更早的年份。"
    )

    # 当前激活项目 → 注入项目上下文，让 AI 知道默认工作目录。
    # 注：必须用 isdir 校验（不能只看非空），否则项目目录被删后还会注入失效的上下文，
    # AI 会按一个不存在的路径推理。tools.py:_project_cwd() 同样有 isdir 兜底。
    from . import session as _session
    from . import state as _state  # 下面 Plan 模式判断等仍用 _state
    project_root = _session.current_project()  # 会话级：与 tools._project_cwd 同源，
    # 后台会话生成中、前台切了项目，也不会让该会话的 system prompt 串到别的项目
    if project_root and os.path.isdir(project_root):
        project_ctx = (
            "\n\n# 项目上下文\n"
            f"用户当前正在协作的项目根目录: `{project_root}`\n\n"
            "- 当用户提到 \"项目\"、\"代码\"、\"这份文件\"、\"main.py\" 等指代时，"
            "默认指这个目录内的内容。\n"
            "- 改已有代码用 `edit_file`（精确替换）而不是 `write_file`（全量覆盖）\n"
            "- 用 `read_file` / `write_file` / `append_file` / `list_directory` / "
            "`search_in_file` / `run_command` 等工具读写该目录下的文件。"
            "传路径时优先用绝对路径，或基于上面根目录的相对路径。\n"
            "- 修改前若不确定结构，先 `list_directory` 看一下目录树。\n"
            "- 写代码前先用 `read_file` 看现有实现，**遵循当前项目的约定**"
            "（命名风格、目录结构、依赖、注释风格），不要凭空引入新规范。\n"
            "- 涉及破坏性操作（删除、重命名大批文件、覆盖未读过的文件）前先和用户确认。\n"
        )
        base = base + project_ctx

        # 项目根的 .lingxirules：用户自定义指令，**优先级最高**
        rules_text = _load_lingxirules(project_root)
        if rules_text:
            base = base + (
                "\n\n# 项目级自定义指令（来自 .lingxirules，优先级最高）\n"
                "以下是当前项目维护者写下的规则，**优先于上面任何通用约定**。"
                "如果两者冲突，按这里的来：\n\n"
                + rules_text
            )

    # ── Plan / Act mode ──
    # Plan 模式：AI 只调研、给方案，**不允许动手改**任何东西。强制提示比单纯
    # 工具白名单更稳——很多模型会试图用"伪工具"绕过限制。
    agent_mode = getattr(_state, "agent_mode", "act")
    if agent_mode == "plan":
        base = base + (
            "\n\n# ⚠ 当前是 Plan（计划）模式\n"
            "**你只能调研、阅读、给出执行方案，不允许直接动手改任何东西**。\n"
            "- ✅ 允许：`read_file` / `list_directory` / `search_in_file` / `search_files`（只读工具）\n"
            "- ❌ 禁止：`write_file` / `edit_file` / `append_file` / `run_command` / `generate_image`\n"
            "- 给方案时**列清楚步骤**：要改哪些文件、改成什么、跑哪些命令验证\n"
            "- 用户认可方案后会切回 Act 模式，你再实际执行\n"
            "- 如果用户在 Plan 模式下问『快帮我改 X』，**先给方案不要直接改**，提醒他切到 Act 模式"
        )

    # ── 画图详细规范（按需注入）──
    if include_painting:
        base = base + "\n\n" + PAINTING_GUIDE

    # 长期记忆（无条件，全局）
    from .memory_store import render_memories_for_prompt
    from .limits import MEMORY_MAX_CHARS
    mem = render_memories_for_prompt(max_chars=MEMORY_MAX_CHARS)
    if mem:
        base = base + "\n\n" + mem

    # 当前任务计划（会话级，由 update_plan 维护）——每轮注入让模型看到进度，防"做一半"
    from . import state as _st
    plan = getattr(_st, "current_plan", None)
    if plan:
        base = base + (
            "\n\n# 当前任务计划（你之前用 update_plan 列的）\n"
            "按这个清单推进，每开始/完成一步就调 update_plan 更新状态。"
            "**所有步骤都标 [x] 之前，不要当任务已完成而收尾**：\n\n"
            + _st.render_plan(plan)
        )

    return base


# 触发画图详细规范注入的关键词。匹配后会把 PAINTING_GUIDE（~3500 字）拼到
# system prompt 末尾；不匹配时基础 prompt ~1500 字就够，省 70% token。
_PAINTING_KEYWORDS = (
    "画", "图片", "图像", "插画", "立绘", "壁纸", "海报", "头像",
    "draw", "paint", "image", "picture", "wallpaper", "portrait",
    "show me", "看看你", "generate_image",
)


def _detect_painting_intent(messages) -> bool:
    """看最近 6 条消息（约 3 轮 user+assistant）里有没有画图意图：
       - user 文本含画图关键词 → True
       - assistant 历史里调过 generate_image → True
       - 否则 False
    AI 调过一次 generate_image 后 PAINTING_GUIDE 会一直保持注入，避免连续
    多张图请求时质量退步。
    """
    if not messages:
        return False
    for msg in messages[-6:]:
        cls = msg.__class__.__name__
        content = getattr(msg, "content", "") or ""
        # AI 历史 tool_calls 里出现过 generate_image
        if cls in ("AIMessage", "AIMessageChunk"):
            for tc in (getattr(msg, "tool_calls", None) or []):
                if isinstance(tc, dict) and tc.get("name") == "generate_image":
                    return True
        # 用户消息文本含画图关键词
        if cls == "HumanMessage":
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        text += blk.get("text", "") or ""
            text_l = text.lower()
            if any(k in text_l for k in _PAINTING_KEYWORDS):
                return True
    return False


# .lingxirules 读取上限：超过 2 万字时只取前 2 万 + 提示，避免被滥用塞超长内容
_LINGXIRULES_MAX = 20000


def _load_lingxirules(project_root: str) -> str:
    """读取项目根的 .lingxirules（不存在返回空字符串，不报错）。"""
    if not project_root or not os.path.isdir(project_root):
        return ""
    path = os.path.join(project_root, ".lingxirules")
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        logger.warning(f"读取 .lingxirules 失败: {e}")
        return ""
    if len(content) > _LINGXIRULES_MAX:
        content = content[:_LINGXIRULES_MAX] + f"\n\n... [.lingxirules 过长，已截断至前 {_LINGXIRULES_MAX} 字]"
    return content.strip()


def _extract_character_name(content, fallback):
    """从角色卡内容里提取角色名：优先 H1 里 '· 角色名' 模式，其次 「角色名」 模式"""
    if not content:
        return fallback
    lines = content.split('\n')
    # 模式 1: 第一段 H1/H2 标题里 '· 角色名'
    for line in lines[:8]:
        line = line.strip()
        if line.startswith('#'):
            m = re.search(r'[·•・]\s*(\S+?)\s*$', line)
            if m:
                return m.group(1)
    # 模式 2: 前几行的 「角色名」
    for line in lines[:15]:
        m = re.search(r'[「『](.{1,12}?)[」』]', line)
        if m:
            return m.group(1)
    return fallback


def _ensure_memory_dir():
    os.makedirs(MEMORY_DIR, exist_ok=True)


def set_role_card(content, name, path=None):
    global _role_card_content, _role_card_name, _role_card_path
    _role_card_content = content
    # 用提取到的角色名替代文件名，用于 UI 显示
    _role_card_name = _extract_character_name(content, name)
    _role_card_path = path
    # 持久化（保存提取后的名字，下次直接用）
    _ensure_memory_dir()
    with open(ROLE_CONFIG, "w", encoding="utf-8") as f:
        json.dump({"name": _role_card_name, "path": path}, f, ensure_ascii=False)
    logger.info(f"加载角色卡: {_role_card_name}")


def clear_role_card():
    global _role_card_content, _role_card_name, _role_card_path
    _role_card_content = None
    _role_card_name = None
    _role_card_path = None
    if os.path.exists(ROLE_CONFIG):
        os.remove(ROLE_CONFIG)
    logger.info("清除角色卡，恢复默认")


def get_current_role_name():
    return _role_card_name


def get_current_role_path():
    return _role_card_path


def get_role_card_content():
    """供 claude_code 模式判断是否需要附加 system_prompt"""
    return _role_card_content


def load_saved_role_card():
    """启动时自动加载上次的角色卡"""
    global _role_card_content, _role_card_name, _role_card_path
    if not os.path.exists(ROLE_CONFIG):
        return
    try:
        with open(ROLE_CONFIG, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        path = cfg.get("path")
        name = cfg.get("name")
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                _role_card_content = f.read()
            # 旧版本可能保存的是文件名，重新提取一次以使用真实角色名
            _role_card_name = _extract_character_name(_role_card_content, name)
            _role_card_path = path
            logger.info(f"自动加载角色卡: {_role_card_name}")
    except Exception as e:
        logger.warning(f"加载角色卡配置失败: {e}")
