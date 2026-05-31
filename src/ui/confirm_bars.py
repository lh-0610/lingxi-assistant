"""edit_file / run_command 内联确认条（mixin for ChatUI）。

从 chat_window.py 抽出来的两个对称子系统：

- run_command 确认条：worker 线程要执行命令时弹卡片让用户允许/拒绝，配套
  危险命令正则、base 命令前缀白名单
- edit_file diff 预览条：worker 线程要改文件时弹 diff 让用户审，配套路径白名单

两者都是 Signal → 主线程弹卡 → worker `done.wait()` 同步等待 的模式。

手机遥控模式下，同时把确认推到 Telegram inline 按钮（双向确认，先点先到）。

作为 ChatUI 的 mixin 接入。依赖宿主提供：
- `self._t(key)` 主题色查表
- `self._svg_icon(filename, color)` 单色 SVG 图标
- `self.bridge.confirm_request` / `edit_confirm_request` 跨线程 Signal
- `self._session_command_allowlist` / `_session_command_prefix_allowlist`
  / `_session_edit_path_allowlist` 三个会话级白名单 set（在 ChatUI.__init__ 里建）
- QMainWindow 的 `show / raise_ / activateWindow`
"""
import threading

from .. import state as _state
from .. import telegram_push
from ..config import REMOTE_TELEGRAM_CONFIRM
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTextBrowser, QVBoxLayout, QWidget,
)


# ---------------------------------------------------------------------------
# 手机 Telegram 遥控确认注册表
# ---------------------------------------------------------------------------
# key = confirm_id, value = {"result": dict, "done": threading.Event, "msg_id": int|None}
_pending_remote_confirms: dict = {}
_pending_lock = threading.Lock()
_confirm_counter = [0]


def _new_confirm_id() -> str:
    _confirm_counter[0] += 1
    return str(_confirm_counter[0])


def _resolve_remote_confirm(cid: str, allow: bool, remember: bool = False) -> bool:
    """从手机端 callback_data 解析操作确认。

    allow: 是否允许操作
    remember: 允许时是否"记住同类"（对命令：加入白名单；对编辑：加入路径白名单）
    返回 True 表示找到并处理，False 表示已过期/找不到。
    answerCallbackQuery 由调用方负责，这里不调。
    """
    with _pending_lock:
        entry = _pending_remote_confirms.pop(cid, None)
    if entry is None:
        return False
    # 只有 done 还没被设置时才写入结果（PC 端可能已经先处理了——先点先到）
    won = not entry["done"].is_set()
    if won:
        entry["result"]["allow"] = allow
        entry["result"]["by_remote"] = True  # 标记本次由手机决出，worker 清理时不再覆盖文案
        if remember:
            entry["result"]["remember"] = True
        # 手机端明确拒绝 = 停掉本次生成（与 PC 端 _resolve_command_confirm 行为一致），
        # 否则远程 agent 被拒后会继续往下试别的、反复弹手机确认
        if not allow:
            _state.stop_flag = True
        # 让主线程隐藏可能还挂着的 PC 确认卡（仅 UI，不碰 result/done）。
        # 必须在 done.set() 之前 emit：dismiss 先入主线程队列，worker 唤醒后即便
        # 立刻弹下一张卡，FIFO 也保证 dismiss 先处理、不会误清掉新卡。
        ui = getattr(_state, "ui_ref", None)
        if ui is not None and hasattr(ui, "bridge"):
            try:
                ui.bridge.dismiss_confirm.emit()
            except Exception:
                pass
    entry["done"].set()
    msg_id = entry.get("msg_id")
    if msg_id:
        if allow and remember:
            label = "✅ 已记住同类并允许"
        elif allow:
            label = "✅ 已允许"
        else:
            label = "❌ 已拒绝"
        telegram_push.edit_message_text(msg_id, label)
    return True


class ConfirmBarsMixin:
    """两个内联确认条的全部 UI + 状态机 + worker 同步逻辑。"""

    # ══════════════════════════════════════
    # run_command 确认条
    # ══════════════════════════════════════

    def _build_command_confirm_bar(self):
        """AI 调 run_command 时显示在输入框上方的内联确认卡片。

        样式参考 Claude Code CLI：单卡内含标题、命令预览、3 个堆叠的选项行
        （1/2/3 数字快捷键），整体看起来"一体"。第 2 项"允许并记住"只在
        非危险命令下显示，避免给 AI 永久授权后被 rm -rf。
        """
        bar = QFrame()
        bar.setObjectName("commandConfirmBar")
        bar.setVisible(False)
        bar.setFixedWidth(920)  # _resize_input_container 会同步

        v = QVBoxLayout(bar)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # ── 顶部：标题 + 命令预览（同一块 padding） ──
        top = QWidget()
        top_v = QVBoxLayout(top)
        top_v.setContentsMargins(18, 14, 18, 14)
        top_v.setSpacing(10)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        self._cmd_confirm_icon = QLabel()
        self._cmd_confirm_icon.setFixedSize(16, 16)
        self._cmd_confirm_title = QLabel("允许执行此命令？")
        self._cmd_confirm_title.setObjectName("commandConfirmTitle")
        title_row.addWidget(self._cmd_confirm_icon)
        title_row.addWidget(self._cmd_confirm_title, 1)
        top_v.addLayout(title_row)

        self.command_confirm_text = QTextBrowser()
        self.command_confirm_text.setObjectName("commandConfirmText")
        self.command_confirm_text.setMinimumHeight(120)
        self.command_confirm_text.setMaximumHeight(280)
        self.command_confirm_text.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.command_confirm_text.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.command_confirm_text.setOpenExternalLinks(False)
        cmd_font = QFont("Consolas")
        cmd_font.setPixelSize(12)
        self.command_confirm_text.setFont(cmd_font)
        top_v.addWidget(self.command_confirm_text)

        v.addWidget(top)

        # ── 反馈输入框（拒绝时说明原因，AI 据此调整） ──
        self.command_confirm_feedback = QLineEdit()
        self.command_confirm_feedback.setObjectName("commandConfirmFeedback")
        self.command_confirm_feedback.setPlaceholderText(
            "可选：说明该怎么改，AI 会据此调整；留空 = 直接拒绝")
        self.command_confirm_feedback.setMinimumHeight(30)
        # 在反馈框里按 Enter = 直接拒绝并把反馈带给 AI
        self.command_confirm_feedback.returnPressed.connect(
            lambda: self._resolve_command_confirm(False))
        v.addWidget(self.command_confirm_feedback)

        # ── 选项行：每行一个全宽按钮 ──
        def _make_option_row(num: str, label: str, object_name: str, on_click):
            btn = QPushButton()
            btn.setObjectName(object_name)
            btn.setCursor(Qt.PointingHandCursor)
            # 用 layout 把"序号 + 文字"排进按钮，比 setText 灵活
            btn.setMinimumHeight(40)
            row = QHBoxLayout(btn)
            row.setContentsMargins(20, 0, 20, 0)
            row.setSpacing(14)
            num_lbl = QLabel(num)
            num_lbl.setObjectName("commandConfirmOptNum")
            num_lbl.setFixedWidth(14)
            text_lbl = QLabel(label)
            text_lbl.setObjectName("commandConfirmOptText")
            row.addWidget(num_lbl)
            row.addWidget(text_lbl, 1)
            btn.clicked.connect(on_click)
            return btn, text_lbl

        self._cmd_allow_btn, self._cmd_allow_label = _make_option_row(
            "1", "允许执行", "commandConfirmAllowRow",
            lambda: self._resolve_command_confirm(True, remember=False),
        )
        # 第 2 项的文案是模板，_on_confirm_request 时会按实际命令的 base 替换占位
        # （比如 "信任所有 `git` 类命令（本次会话）"）
        self._cmd_remember_btn, self._cmd_remember_label = _make_option_row(
            "2", "信任所有同类命令（本次会话不再询问）", "commandConfirmRememberRow",
            lambda: self._resolve_command_confirm(True, remember=True),
        )
        self._cmd_deny_btn, self._cmd_deny_label = _make_option_row(
            "3", "拒绝", "commandConfirmDenyRow",
            lambda: self._resolve_command_confirm(False),
        )

        v.addWidget(self._cmd_allow_btn)
        v.addWidget(self._cmd_remember_btn)
        v.addWidget(self._cmd_deny_btn)

        # ── 底部提示行 ──
        self._cmd_confirm_hint = QLabel("1 / 2 / 3 选择 · Esc 取消")
        self._cmd_confirm_hint.setObjectName("commandConfirmHint")
        self._cmd_confirm_hint.setAlignment(Qt.AlignCenter)
        hint_wrap = QWidget()
        hint_wrap_l = QVBoxLayout(hint_wrap)
        hint_wrap_l.setContentsMargins(0, 8, 0, 10)
        hint_wrap_l.addWidget(self._cmd_confirm_hint)
        v.addWidget(hint_wrap)

        # 等待回调的状态
        self._command_confirm_result_holder = None
        self._command_confirm_done_event = None
        # 当前请求是否危险命令（决定第 2 行显示与否）
        self._command_confirm_destructive = False

        # 装事件过滤器接 1 / 2 / 3 / Esc 键
        bar.installEventFilter(self)
        bar.setFocusPolicy(Qt.StrongFocus)

        self.command_confirm_bar = bar
        self._style_command_confirm_bar()

    def _style_command_confirm_bar(self):
        """根据当前主题刷确认卡片配色 + 图标。_apply_theme 里调。"""
        if not hasattr(self, "command_confirm_bar"):
            return
        accent = self._t("ai_label")  # 跟 AI 名牌一致的强调色
        self._cmd_accent = accent  # 存起来给 HTML 格式化用
        divider = self._t("input_border")
        text = self._t("text")
        text_dim = self._t("text_dim")
        # 选项 hover 背景：在 input_bg 和 history_hover_bg 之间挑个能看出来的
        hover_bg = self._t("history_hover_bg")

        self.command_confirm_bar.setStyleSheet(
            # 卡片整体：圆角 + 单边框
            f"QFrame#commandConfirmBar {{"
            f"  background: {self._t('input_bg')};"
            f"  border: 1px solid {accent};"
            f"  border-radius: 12px;"
            f"}}"
            # 标题
            f"QLabel#commandConfirmTitle {{"
            f"  color: {accent}; font-size: 14px; font-weight: 600;"
            f"  letter-spacing: 0.3px; background: transparent;"
            f"}}"
            # 命令预览（等宽 + 内嵌灰底盒子）
            f"QTextBrowser#commandConfirmText {{"
            f"  background: {self._t('md_pre_bg')};"
            f"  color: {self._t('md_pre_text')};"
            f"  border: 1px solid {divider};"
            f"  border-radius: 8px; padding: 10px 14px;"
            f"  font-family: Consolas, 'Cascadia Code', 'Microsoft YaHei UI';"
            f"  font-size: 13px;"
            f"  white-space: pre-wrap;"
            f"}}"
            # 选项行：QPushButton 做全宽行，无 border、靠 hover bg 区分
            # 上方用 top border 当分隔线，营造"一体卡片"感
            f"QPushButton#commandConfirmAllowRow,"
            f"QPushButton#commandConfirmRememberRow,"
            f"QPushButton#commandConfirmDenyRow {{"
            f"  background: transparent;"
            f"  border: none;"
            f"  border-top: 1px solid {divider};"
            f"  border-radius: 0;"
            f"  text-align: left;"
            f"  padding: 0;"
            f"}}"
            f"QPushButton#commandConfirmAllowRow:hover,"
            f"QPushButton#commandConfirmRememberRow:hover,"
            f"QPushButton#commandConfirmDenyRow:hover {{"
            f"  background: {hover_bg};"
            f"}}"
            # 序号小 chip
            f"QLabel#commandConfirmOptNum {{"
            f"  color: {text_dim}; font-size: 12px; font-weight: 600;"
            f"  background: transparent;"
            f"}}"
            f"QLabel#commandConfirmOptText {{"
            f"  color: {text}; font-size: 13px;"
            f"  background: transparent;"
            f"}}"
            # 底部 hint
            f"QLabel#commandConfirmHint {{"
            f"  color: {text_dim}; font-size: 11px;"
            f"  background: transparent;"
            f"  border-top: 1px solid {divider};"
            f"  padding-top: 8px;"
            f"}}"
            # 反馈输入框：跟命令预览框同底色，聚焦时强调色描边
            f"QLineEdit#commandConfirmFeedback {{"
            f"  background: {self._t('md_pre_bg')};"
            f"  border: 1px solid {divider};"
            f"  border-radius: 6px; padding: 6px 9px;"
            f"  color: {text}; font-size: 12px;"
            f"  font-family: Consolas, 'Microsoft YaHei UI';"
            f"}}"
            f"QLineEdit#commandConfirmFeedback:focus {{ border-color: {accent}; }}"
        )
        # 终端图标
        icon = self._svg_icon("code_lucide.svg", accent)
        if not icon.isNull():
            self._cmd_confirm_icon.setPixmap(icon.pixmap(QSize(16, 16)))

    # ── command text HTML 格式化 ─────────────────────────────────────
    # 关键词高亮列表
    _CMD_KEYWORDS = (
        # 包管理
        "pip", "install", "uninstall", "upgrade", "npm", "yarn", "pnpm", "bun",
        "apt", "apt-get", "brew", "choco", "scoop", "winget", "pacman", "yum", "dnf",
        # Python / 运行时
        "python", "python3", "py", "uv", "poetry", "conda", "mamba", "pipenv",
        "pytest", "unittest", "mypy", "ruff", "black", "isort", "flake8", "pylint",
        # Node / JS
        "node", "npx", "ts-node", "tsx", "deno", "bun",
        # 版本控制
        "git", "svn", "hg",
        # 构建 / 部署
        "make", "cmake", "cargo", "go", "dotnet", "mvn", "gradle", "sbt",
        "docker", "docker-compose", "podman", "kubectl", "helm",
        # 系统关键
        "sudo", "su", "chmod", "chown", "mount", "umount", "systemctl", "service",
        "shutdown", "reboot", "kill", "killall", "pkill",
        # Shell
        "bash", "sh", "zsh", "fish", "powershell", "pwsh", "cmd",
        # 常见子命令
        "add", "remove", "run", "build", "test", "start", "stop", "restart",
        "push", "pull", "clone", "checkout", "merge", "rebase", "reset", "clean",
        "init", "create", "new", "generate",
    )
    # 需要高亮为"危险"的标志词
    _CMD_DANGER_WORDS = (
        "rm -rf", "rm -fr", "rmdir", "DROP", "DELETE", "FORMAT",
        "mkfs", "fdisk", "dd if=", ":(){", "shutdown", "reboot",
        "sudo rm", "sudo chmod 777", "--force", "-f",
    )

    def _format_command_html(self, command: str) -> str:
        """将命令字符串转换为带语法高亮的 HTML（用于 QTextBrowser）。"""
        import html as _html
        import shlex
        import re

        is_dark = getattr(self, "theme", "light") == "dark"
        if is_dark:
            accent        = "#7c5cbf"   # 命令名（首 token）
            kw_color      = "#c084fc"   # 关键字（命令名/工具名）
            danger_color  = "#f87171"   # 危险词（rm、format、dd 等）
            flag_color    = "#60a5fa"   # flag (-xxx)
            sep_color     = "#94a3b8"   # 分隔符 (|, &&, ;, > 等)
            path_color    = "#34d399"   # 路径 / URL
            str_color     = "#fbbf24"   # 引号字符串
            default_color = "#e2e8f0"   # 其它默认
        else:
            accent        = "#7c3aed"
            kw_color      = "#7e22ce"
            danger_color  = "#dc2626"
            flag_color    = "#1d4ed8"
            sep_color     = "#475569"
            path_color    = "#15803d"
            str_color     = "#b45309"
            default_color = "#1f2937"

        text = command.strip()

        # 检测是否为危险命令（整条命令级别）
        text_lower = text.lower()
        is_dangerous = any(dw.lower() in text_lower for dw in ConfirmBarsMixin._CMD_DANGER_WORDS)

        keywords_lower = {kw.lower() for kw in ConfirmBarsMixin._CMD_KEYWORDS}

        def _highlight_line(line: str) -> str:
            """对单行命令进行语法高亮。"""
            try:
                tokens = shlex.split(line)
            except ValueError:
                tokens = line.split()

            highlighted_parts: list[str] = []

            for i, tok in enumerate(tokens):
                tok_escaped = _html.escape(tok)
                tok_lower = tok.lower()

                line_break_after = False

                # ① 危险关键词（如 rm, -rf 等）
                if any(dw.lower() in tok_lower for dw in ("rm", "rmdir", "mkfs", "dd", "DROP", "DELETE", "FORMAT")):
                    color = danger_color
                    weight = "bold"
                # ② 管道 / 重定向 / 链接符
                elif tok in ("|", "&&", "||", ";", ">>", ">"):
                    color = sep_color
                    weight = "normal"
                    # 链接符后强制换行（重定向 >>/> 通常短，不换）
                    if tok in ("&&", "||", "|", ";"):
                        line_break_after = True
                # ③ flags（-xxx）
                elif tok.startswith("-"):
                    color = flag_color
                    weight = "normal"
                # ④ 关键词（命令名 + 工具名）
                elif tok_lower in keywords_lower or (i == 0 and tok_lower not in keywords_lower):
                    # 第一个 token 总是命令名
                    if i == 0:
                        color = accent
                        weight = "bold"
                    else:
                        color = kw_color
                        weight = "600"
                # ⑤ 路径 / URL（含 / 或 . 或 ~）
                elif ("/" in tok or tok.startswith("~") or
                      re.search(r'\.\w{1,5}$', tok) or "://" in tok):
                    color = path_color
                    weight = "normal"
                # ⑥ 引号字符串
                elif (tok.startswith('"') and tok.endswith('"')) or \
                     (tok.startswith("'") and tok.endswith("'")):
                    color = str_color
                    weight = "normal"
                else:
                    color = default_color
                    weight = "normal"

                highlighted_parts.append(
                    f'<span style="color:{color};font-weight:{weight}">{tok_escaped}</span>'
                )
                if line_break_after:
                    highlighted_parts.append("<br>")

            return " ".join(highlighted_parts)

        # 逐行处理，保留换行符（用 <br> 拼接，确保 QTextBrowser 正确渲染）
        lines = text.split("\n")
        highlighted_lines = [_highlight_line(line) for line in lines]
        result = "<br>".join(highlighted_lines)

        # 危险命令在开头加醒目警告
        if is_dangerous:
            result = (
                f'<span style="color:{danger_color};font-weight:bold">⚠ </span>'
                + result
            )

        return result

    def _on_confirm_request(self, command, result_holder, done_event):
        """UI 主线程槽：把命令灌进确认卡片 + 显示。点击时再唤醒 worker。

        - 危险命令（rm -rf / format / sudo 等）会隐藏"允许并记住"行，避免被
          AI 永久授权后造成数据损失
        - 旧请求未解时新请求会"取代"旧请求：deny 解阻塞旧 worker，再显示新卡
        - 卡片显示后强制 raise + activateWindow，避免被桌宠遮
        """
        if not hasattr(self, "command_confirm_bar"):
            result_holder["allow"] = False
            done_event.set()
            return
        if self._command_confirm_done_event is not None:
            # 旧请求被新的取代：先 deny 解阻塞旧 worker，再走新流程显示新卡
            try:
                if self._command_confirm_result_holder is not None:
                    self._command_confirm_result_holder["allow"] = False
                self._command_confirm_done_event.set()
            except Exception:
                pass
            self._command_confirm_result_holder = None
            self._command_confirm_done_event = None

        self.show()
        self.raise_()
        self.activateWindow()

        self._command_confirm_result_holder = result_holder
        self._command_confirm_done_event = done_event
        self._command_confirm_destructive = self._is_destructive_command(command)

        # 检测是不是 MCP / JSON dump 类的消息，用 <pre> 纯文本渲染，不走 shell 高亮
        if command.startswith("将调用 MCP") or command.startswith("将调用 mcp_"):
            import html as _html
            body_html = (
                '<pre style="font-family: Consolas, monospace; '
                'white-space: pre-wrap; word-wrap: break-word; '
                'margin: 0; font-size: 12px; line-height: 1.45;">'
                + _html.escape(command)
                + '</pre>'
            )
        else:
            body_html = (
                '<div style="white-space:pre-wrap;word-wrap:break-word;">'
                + self._format_command_html(command)
                + '</div>'
            )
        self.command_confirm_text.setHtml(body_html)
        # 危险命令：标题加警告 + 隐藏"信任所有同类命令"行
        if self._command_confirm_destructive:
            self._cmd_confirm_title.setText("⚠ 危险命令 · 是否允许？")
            self._cmd_remember_btn.setVisible(False)
            self._cmd_confirm_hint.setText("1 选择 · 3 拒绝 · Esc 取消")
        else:
            self._cmd_confirm_title.setText("允许执行此命令？")
            # 根据 base 命令动态拼文案，例如 "信任所有 `git` 类命令（本次会话）"
            base = self._extract_base_command(command)
            label = (
                f"信任所有 `{base}` 类命令（本次会话不再询问）"
                if base else "信任所有同类命令（本次会话不再询问）"
            )
            self._cmd_remember_label.setText(label)
            self._cmd_remember_btn.setVisible(True)
            self._cmd_confirm_hint.setText("1 / 2 / 3 选择 · Esc 取消")

        self.command_confirm_bar.setVisible(True)
        # 清空反馈输入框
        self.command_confirm_feedback.clear()
        # 注：手机确认推送由 confirm_command 的 push_confirm 接管（完整命令 + inline 按钮），
        # 不再额外发截断的"等待确认"文本通知（之前 command[:120] 会切断，且与按钮卡冗余）
        # 把焦点交给 bar 本身，1/2/3/Esc 由 eventFilter 接管
        self.command_confirm_bar.setFocus()

    def _resolve_command_confirm(self, allow: bool, remember: bool = False):
        """按钮点击：写结果、（必要时）把 base 命令加进前缀白名单、唤醒 worker、隐藏卡片。

        remember=True 仅在 allow=True 且命令非危险时生效——危险命令的第 2 行
        本来就是隐藏的，但加一道防御保险。
        """
        if self._command_confirm_done_event is None:
            return  # 已被处理过 / 状态被清

        feedback = self.command_confirm_feedback.text().strip()

        if not allow:
            # 只有纯拒绝（无反馈）才停掉本轮；有反馈则让 AI 据此调整
            if not feedback:
                _state.stop_flag = True

        if allow and remember and not self._command_confirm_destructive:
            base = self._extract_base_command(self.command_confirm_text.toPlainText())
            if base:
                self._session_command_prefix_allowlist.add(base)
                logger_log = getattr(self, "_logger", None)  # 不强依赖；只是 best-effort
                if logger_log:
                    try:
                        logger_log.info(f"加入前缀白名单: {base}")
                    except Exception:
                        pass

        self._command_confirm_result_holder["allow"] = allow
        self._command_confirm_result_holder["feedback"] = feedback
        self._command_confirm_done_event.set()
        self._command_confirm_result_holder = None
        self._command_confirm_done_event = None
        self.command_confirm_bar.setVisible(False)
        self.command_confirm_text.clear()
        self._command_confirm_destructive = False

    def _release_pending_confirm(self):
        """关窗 / 退出时唤醒任何挂在 confirm_command 上的 worker，避免无限挂起。
        当作"用户拒绝"处理，agent 收到 False 后会优雅结束这一轮工具调用。
        """
        if self._command_confirm_done_event is None:
            return
        try:
            if self._command_confirm_result_holder is not None:
                self._command_confirm_result_holder["allow"] = False
            self._command_confirm_done_event.set()
        except Exception:
            pass
        self._command_confirm_result_holder = None
        self._command_confirm_done_event = None
        if hasattr(self, "command_confirm_bar"):
            self.command_confirm_bar.setVisible(False)

    # ══════════════════════════════════════
    # edit_file diff 预览条
    # ══════════════════════════════════════

    def _build_edit_confirm_bar(self):
        """AI 调 edit_file 时弹的 diff 预览卡（结构跟 command_confirm_bar 对齐，复用样式）。

        默认隐藏；点 1=允许此次 / 2=信任此文件后续编辑 / 3=拒绝。
        diff 用 unified diff 格式，加号绿色 / 减号红色。
        """
        bar = QFrame()
        bar.setObjectName("editConfirmBar")
        bar.setVisible(False)
        bar.setFixedWidth(920)

        v = QVBoxLayout(bar)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # 顶部：标题 + 路径 + diff 预览
        top = QWidget()
        top_v = QVBoxLayout(top)
        top_v.setContentsMargins(18, 14, 18, 14)
        top_v.setSpacing(10)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        self._edit_confirm_icon = QLabel()
        self._edit_confirm_icon.setFixedSize(16, 16)
        self._edit_confirm_title = QLabel("准备修改文件")
        self._edit_confirm_title.setObjectName("editConfirmTitle")
        title_row.addWidget(self._edit_confirm_icon)
        title_row.addWidget(self._edit_confirm_title, 1)
        top_v.addLayout(title_row)

        self.edit_confirm_path = QLabel("")
        self.edit_confirm_path.setObjectName("editConfirmPath")
        self.edit_confirm_path.setWordWrap(True)
        self.edit_confirm_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        top_v.addWidget(self.edit_confirm_path)

        # diff 用 QTextBrowser 渲染（带行颜色）。
        # 给固定的最小/最大高度 + 强制竖向滚动条：否则在底部窄条里会被挤成几行、
        # 长 diff 看不全也滚不动。min 200 保证至少十几行可见，max 360 封顶（再长就滚）。
        self.edit_confirm_diff = QTextBrowser()
        self.edit_confirm_diff.setObjectName("editConfirmDiff")
        self.edit_confirm_diff.setMinimumHeight(200)
        self.edit_confirm_diff.setMaximumHeight(360)
        self.edit_confirm_diff.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.edit_confirm_diff.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.edit_confirm_diff.setOpenExternalLinks(False)
        diff_font = QFont("Consolas")
        diff_font.setPixelSize(12)
        self.edit_confirm_diff.setFont(diff_font)
        top_v.addWidget(self.edit_confirm_diff)

        v.addWidget(top)

        # ── 反馈输入框（拒绝时说明原因，AI 据此调整） ──
        self.edit_confirm_feedback = QLineEdit()
        self.edit_confirm_feedback.setObjectName("editConfirmFeedback")
        self.edit_confirm_feedback.setPlaceholderText(
            "可选：说明该怎么改，AI 会据此调整；留空 = 直接拒绝")
        self.edit_confirm_feedback.setMinimumHeight(30)
        # 在反馈框里按 Enter = 直接拒绝并把反馈带给 AI
        self.edit_confirm_feedback.returnPressed.connect(
            lambda: self._resolve_edit_confirm(False))
        v.addWidget(self.edit_confirm_feedback)

        # 选项行：复用命令卡的"全宽行"工厂
        def _row(num, label, obj_name, on_click):
            btn = QPushButton()
            btn.setObjectName(obj_name)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setMinimumHeight(40)
            row_lo = QHBoxLayout(btn)
            row_lo.setContentsMargins(20, 0, 20, 0)
            row_lo.setSpacing(14)
            num_lbl = QLabel(num)
            num_lbl.setObjectName("editConfirmOptNum")
            num_lbl.setFixedWidth(14)
            text_lbl = QLabel(label)
            text_lbl.setObjectName("editConfirmOptText")
            row_lo.addWidget(num_lbl)
            row_lo.addWidget(text_lbl, 1)
            btn.clicked.connect(on_click)
            return btn

        self._edit_allow_btn = _row(
            "1", "允许此次修改", "editConfirmAllowRow",
            lambda: self._resolve_edit_confirm(True, remember=False),
        )
        self._edit_trust_btn = _row(
            "2", "信任对此文件的所有后续修改（本次会话）", "editConfirmTrustRow",
            lambda: self._resolve_edit_confirm(True, remember=True),
        )
        self._edit_deny_btn = _row(
            "3", "拒绝", "editConfirmDenyRow",
            lambda: self._resolve_edit_confirm(False),
        )
        v.addWidget(self._edit_allow_btn)
        v.addWidget(self._edit_trust_btn)
        v.addWidget(self._edit_deny_btn)

        self._edit_confirm_hint = QLabel("1 / 2 / 3 选择 · Esc 取消")
        self._edit_confirm_hint.setObjectName("editConfirmHint")
        self._edit_confirm_hint.setAlignment(Qt.AlignCenter)
        hint_wrap = QWidget()
        hint_wrap_l = QVBoxLayout(hint_wrap)
        hint_wrap_l.setContentsMargins(0, 8, 0, 10)
        hint_wrap_l.addWidget(self._edit_confirm_hint)
        v.addWidget(hint_wrap)

        # 等待回调的状态（worker 阻塞用的 Event 和结果 dict）
        self._edit_confirm_result_holder = None
        self._edit_confirm_done_event = None
        self._edit_confirm_path = ""  # 当前待批 path，按钮 callback 用

        # 装事件过滤器接 1 / 2 / 3 / Esc 键
        bar.installEventFilter(self)
        bar.setFocusPolicy(Qt.StrongFocus)

        self.edit_confirm_bar = bar
        self._style_edit_confirm_bar()

    def _style_edit_confirm_bar(self):
        """diff 预览卡的主题配色——蓝色调，跟红色（命令）/橙色（AI）区分开。"""
        if not hasattr(self, "edit_confirm_bar"):
            return
        accent = self._t("user_label")  # 用户色调，跟工具调用强调色区分
        divider = self._t("input_border")
        text = self._t("text")
        text_dim = self._t("text_dim")
        hover_bg = self._t("history_hover_bg")

        self.edit_confirm_bar.setStyleSheet(
            f"QFrame#editConfirmBar {{"
            f"  background: {self._t('input_bg')};"
            f"  border: 1px solid {accent};"
            f"  border-radius: 12px;"
            f"}}"
            f"QLabel#editConfirmTitle {{"
            f"  color: {accent}; font-size: 14px; font-weight: 600;"
            f"  background: transparent;"
            f"}}"
            f"QLabel#editConfirmPath {{"
            f"  color: {text_dim}; font-size: 12px;"
            f"  font-family: Consolas, 'Microsoft YaHei UI';"
            f"  background: transparent; padding: 0;"
            f"}}"
            f"QTextBrowser#editConfirmDiff {{"
            f"  background: {self._t('md_pre_bg')};"
            f"  color: {self._t('md_pre_text')};"
            f"  border: 1px solid {divider};"
            f"  border-radius: 8px; padding: 8px 10px;"
            f"}}"
            f"QPushButton#editConfirmAllowRow,"
            f"QPushButton#editConfirmTrustRow,"
            f"QPushButton#editConfirmDenyRow {{"
            f"  background: transparent; border: none;"
            f"  border-top: 1px solid {divider};"
            f"  border-radius: 0; text-align: left; padding: 0;"
            f"}}"
            f"QPushButton#editConfirmAllowRow:hover,"
            f"QPushButton#editConfirmTrustRow:hover,"
            f"QPushButton#editConfirmDenyRow:hover {{"
            f"  background: {hover_bg};"
            f"}}"
            f"QLabel#editConfirmOptNum {{"
            f"  color: {text_dim}; font-size: 12px; font-weight: 600;"
            f"  background: transparent;"
            f"}}"
            f"QLabel#editConfirmOptText {{"
            f"  color: {text}; font-size: 13px; background: transparent;"
            f"}}"
            f"QLabel#editConfirmHint {{"
            f"  color: {text_dim}; font-size: 11px;"
            f"  background: transparent;"
            f"  border-top: 1px solid {divider};"
            f"  padding-top: 8px;"
            f"}}"
            # 反馈输入框：跟 diff 预览框同底色，聚焦时强调色描边
            f"QLineEdit#editConfirmFeedback {{"
            f"  background: {self._t('md_pre_bg')};"
            f"  border: 1px solid {divider};"
            f"  border-radius: 6px; padding: 6px 9px;"
            f"  color: {text}; font-size: 12px;"
            f"  font-family: Consolas, 'Microsoft YaHei UI';"
            f"}}"
            f"QLineEdit#editConfirmFeedback:focus {{ border-color: {accent}; }}"
        )
        icon = self._svg_icon("edit_lucide.svg", accent)
        if icon.isNull():
            icon = self._svg_icon("file_text_lucide.svg", accent)
        if not icon.isNull():
            self._edit_confirm_icon.setPixmap(icon.pixmap(QSize(16, 16)))

    def _on_edit_confirm_request(self, path, diff_text, result_holder, done_event):
        """UI 主线程槽：把 diff 灌进 edit 卡片 + 显示。"""
        if not hasattr(self, "edit_confirm_bar"):
            result_holder["allow"] = False
            done_event.set()
            return
        if self._edit_confirm_done_event is not None:
            # 旧请求被新的取代：先 deny 解阻塞旧 worker，再走新流程显示新卡
            try:
                if self._edit_confirm_result_holder is not None:
                    self._edit_confirm_result_holder["allow"] = False
                self._edit_confirm_done_event.set()
            except Exception:
                pass
            self._edit_confirm_result_holder = None
            self._edit_confirm_done_event = None

        self.show()
        self.raise_()
        self.activateWindow()

        self._edit_confirm_result_holder = result_holder
        self._edit_confirm_done_event = done_event
        self._edit_confirm_path = path

        self.edit_confirm_path.setText(f"📝 {path}")
        # diff 渲染：加号绿色 / 减号红色 / 头部信息灰色
        self.edit_confirm_diff.setHtml(self._format_diff_html(diff_text))
        self.edit_confirm_bar.setVisible(True)
        self.edit_confirm_bar.setFocus()
        # 清空反馈输入框
        self.edit_confirm_feedback.clear()
        # 注：手机确认推送由 confirm_edit 的 push_confirm 接管（文件 + diff + inline 按钮），
        # 不再额外发"等待编辑确认"文本通知（与按钮卡冗余）

    def _resolve_edit_confirm(self, allow: bool, remember: bool = False):
        """按钮点击：写结果 / 加路径白名单 / 隐藏卡片 / 唤醒 worker。"""
        if self._edit_confirm_done_event is None:
            return
        feedback = self.edit_confirm_feedback.text().strip()
        if not allow:
            # 只有纯拒绝（无反馈）才停掉本轮；有反馈则让 AI 据此调整
            if not feedback:
                _state.stop_flag = True
        if allow and remember:
            p = self._edit_confirm_path
            if p:
                self._session_edit_path_allowlist.add(p)
        self._edit_confirm_result_holder["allow"] = allow
        self._edit_confirm_result_holder["feedback"] = feedback
        self._edit_confirm_done_event.set()
        self._edit_confirm_result_holder = None
        self._edit_confirm_done_event = None
        self._edit_confirm_path = ""
        self.edit_confirm_bar.setVisible(False)
        self.edit_confirm_diff.clear()
        self.edit_confirm_path.setText("")

    def _release_pending_edit(self):
        """关窗 / 退出时唤醒挂着的 edit confirm 请求，避免 worker 无限挂起。"""
        if self._edit_confirm_done_event is None:
            return
        try:
            if self._edit_confirm_result_holder is not None:
                self._edit_confirm_result_holder["allow"] = False
            self._edit_confirm_done_event.set()
        except Exception:
            pass
        self._edit_confirm_result_holder = None
        self._edit_confirm_done_event = None
        if hasattr(self, "edit_confirm_bar"):
            self.edit_confirm_bar.setVisible(False)

    def _on_dismiss_confirm(self):
        """手机端已决出确认 → 主线程收掉可能还挂着的 PC 卡片。

        只动 UI + 清 UI 状态指针，**绝不碰 result/done**（已由 _resolve_remote_confirm
        写好并 set）。把 done_event 指针清成 None，使之后误点 PC 卡按钮成为 no-op
        （_resolve_command_confirm / _resolve_edit_confirm 开头有 None 守卫）。
        同一时刻只有一个确认在 pending，这里两张卡都收一遍，另一张是 no-op。
        """
        if hasattr(self, "command_confirm_bar"):
            self.command_confirm_bar.setVisible(False)
            self.command_confirm_text.clear()
        self._command_confirm_result_holder = None
        self._command_confirm_done_event = None
        self._command_confirm_destructive = False
        if hasattr(self, "edit_confirm_bar"):
            self.edit_confirm_bar.setVisible(False)
            self.edit_confirm_diff.clear()
            self.edit_confirm_path.setText("")
        self._edit_confirm_result_holder = None
        self._edit_confirm_done_event = None
        self._edit_confirm_path = ""

    # ══════════════════════════════════════
    # worker 线程同步等待入口
    # ══════════════════════════════════════

    def confirm_command(self, command: str) -> tuple[bool, str]:
        """从 worker 线程同步等待用户在主线程的内联确认条上选择。

        放行优先级：
          1. 命令被"危险"判定 → 永不绕过，必须弹卡片
          2. base 命令在前缀白名单 → 直接放行（"信任所有 git 类"那种）
          3. 精确字符串命中旧版白名单 → 放行（向后兼容）
          4. 其它 → 弹卡片让用户选

        手机遥控模式（remote_session + REMOTE_TELEGRAM_CONFIRM）下，
        同时把确认推到 Telegram inline 按钮，PC ↔ Telegram 双向竞争，
        先点先到。

        done.wait() 无限等待，由用户点击按钮或关窗 _release 唤醒。

        返回 (allowed, feedback)：allowed 为是否允许；feedback 是用户附带的
        文字反馈（允许时也可附反馈；拒绝时反馈用于告知 AI 如何调整）。
        """
        # 危险命令必须每次确认，永不被白名单绕过
        is_destructive = self._is_destructive_command(command)
        if not is_destructive:
            base = self._extract_base_command(command)
            if base and base in self._session_command_prefix_allowlist:
                return True, ""
            if self._normalize_command(command) in self._session_command_allowlist:
                return True, ""

        result = {}
        done = threading.Event()

        # --- 手机 Telegram 遥控确认（与 PC 卡片竞争，先点先到） ---
        remote_cid = None
        remote_msg_id = None
        if REMOTE_TELEGRAM_CONFIRM:
            remote_cid = _new_confirm_id()
            with _pending_lock:
                _pending_remote_confirms[remote_cid] = {
                    "result": result, "done": done, "msg_id": None,
                }
            remote_msg_id = telegram_push.push_confirm(
                f"⚠️ 执行命令？\n\n{command}\n\n请点击下方按钮 ⬇️",
                remote_cid,
                is_destructive=is_destructive,
            )
            if remote_msg_id:
                with _pending_lock:
                    entry = _pending_remote_confirms.get(remote_cid)
                    if entry:
                        entry["msg_id"] = remote_msg_id

        # --- PC 端内联卡片（始终弹，保证 PC 端也能操作） ---
        self.bridge.confirm_request.emit(command, result, done)
        done.wait()

        # 手机端点了"记住同类"——补加前缀白名单（PC 路径已在 _resolve_command_confirm 里加过）
        if result.get("allow") and result.get("remember") and not is_destructive:
            base = self._extract_base_command(command)
            if base:
                self._session_command_prefix_allowlist.add(base)

        # 清理远程注册表（如果远程那边还没触发）
        if remote_cid:
            with _pending_lock:
                _pending_remote_confirms.pop(remote_cid, None)
            # 仅当 PC 先点（远程消息还挂着按钮）时补一条结果文案；手机自己点的
            # 由 _resolve_remote_confirm 已改过消息，不再覆盖（避免错标"PC 端操作"）。
            if remote_msg_id and not result.get("by_remote"):
                label = "✅ 已允许" if result.get("allow") else "❌ 已拒绝"
                telegram_push.edit_message_text(remote_msg_id, f"{label}（PC 端操作）")

        return bool(result.get("allow", False)), result.get("feedback", "")

    def confirm_edit(self, path: str, diff_text: str) -> tuple[bool, str]:
        """从 worker 线程同步等待用户审批 edit_file 的 diff 预览。

        本次会话用户主动选过"信任所有对此文件的修改"的话直接放行。
        否则弹 diff 预览卡（参考命令确认卡的非模态机制）。

        手机遥控模式下同时推 Telegram inline 确认（与 PC 卡片竞争）。

        done.wait() 无限等待，由用户点击按钮或关窗 _release 唤醒。
        """
        if path and path in self._session_edit_path_allowlist:
            return True, ""

        result = {}
        done = threading.Event()

        # --- 手机 Telegram 遥控确认 ---
        remote_cid = None
        remote_msg_id = None
        if REMOTE_TELEGRAM_CONFIRM:
            remote_cid = _new_confirm_id()
            with _pending_lock:
                _pending_remote_confirms[remote_cid] = {
                    "result": result, "done": done, "msg_id": None,
                }
            # 截取 diff 前 800 字符（Telegram 消息有 4096 上限）
            diff_preview = (diff_text or "")[:800]
            if len(diff_text or "") > 800:
                diff_preview += "\n…(已截断)"
            remote_msg_id = telegram_push.push_confirm(
                f"📝 编辑文件确认\n\n{path}\n\n{diff_preview}\n\n请点击下方按钮 ⬇️",
                remote_cid,
            )
            if remote_msg_id:
                with _pending_lock:
                    entry = _pending_remote_confirms.get(remote_cid)
                    if entry:
                        entry["msg_id"] = remote_msg_id

        # --- PC 端内联卡片 ---
        self.bridge.edit_confirm_request.emit(path or "", diff_text or "", result, done)
        done.wait()

        # 手机端点了"记住同类"——补加文件白名单（PC 路径已在 _resolve_edit_confirm 里加过）
        if result.get("allow") and result.get("remember") and path:
            self._session_edit_path_allowlist.add(path)

        # 清理
        if remote_cid:
            with _pending_lock:
                _pending_remote_confirms.pop(remote_cid, None)
            # 同 confirm_command：手机自己点的不再覆盖文案（避免错标"PC 端操作"）
            if remote_msg_id and not result.get("by_remote"):
                label = "✅ 已允许" if result.get("allow") else "❌ 已拒绝"
                telegram_push.edit_message_text(remote_msg_id, f"{label}（PC 端操作）")

        return bool(result.get("allow", False)), result.get("feedback", "")

    # ══════════════════════════════════════
    # 静态辅助
    # ══════════════════════════════════════

    @staticmethod
    def _normalize_command(command: str) -> str:
        """允许列表用的命令规范化：去首尾空白 + 折叠中间连续空格，便于"相同命令"匹配。"""
        return " ".join((command or "").split())

    @staticmethod
    def _extract_base_command(command: str) -> str:
        """从命令字符串里抽出 base（第一个 token），用作前缀白名单的 key。

        例：
          "git status --short"   → "git"
          "  python  -m pytest"  → "python"
          "dir /b"               → "dir"
          ""                     → ""

        注意：对 `cd foo && git status` 这种复合命令，返回的是 "cd"——这是有意为之，
        让用户**不能**通过"信任 cd"绕过后面接的危险操作（destructive 判定会先把
        整个字符串扫一遍，命中就拒绝白名单短路）。
        """
        s = (command or "").strip()
        if not s:
            return ""
        # 第一个空白前的内容
        first = s.split(None, 1)[0]
        # 如果带路径前缀（如 /usr/bin/git 或 C:\Tools\python.exe），取 basename
        # 这样不同安装路径的同一工具能匹配同一前缀
        import os as _os
        return _os.path.basename(first).lower()

    @staticmethod
    def _is_destructive_command(command: str) -> bool:
        """启发式判断命令是否"危险"（永久数据丢失类）。匹配则**不**给"记住"选项。

        匹配规则保守：宁可多问一次，不要漏给 AI 永久授权后被 rm -rf。
        """
        import re as _re
        if not command:
            return False
        c = command.lower()
        c_no_sql_comments = _re.sub(r'/\*.*?\*/', ' ', c, flags=_re.S)
        patterns = [
            r'\brm\b(?=.*(?:\s|^)(?:-\w*r\w*|-\w*f\w*|--recursive|--force)\b)',
            r'\bdel\s+(?:/[sfqa]\b|/[sfqa]\s)',          # del /s /f /q
            r'\brmdir\s+(?:/s|/q)',
            r'\bremove-item\b.*-(?:recurse|force)',
            r'\bformat\s+[a-z]:',                        # format C:
            r'\bmkfs\b',
            r'\bdd\s+(?:if|of)=',
            r'\bsudo\b',
            r'\brunas\b',
            r'\bshutdown\b',
            r'\breboot\b',
            r'\bchmod\s+777',
            r'>\s*/dev/sd',                              # 直接写裸盘
            r':>\s*/',                                   # truncate root
        ]
        sql_patterns = [
            r'\bdrop\s+(?:table|database|schema)\b',
            r'\btruncate\s+table\b',
        ]
        return (
            any(_re.search(p, c) for p in patterns)
            or any(_re.search(p, c_no_sql_comments) for p in sql_patterns)
        )

    @staticmethod
    def _format_diff_html(diff_text: str) -> str:
        """把 unified diff 文本转成带颜色的 HTML。"""
        def esc(s):
            return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        lines = []
        for ln in (diff_text or "").splitlines():
            color = None
            if ln.startswith("+") and not ln.startswith("+++"):
                color = "#27ae60"  # 绿：新增
            elif ln.startswith("-") and not ln.startswith("---"):
                color = "#c0392b"  # 红：删除
            elif ln.startswith("@@"):
                color = "#5b66d6"  # 蓝紫：hunk 头
            elif ln.startswith("---") or ln.startswith("+++"):
                color = "#888"     # 灰：文件头
            if color:
                lines.append(f'<span style="color:{color};">{esc(ln)}</span>')
            else:
                lines.append(esc(ln))
        return (
            '<pre style="font-family:Consolas,monospace;font-size:12px;'
            'margin:0;white-space:pre-wrap;word-wrap:break-word;">'
            + "\n".join(lines)
            + "</pre>"
        )

    # ══════════════════════════════════════
    # 键盘事件分发（被 ChatUI.eventFilter 调用）
    # ══════════════════════════════════════

    def _handle_confirm_bar_keys(self, obj, event) -> bool:
        """eventFilter 把确认条相关按键派发到这里。处理掉返回 True。

        - command confirm bar：1=允许 / 2=记住（非危险才有） / 3=拒绝 / Esc=拒绝
        - edit confirm bar：1=允许 / 2=信任 / 3=拒绝 / Esc=拒绝
        """
        if (hasattr(self, 'command_confirm_bar')
                and obj == self.command_confirm_bar
                and event.type() == event.Type.KeyPress
                and self.command_confirm_bar.isVisible()):
            key = event.key()
            if key == Qt.Key_1:
                self._cmd_allow_btn.click()
                return True
            if key == Qt.Key_2 and self._cmd_remember_btn.isVisible():
                self._cmd_remember_btn.click()
                return True
            if key == Qt.Key_3:
                self._cmd_deny_btn.click()
                return True
            if key == Qt.Key_Escape:
                self._cmd_deny_btn.click()
                return True
        if (hasattr(self, 'edit_confirm_bar')
                and obj == self.edit_confirm_bar
                and event.type() == event.Type.KeyPress
                and self.edit_confirm_bar.isVisible()):
            key = event.key()
            if key == Qt.Key_1:
                self._edit_allow_btn.click()
                return True
            if key == Qt.Key_2:
                self._edit_trust_btn.click()
                return True
            if key == Qt.Key_3 or key == Qt.Key_Escape:
                self._edit_deny_btn.click()
                return True
        return False
