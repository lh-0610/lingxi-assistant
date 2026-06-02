"""主聊天窗口 ChatUI。

UI/Agent 解耦：agent 线程通过 SignalBridge.emit 把渲染请求 queue 到主线程，
ChatUI 暴露给 agent 的全部公开方法（show_message / render_final_markdown /
remove_thinking_indicator / show_token_usage / show_retry）都是线程安全 wrapper。
"""
import os
import sys
import json
import base64
import threading

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTextEdit,
    QTextBrowser, QPushButton, QLabel, QFrame, QScrollArea,
    QSplitter, QSizePolicy, QFileDialog, QComboBox, QMenu, QMessageBox,
    QDialog, QLineEdit, QCheckBox
)
from PySide6.QtCore import Qt, Signal, QObject, QSize, QTimer, QPoint
from PySide6.QtGui import (
    QFont, QIcon, QTextCursor, QColor, QTextCharFormat, QPixmap, QImage,
    QPainter, QPolygon, QAction,
)
from langchain_core.messages import HumanMessage, SystemMessage

from .. import agent
from .. import state
from ._base import BASE_DIR, CONFIG_PATH
from .theme import THEMES, build_stylesheet, load_saved_theme, save_theme_choice
from .widgets import (
    CloseConfirmDialog, DragDropTextBrowser, DragDropTextEdit, FileCompleter,
    HistoryRow, SignalBridge,
)
from .helpers import (
    _build_image_content_block, _make_button_icon, _make_upload_icon,
    _strip_markdown_for_tts,
)
from .prefs import _load_ui_prefs, _save_ui_prefs
from .settings_dialog import SettingsDialog
from .confirm_bars import ConfirmBarsMixin
from .markdown_render import MarkdownRenderMixin
from .search_overlay import SearchOverlayMixin
from .sidebar import SidebarMixin
from .header import HeaderMixin


# 语音可选依赖：装了就启用，没装就降级（按钮不出现）
try:
    from ..voice import Recorder, STT, make_tts
    from ..config import (
        VOICE_STT_MODEL, VOICE_STT_LANGUAGE,
        VOICE_TTS_DEFAULT_ENABLED,
    )
    _VOICE_AVAILABLE = True
except Exception as _voice_err:
    _VOICE_AVAILABLE = False
    Recorder = STT = make_tts = None
    VOICE_TTS_DEFAULT_ENABLED = False


# 聊天区 HTML 文本里的彩色 emoji → icons/ 下的 SVG 文件（见 docs/emoji_inventory.md）。
# 8 个概念复用现有 *_lucide.svg，其余用合并进来的 lucide 图标。✓/✗/⚙ 等单色字符符号
# 按 README 决定保留为字体字形、不在此映射。SVG 走 currentColor，由 _inline_svg_img 按主题着色。
_EMOJI_ICON = {
    # 工具显示名（tools.py TOOL_DISPLAY_NAMES）
    "📖": "book-open.svg", "✏️": "file-pen.svg", "📝": "file-plus.svg",
    "🪄": "wand-sparkles.svg", "📂": "folder_open_lucide.svg", "⚡": "zap.svg",
    "🔍": "search.svg", "🌐": "globe.svg", "🎨": "palette.svg",
    "🧠": "brain_lucide.svg", "🗑️": "trash_lucide.svg", "📋": "clipboard-list.svg",
    "⏹": "square-stop.svg", "🗺": "map.svg", "🔀": "git-compare.svg",
    "📜": "scroll-text.svg", "🧪": "flask-conical.svg", "🔧": "wrench.svg",
    "🔌": "plug.svg",
    # 工具区还会出现的拦截/安全提示
    "⚠️": "triangle-alert.svg", "⛔": "octagon-x.svg", "🔒": "lock.svg",
    # 状态/过程（tool_result 等宽输出 + 错误/重试/图片识别）
    "📁": "folder_lucide.svg", "📄": "file_text_lucide.svg", "⏱️": "timer.svg",
    "✅": "circle-check.svg", "❌": "circle-x.svg", "🔎": "scan-search.svg",
    "🔄": "refresh_cw_lucide.svg",
}


class ChatUI(ConfirmBarsMixin, MarkdownRenderMixin, SearchOverlayMixin,
             SidebarMixin, HeaderMixin, QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("灵犀")
        self.resize(1000, 700)
        self.setMinimumSize(600, 400)
        self.theme = load_saved_theme()
        self.setStyleSheet(build_stylesheet(self.theme))
        self._apply_tooltip_style()  # QToolTip 要设到 app 级才生效

        # 设置图标
        icon_path = os.path.join(BASE_DIR, "icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.is_generating = False
        self._has_input = False
        self._sidebar_visible = True
        self._pending_images = []  # [(file_path, base64_data), ...]
        # 发送停止按钮图标
        self._icon_arrow = _make_button_icon(arrow=True)
        self._icon_pause = _make_button_icon(arrow=False)
        self._icon_upload = _make_upload_icon(color="#888888")
        self._settings_btn_icon = None
        self._settings_btn_icon_hover = None

        # 信号
        self.bridge = SignalBridge()
        self.bridge.append_signal.connect(self._append_html)
        self.bridge.remove_thinking.connect(self._remove_thinking)
        self.bridge.update_thinking.connect(self._update_thinking)
        self.bridge.render_md.connect(self._render_markdown)
        self.bridge.show_retry.connect(self._show_retry)
        self.bridge.finished.connect(self._on_finished)
        self.bridge.token_usage.connect(self._update_token_usage)
        self.bridge.sessions_refresh.connect(self._refresh_session_list)
        self.bridge.confirm_request.connect(self._on_confirm_request)
        self.bridge.edit_confirm_request.connect(self._on_edit_confirm_request)
        self.bridge.remote_submit.connect(self._on_remote_submit)
        self.bridge.dismiss_confirm.connect(self._on_dismiss_confirm)

        # 让 tools.py 在 worker 线程里能找到主窗口（弹确认框用）
        state.ui_ref = self

        # 跟踪位置
        self._ai_reply_start = None
        self._thinking_start = None
        self._thinking_end = None
        self._think_block_start = None
        self._think_block_chars = 0
        self._think_block_text = ""        # 累积思考原文，用于折叠后查看
        self._thinking_history = {}        # think_id -> 思考全
        self._thinking_dialog = None       # 思考过程弹窗
        self._code_blocks = {}             # code_idx -> raw code text
        self.setAcceptDrops(True)
        self._search_widget = None         # Ctrl+F search floating window
        self._msg_buffers = {}             # msg_idx -> AI message plain text
        # 本次会话用户主动放行过的命令（normalize 后的字符串），重启清空
        # 旧"精确字符串"白名单——还保留是为了向后兼容，主要靠下面的前缀白名单
        self._session_command_allowlist = set()
        # 前缀白名单：base command（如 "git" / "python" / "dir"），本次会话内所有以该
        # base 开头的命令都自动放行。但**危险命令（rm -rf / format / sudo 等）始终要确认**，
        # 不被前缀白名单跳过
        self._session_command_prefix_allowlist = set()
        # edit_file 路径白名单：用户对某个文件选过"信任此文件的所有修改"后，本会话
        # 内对同一路径的 edit_file 自动放行（不再弹 diff 预览）。这是给 AI 修一个
        # 文件改多处时的便利项——审了第一次就信任剩下几次
        self._session_edit_path_allowlist = set()

        # 语音 STT/TTS（懒加载模型，启动不阻塞）
        if _VOICE_AVAILABLE:
            self._recorder = Recorder()
            self._stt = STT(model_size=VOICE_STT_MODEL, language=VOICE_STT_LANGUAGE)
            self._tts = make_tts()  # 工厂按 config 选 edge-tts 或 gpt-sovits
            self._tts_enabled = VOICE_TTS_DEFAULT_ENABLED
            self._stt.transcribed.connect(self._on_stt_transcribed)
            self._stt.failed.connect(self._on_stt_failed)
            # GPT-SoVITS 启动器（让设置弹窗里"启动语音模块"按钮用得上）
            try:
                from ..gpt_sovits_launcher import GPTSoVITSLauncher
                from ..config import GPT_SOVITS_URL
                self._gpt_sovits_launcher = GPTSoVITSLauncher(GPT_SOVITS_URL)
            except Exception:
                self._gpt_sovits_launcher = None
        else:
            self._recorder = None
            self._stt = None
            self._tts = None
            self._tts_enabled = False
            self._gpt_sovits_launcher = None

        self._build_ui()
        self._refresh_session_list()
        self._restore_role_card_ui()
        self._show_empty_state()
        QTimer.singleShot(300, self._show_current_model_config_warning)

    # ── 主题工具 ──
    def _t(self, key):
        """读取当前主题的 token 颜色"""
        return THEMES[self.theme][key]

    def _toggle_theme(self):
        """白天 ↔ 夜间。立即应用到主样式表与所有内联样式 chrome；
        已渲染聊天历史保留旧色，下次重新载入会话时刷新。"""
        self.theme = "light" if self.theme == "dark" else "dark"
        save_theme_choice(self.theme)
        self._apply_theme()

    # ── 设置弹窗 ──

    def _open_settings_menu(self):
        """齿轮按钮：弹出 VSCode 风格的设置对话框。"""
        dlg = SettingsDialog(self)
        dlg.exec()

    def _apply_tooltip_style(self):
        """把 QToolTip 颜色强行刷成跟主题一致。

        QToolTip 是顶层弹窗，**不继承主窗口 setStyleSheet**。在 Windows 上 Qt 还会
        在多种情况下绕过 app.setStyleSheet 的 QToolTip 规则，用系统默认（黑底白字）。

        所以这里**三管齐下**全设上：
          1. app.setStyleSheet(QToolTip QSS) —— 标准路径
          2. app.setPalette(ToolTipBase/Text) —— Qt 优先级最高的色板系统
          3. QToolTip.setPalette(同) —— 类级 palette 兜底
        三条都设上，无论 Qt 走哪条路解析颜色，结果都跟我们主题一致。
        """
        from PySide6.QtWidgets import QApplication, QToolTip
        from PySide6.QtGui import QColor, QPalette
        from PySide6.QtCore import QTimer
        from .theme import build_tooltip_qss

        app = QApplication.instance()
        if app is None:
            return

        bg = QColor(self._t("tooltip_bg"))
        fg = QColor(self._t("tooltip_text"))

        def _apply():
            # 1. QSS
            app.setStyleSheet(build_tooltip_qss(self.theme))
            # 2. App palette（覆盖整个 app 的 ToolTipBase/Text 角色色板）
            app_palette = app.palette()
            app_palette.setColor(QPalette.ToolTipBase, bg)
            app_palette.setColor(QPalette.ToolTipText, fg)
            app_palette.setColor(QPalette.Inactive, QPalette.ToolTipBase, bg)
            app_palette.setColor(QPalette.Inactive, QPalette.ToolTipText, fg)
            app.setPalette(app_palette)
            # 3. QToolTip 类级 palette
            tooltip_palette = QToolTip.palette()
            tooltip_palette.setColor(QPalette.ToolTipBase, bg)
            tooltip_palette.setColor(QPalette.ToolTipText, fg)
            tooltip_palette.setColor(QPalette.Inactive, QPalette.ToolTipBase, bg)
            tooltip_palette.setColor(QPalette.Inactive, QPalette.ToolTipText, fg)
            QToolTip.setPalette(tooltip_palette)

        _apply()
        # 再延迟一次：有些场景 Qt 在 init 过程中会重置 palette，延一个 tick 再覆盖
        QTimer.singleShot(0, _apply)

    def _apply_theme(self):
        """重新生成全局 QSS，并刷新所有用 setStyleSheet 直接设置的 chrome。"""
        self.setStyleSheet(build_stylesheet(self.theme))
        self._apply_tooltip_style()  # 切主题时 tooltip 也跟着刷
        # 主题按钮图标
        if hasattr(self, "theme_btn"):
            self.theme_btn.setText("☀" if self.theme == "dark" else "☾")
            self.theme_btn.setToolTip("切到白天模式" if self.theme == "dark" else "切到夜间模式")
        # 品牌字符在白天主题里隐藏，夜间显示
        if hasattr(self, "header_brand"):
            visible = self._t("brand_visible") == "true"
            self.header_brand.setVisible(visible)
            self.header_brand_dot.setVisible(visible)
        # 各内联样式区域重新涂色
        if hasattr(self, "history_widget"):
            self._style_sidebar_scroll()
        if hasattr(self, "settings_btn"):
            self._style_settings_btn()
        if hasattr(self, "chat_area"):
            self._style_chat_area()
        if hasattr(self, "model_combo"):
            self._style_model_combo()
        if hasattr(self, "think_btn"):
            self._style_think_btn()
        if hasattr(self, "mode_btn"):
            self._style_mode_btn()
        if hasattr(self, "undo_btn"):
            self._style_undo_btn()
        if hasattr(self, "role_btn"):
            self._restore_role_card_ui()
        if hasattr(self, "img_btn"):
            self._style_img_btn()
        if hasattr(self, "scroll_bottom_btn"):
            self._style_scroll_bottom_btn()
        # 新一轮历史项（删除按钮）会用新色
        if hasattr(self, "history_layout"):
            self._refresh_session_list()
        if hasattr(self, "project_btn"):
            self._refresh_project_indicator()
        if hasattr(self, "command_confirm_bar"):
            self._style_command_confirm_bar()
        if hasattr(self, "edit_confirm_bar"):
            self._style_edit_confirm_bar()
        if hasattr(self, "_file_completer"):
            self._apply_completer_theme()
        # 已存在的搜索浮层销毁，下次再显示用新主题重建
        if getattr(self, "_search_widget", None) is not None:
            try:
                self._search_widget.deleteLater()
            except Exception:
                pass
            self._search_widget = None
        # 已存在的思考过程对话框销毁，下次重建
        if getattr(self, "_thinking_dialog", None) is not None:
            try:
                self._thinking_dialog.deleteLater()
            except Exception:
                pass
            self._thinking_dialog = None

    def _show_current_model_config_warning(self):
        issues = agent.get_model_config_issues()
        if not issues:
            return
        warning_key = (agent.current_model_index, tuple(issues))
        if getattr(self, "_last_config_warning_key", None) == warning_key:
            return
        self._last_config_warning_key = warning_key
        text = "\n⚠️ " + "\n".join(issues) + "\n"
        self.show_message(text, "tool_result")
        self._show_toast(issues[0], duration=5000)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 侧边栏
        self._build_sidebar()
        main_layout.addWidget(self.sidebar)

        # 主区域
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self._build_header(right_layout)
        self._build_chat_area(right_layout)
        self._build_input_area(right_layout)
        self._build_project_indicator(right_layout)
        self._build_footer(right_layout)

        main_layout.addWidget(right, 1)

    # ── 侧边栏 ──


    def _reset_render_state(self):
        """切换/新建会话前，清掉只对当前会话有意义的渲染状态"""
        self._thinking_history.clear()
        self._code_blocks.clear()
        self._msg_buffers.clear()
        if hasattr(self, "_image_paths"):
            self._image_paths.clear()
        if hasattr(self, "chat_area"):
            self.chat_area.document().clear()
        if hasattr(self, 'token_usage_label'):
            self.token_usage_label.setText('Token: -')

    def _is_hidden_bridge_message(self, msg):
        """内部图片识别桥接消息只给模型看，历史界面不当作用户聊天展示。"""
        if not isinstance(msg, HumanMessage) or isinstance(msg.content, list):
            return False
        content = str(msg.content or "").lstrip()
        return (
            content.startswith("[[LINGXI_INTERNAL_VISION_BRIDGE]]")
            or content.startswith("[图片识别结果，由 ")
        )

    def _redraw_chat(self):
        import markdown
        self.chat_area.clear()
        if hasattr(self, "_image_paths"):
            self._image_paths.clear()
        rendered_any = False
        history_snapshot = list(agent.chat_history)
        for msg in history_snapshot:
            if self._is_hidden_bridge_message(msg):
                continue
            if isinstance(msg, HumanMessage):
                rendered_any = True
                self._append_html("你\n", "user_label")
                # 多模态消息
                if isinstance(msg.content, list):
                    for part in msg.content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            self._append_html(part["text"] + "\n", "user_msg")
                        elif isinstance(part, dict) and part.get("type") == "image_url":
                            url = part["image_url"]["url"]
                            if url.startswith("data:image"):
                                b64_data = url.split(",", 1)[1]
                                img_bytes = base64.b64decode(b64_data)
                                img = QImage()
                                img.loadFromData(img_bytes)
                                if not img.isNull():
                                    if img.width() > 300:
                                        img = img.scaledToWidth(300, Qt.SmoothTransformation)
                                    name = f"hist_img_{id(img)}"
                                    self.chat_area.document().addResource(
                                        self.chat_area.document().ResourceType.ImageResource,
                                        name, img
                                    )
                                    cursor = self.chat_area.textCursor()
                                    cursor.movePosition(QTextCursor.End)
                                    cursor.insertImage(name)
                                    cursor.insertText("\n")
                        elif isinstance(part, dict) and part.get("type") == "image":
                            source = part.get("source", {})
                            if source.get("type") == "base64":
                                b64_data = source.get("data", "")
                                if b64_data:
                                    img_bytes = base64.b64decode(b64_data)
                                    img = QImage()
                                    img.loadFromData(img_bytes)
                                    if not img.isNull():
                                        if img.width() > 300:
                                            img = img.scaledToWidth(300, Qt.SmoothTransformation)
                                        name = f"hist_img_{id(img)}"
                                        self.chat_area.document().addResource(
                                            self.chat_area.document().ResourceType.ImageResource,
                                            name, img
                                        )
                                        cursor = self.chat_area.textCursor()
                                        cursor.movePosition(QTextCursor.End)
                                        cursor.insertImage(name)
                                        cursor.insertText("\n")
                    self._append_html("\n", "spacer")
                else:
                    self._append_html(msg.content + "\n\n", "user_msg")
            elif hasattr(msg, 'content') and msg.__class__.__name__ == "AIMessage":
                # content 可能是 str 或 list（含 thinking + text blocks）
                _ai_content = msg.content
                if isinstance(_ai_content, list):
                    # 从 content blocks 中提取纯文本部分用于显示
                    _text_parts = []
                    for _blk in _ai_content:
                        if isinstance(_blk, dict) and _blk.get('type') == 'text' and _blk.get('text'):
                            _text_parts.append(_blk['text'])
                    _ai_content = "\n".join(_text_parts)
                _has_text = bool(_ai_content and _ai_content.strip())
                _tool_names = [tc.get('name', '?') for tc in (getattr(msg, 'tool_calls', None) or [])
                               if isinstance(tc, dict)]
                ai_name = agent.get_current_role_name() or "AI"

                # 有正文就渲染正文 + 复制按钮
                if _has_text:
                    rendered_any = True
                    self._append_html(f"{ai_name}\n", "ai_label")
                    styled_html = self._md_to_html(_ai_content)
                    cursor = self.chat_area.textCursor()
                    cursor.movePosition(QTextCursor.End)
                    cursor.insertHtml(styled_html)
                    # 同 _render_markdown：用表格 spacer 撑开正文与按钮间的距离，
                    # QTextDocument 对 <div margin> 支持太差
                    spacer = '<table border="0" cellspacing="0" cellpadding="0"><tr><td style="height:18px;font-size:1px;line-height:1px;">&nbsp;</td></tr></table>'
                    cursor.insertHtml(spacer)
                    msg_idx = len(self._msg_buffers)
                    self._msg_buffers[str(msg_idx)] = _ai_content
                    copy_icon = self._inline_svg_img("copy_lucide.svg", self._t("copy_link"), 15, "Copy")
                    cursor.insertHtml(
                        f'<a href="action:copy_msg:{msg_idx}" style="color:{self._t("copy_link")};font-size:13px;'
                        f'text-decoration:none;padding:3px 8px;background:{self._t("copy_link_bg")};border-radius:5px;" title="复制">'
                        f'{copy_icon}</a>'
                    )
                    cursor.insertText("\n\n")

                # 有工具调用就显示摘要——不管这条 AIMessage 有没有正文。
                # （之前只在"无正文"时显示，导致 MiMo "短文字 + 工具调用"同条时工具被吞，
                #   恢复的历史看不出调了哪些工具，整段很怪）
                if _tool_names:
                    rendered_any = True
                    if not _has_text:
                        self._append_html(f"{ai_name}\n", "ai_label")
                    self._append_html(f"🔧 调用了工具: {', '.join(_tool_names)}\n\n", "tool_result")
            elif msg.__class__.__name__ == "ToolMessage":
                # generate_image 的工具结果：重新显示生成的图片
                content = msg.content or ""
                if "已生成图片" in content:
                    import re as _re
                    m = _re.search(r"已生成图片[^:]*:\s*(.+?\.(?:png|jpg|jpeg|webp|gif))", content, _re.IGNORECASE)
                    if m:
                        img_path = m.group(1).strip()
                        if os.path.exists(img_path):
                            rendered_any = True
                            self._insert_image_path(img_path)
        if not rendered_any:
            self._show_empty_state()

    def _show_empty_state(self):
        """聊天为空时显示欢迎态。"""
        if not hasattr(self, "chat_area") or not hasattr(self, "empty_state"):
            return
        self._empty_state_visible = True
        self.chat_area.setProperty("empty", "true")
        self.chat_area.style().unpolish(self.chat_area)
        self.chat_area.style().polish(self.chat_area)
        self.chat_area.clear()
        self._position_empty_state()
        self.empty_state.show()
        self.empty_state.raise_()
        # 初次打开时 viewport 还没完成最终布局，立刻算出来的 width/height
        # 会偏小导致欢迎态居中错位；延迟几次再 reposition，覆盖到布局稳定后的尺寸。
        for delay in (0, 30, 120):
            QTimer.singleShot(delay, self._position_empty_state)

    def _clear_empty_state(self):
        if getattr(self, "_empty_state_visible", False):
            self._empty_state_visible = False
            if hasattr(self, "empty_state"):
                self.empty_state.hide()
            self.chat_area.clear()
            self.chat_area.setProperty("empty", "false")
            self.chat_area.style().unpolish(self.chat_area)
            self.chat_area.style().polish(self.chat_area)

    # ── 顶栏 ──


    def _svg_icon(self, filename, color):
        """共用 helper：把 icons/*.svg 渲染成 QIcon（支持高 DPI）。"""
        svg_path = os.path.join(BASE_DIR, "icons", filename)
        if not os.path.exists(svg_path):
            return QIcon()
        from PySide6.QtSvg import QSvgRenderer
        with open(svg_path, 'r', encoding='utf-8') as f:
            svg_tpl = f.read()
        svg_filled = svg_tpl.replace('currentColor', color)
        renderer = QSvgRenderer(svg_filled.encode('utf-8'))
        dpr = self.devicePixelRatioF() if hasattr(self, 'devicePixelRatioF') else 1.0
        px = QPixmap(int(24 * dpr), int(24 * dpr))
        px.fill(Qt.transparent)
        painter = QPainter(px)
        renderer.render(painter)
        painter.end()
        px.setDevicePixelRatio(dpr)
        return QIcon(px)

    def _inline_svg_img(self, filename, color, size=15, alt=""):
        """给 QTextBrowser HTML 链接用的内联 SVG 图标。"""
        svg_path = os.path.join(BASE_DIR, "icons", filename)
        if not os.path.exists(svg_path):
            return alt
        with open(svg_path, "r", encoding="utf-8") as f:
            svg = f.read().replace("currentColor", color)
        # 归一化 SVG 自身宽高到目标尺寸：QTextBrowser 渲染内联 SVG 时按 SVG 自带的
        # width/height（这批图标都是 24）来画，<img> 的 width/height 未必生效——不归一
        # 化图标会按 24px 渲染、比 14/15px 文字大，撑破行高看着"不在一行"。
        svg = svg.replace('width="24" height="24"', f'width="{size}" height="{size}"', 1)
        data = base64.b64encode(svg.encode("utf-8")).decode("ascii")
        return (
            f'<img src="data:image/svg+xml;base64,{data}" width="{size}" height="{size}" '
            f'alt="{alt}" style="vertical-align:middle;" />'
        )

    def _emoji_to_svg_html(self, text, color, size=14):
        """把 text 里 _EMOJI_ICON 已知的 emoji 替换成内联彩色 SVG <img>，
        其余字符做 HTML 转义。返回可 insertHtml 的片段。最长 emoji 优先匹配
        （含 FE0F 变体选择符的多码点 emoji 排前），避免被前缀截断。"""
        import html as _html
        keys = sorted(_EMOJI_ICON.keys(), key=len, reverse=True)
        out = []
        i = 0
        while i < len(text):
            for emo in keys:
                if text.startswith(emo, i):
                    out.append(self._inline_svg_img(_EMOJI_ICON[emo], color, size, alt=emo))
                    i += len(emo)
                    break
            else:
                out.append(_html.escape(text[i]))
                i += 1
        return "".join(out)

    def _insert_text_with_icons(self, cursor, text, fmt, size=14):
        """按 fmt（字体/颜色/背景）插入 text，但把 _EMOJI_ICON 已知 emoji 换成内联 SVG 图标。
        非 emoji 文本一律 insertText 原样插入——等宽 / 空白 / 换行完全保留（适合 tool_result
        等宽工具输出）；emoji 用 HTML <img vertical-align:middle> 插（和 tool_tag 同款居中，
        比 insertImage 的 QTextImageFormat.AlignMiddle 准——后者会偏低、看着像残留 -3px），
        前后文本仍走 insertText，所以排版不塌。"""
        keys = sorted(_EMOJI_ICON.keys(), key=len, reverse=True)
        fg = fmt.foreground()
        color = fg.color().name() if fg.style() else self._t("tool_result")
        n = len(text)
        i = run_start = 0
        while i < n:
            emo = next((k for k in keys if text.startswith(k, i)), None)
            if emo is None:
                i += 1
                continue
            if i > run_start:
                cursor.insertText(text[run_start:i], fmt)       # 刷出 emoji 前的普通文本段
            cursor.insertHtml(self._inline_svg_img(_EMOJI_ICON[emo], color, size, alt=emo))
            i += len(emo)
            run_start = i
        if n > run_start:
            cursor.insertText(text[run_start:n], fmt)


    def _build_chat_area(self, parent_layout):
        self.chat_area = DragDropTextBrowser()
        self.chat_area.setObjectName("chatArea")
        self.chat_area.setOpenExternalLinks(False)
        self.chat_area.anchorClicked.connect(self._on_link_clicked)
        self.chat_area.setOpenLinks(False)
        chat_font = QFont("Microsoft YaHei")
        chat_font.setPixelSize(15)
        chat_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        chat_font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        self.chat_area.setFont(chat_font)
        self.chat_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._style_chat_area()
        parent_layout.addWidget(self.chat_area, 1)
        self._build_empty_state()

        # 浮动 "回到底部" 按钮（作为主窗口浮层，避免 QTextBrowser 抢焦点/锚点）
        self.scroll_bottom_btn = QPushButton("▼", self)
        self.scroll_bottom_btn.setObjectName("scrollBottomBtn")
        self.scroll_bottom_btn.setFixedSize(36, 36)
        self.scroll_bottom_btn.setCursor(Qt.PointingHandCursor)
        self.scroll_bottom_btn.setToolTip("回到底部")
        self.scroll_bottom_btn.clicked.connect(lambda checked=False: self._scroll_to_bottom())
        self.scroll_bottom_btn.hide()
        self._style_scroll_bottom_btn()

        # 监听滚动条变化，决定是否显示浮动按钮
        sb = self.chat_area.verticalScrollBar()
        sb.valueChanged.connect(self._on_scroll_changed)
        sb.rangeChanged.connect(self._on_scroll_changed)

    def _build_empty_state(self):
        self.empty_state = QWidget(self.chat_area.viewport())
        self.empty_state.setObjectName("emptyState")
        self.empty_state.setAttribute(Qt.WA_TransparentForMouseEvents, False)

        layout = QVBoxLayout(self.empty_state)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        logo = QLabel("灵犀<span style='color:#d87755;'>.</span>")
        logo.setObjectName("emptyLogo")
        logo.setTextFormat(Qt.RichText)
        logo.setAlignment(Qt.AlignCenter)

        title = QLabel("今天想聊点什么？")
        title.setObjectName("emptyTitle")
        title.setAlignment(Qt.AlignCenter)

        subtitle = QLabel("灵犀时刻准备为你提供帮助")
        subtitle.setObjectName("emptySubtitle")
        subtitle.setAlignment(Qt.AlignCenter)

        layout.addWidget(logo)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        # 建议按钮（chips）：副标题下方水平排一行，引导用户点选
        suggestions = QWidget()
        suggestions.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        sug_layout = QHBoxLayout(suggestions)
        sug_layout.setContentsMargins(0, 20, 0, 0)
        sug_layout.setSpacing(12)
        sug_layout.setAlignment(Qt.AlignHCenter)
        for icon_file, text in [
            ("sparkles_lucide.svg", "帮我生成一张插画"),
            ("file_text_lucide.svg", "总结一下这篇文档"),
            ("code_lucide.svg", "解释这段代码的逻辑"),
        ]:
            btn = QPushButton(text)
            btn.setIcon(self._svg_icon(icon_file, self._t("text_dim")))
            btn.setIconSize(QSize(16, 16))
            btn.setObjectName("emptySuggestion")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, t=text: self._use_suggestion(t))
            sug_layout.addWidget(btn)
        layout.addWidget(suggestions, 0, Qt.AlignHCenter)

        self.empty_state.hide()

    def _position_empty_state(self):
        if not hasattr(self, "empty_state"):
            return
        # 让 widget 自然 sizeToContent
        self.empty_state.adjustSize()
        sh = self.empty_state.sizeHint()
        w, h = sh.width(), sh.height()
        vp = self.chat_area.viewport()

        # chat_area 的 CSS padding 是 28/28/18/52（左右不对称），
        # 直接用 viewport 中心居中会偏右。
        # 改用 chat_area 的几何中心做视觉中心，再换算成 viewport 内的坐标。
        chat_w = self.chat_area.width()
        chat_h = self.chat_area.height()
        vp_offset = vp.mapTo(self.chat_area, vp.rect().topLeft())

        target_x = chat_w // 2 - vp_offset.x() - w // 2
        # 垂直方向：在 chat_area 几何中心略偏下（+40 把内容压向下方视觉重心）
        target_y = chat_h // 2 - vp_offset.y() - h // 2

        x = max(0, target_x)
        y = max(34, target_y)
        self.empty_state.setGeometry(x, y, w, h)

    def _use_suggestion(self, text):
        self.entry.setPlainText(text)
        self.entry.setFocus()
        self._check_input_state()

    def _style_chat_area(self):
        self.chat_area.setStyleSheet(
            f"QTextBrowser {{"
            f"  background: {self._t('chat_bg')}; border: none; color: {self._t('chat_text')};"
            f"  padding: 28px 28px 18px 52px;"
            f"  selection-background-color: {self._t('chat_sel_bg')};"
            f"  selection-color: {self._t('chat_sel_text')};"
            f"}}"
            f"QScrollBar:vertical {{ width: 6px; background: transparent; margin: 4px 2px 4px 0px; }}"
            f"QScrollBar::handle:vertical {{ background: {self._t('chat_scroll_handle')}; border-radius: 3px; min-height: 30px; }}"
            f"QScrollBar::handle:vertical:hover {{ background: {self._t('chat_scroll_handle_hover')}; }}"
            f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}"
            f"QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}"
        )

    def _style_scroll_bottom_btn(self):
        self.scroll_bottom_btn.setStyleSheet(
            f"QPushButton {{"
            f"  background: {self._t('scroll_btn_bg')};"
            f"  border: 1px solid {self._t('scroll_btn_border')};"
            f"  border-radius: 18px;"
            f"  color: {self._t('scroll_btn_icon')};"
            f"  font-size: 16px;"
            f"  font-weight: bold;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: {self._t('scroll_btn_hover_bg')};"
            f"}}"
        )

    def _position_scroll_btn(self):
        """将浮动按钮定位到 chat_area 的右下角"""
        if not hasattr(self, 'scroll_bottom_btn'):
            return
        btn = self.scroll_bottom_btn
        pos = self.chat_area.mapTo(self, self.chat_area.rect().bottomRight())
        btn.move(pos.x() - btn.width() - 20, pos.y() - btn.height() - 20)

    def _on_scroll_changed(self):
        """滚动位置变化时，决定是否显示浮动按钮"""
        sb = self.chat_area.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 30
        if at_bottom:
            self.scroll_bottom_btn.hide()
        else:
            self.scroll_bottom_btn.show()
            self.scroll_bottom_btn.raise_()
            self._position_scroll_btn()

    def _scroll_to_bottom(self):
        """滚动到聊天区底部。

        只操作滚动条，不移动 QTextBrowser 文本光标。移动光标会让正文里
        获得焦点的 Copy 链接被框选，并可能把视口拉回该链接位置。
        """
        def force_bottom(final=False):
            sb = self.chat_area.verticalScrollBar()
            self.chat_area.clearFocus()
            self.scroll_bottom_btn.setFocus()
            sb.setValue(sb.maximum())
            sb.setSliderPosition(sb.maximum())
            sb.triggerAction(sb.SliderAction.SliderToMaximum)
            if final and sb.value() >= sb.maximum() - 30:
                self.scroll_bottom_btn.hide()
            elif hasattr(self, "scroll_bottom_btn"):
                self._position_scroll_btn()
                self.scroll_bottom_btn.raise_()

        for delay in (0, 16, 50, 120, 250, 400, 700):
            QTimer.singleShot(delay, lambda final=(delay == 700): force_bottom(final))


    # ── 输入区 ──

    def _build_input_area(self, parent_layout):
        wrapper = QWidget()
        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(48, 8, 48, 12)

        # 图片预览区
        self.image_preview_area = QWidget()
        self.image_preview_area.setVisible(False)
        self.image_preview_layout = QHBoxLayout(self.image_preview_area)
        self.image_preview_layout.setContentsMargins(8, 4, 8, 0)
        self.image_preview_layout.setSpacing(6)
        self.image_preview_layout.addStretch()
        wrapper_layout.addWidget(self.image_preview_area)

        # 命令确认条（默认隐藏；AI 想 run_command 时由 _on_confirm_request 显示）
        self._build_command_confirm_bar()
        wrapper_layout.addWidget(self.command_confirm_bar, 0, Qt.AlignHCenter)

        # edit_file diff 预览卡（默认隐藏；AI 想改文件时由 _on_edit_confirm_request 显示）
        self._build_edit_confirm_bar()
        wrapper_layout.addWidget(self.edit_confirm_bar, 0, Qt.AlignHCenter)

        # 圆角容器
        container = QWidget()
        container.setObjectName("inputContainer")
        self.input_container = container
        container.setFixedWidth(920)
        container.setMinimumHeight(104)
        container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        container_layout = QHBoxLayout(container)
        container_layout.setContentsMargins(10, 6, 10, 6)
        container_layout.setSpacing(6)

        # 输入框（左侧留 padding 给加号按钮）
        self.entry = DragDropTextEdit()
        self.entry.setObjectName("inputEdit")
        self.entry.setPlaceholderText("Send a message")
        entry_font = QFont("Microsoft YaHei")
        entry_font.setPixelSize(16)
        entry_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        entry_font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        self.entry.setFont(entry_font)
        self.entry.setMaximumHeight(132)
        self.entry.setMinimumHeight(82)
        self.entry.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.entry.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.entry.textChanged.connect(self._on_input_change)
        container_layout.addWidget(self.entry, 1)

        # "+" 按钮（悬浮在输入框左下角）——点击弹菜单：上传图片 / 导入项目
        # 之所以变量名仍叫 img_btn 是为了不动 eventFilter 里的几处兼容代码
        self.img_btn = QPushButton(self.entry)
        self.img_btn.setToolTip("上传图片 / 导入项目")
        self.img_btn.setCursor(Qt.PointingHandCursor)
        self.img_btn.setFixedSize(28, 28)
        self._style_img_btn()
        self.img_btn.clicked.connect(self._show_plus_menu)
        self.img_btn.move(4, 40)

        # 麦克风按钮 + TTS 切换按钮（语音依赖装上才显示）
        # 初始 y 和 img_btn 一致；resize 事件里也会一起跟随
        if _VOICE_AVAILABLE:
            self.mic_btn = QPushButton(self.entry)
            self.mic_btn.setCursor(Qt.PointingHandCursor)
            self.mic_btn.setFixedSize(28, 28)
            self._style_mic_btn(recording=False)
            self.mic_btn.clicked.connect(self._on_mic_click)
            self.mic_btn.move(36, max(40, self.entry.height() - 40))

            self.tts_btn = QPushButton(self.entry)
            self.tts_btn.setCursor(Qt.PointingHandCursor)
            self.tts_btn.setCheckable(True)
            self.tts_btn.setFixedSize(28, 28)
            self._style_tts_btn(enabled=self._tts_enabled)
            self.tts_btn.clicked.connect(self._on_tts_click)
            self.tts_btn.move(68, max(40, self.entry.height() - 40))

        # img_btn 创建完成后再装事件过滤器，避免 eventFilter 提前触发时引用未定义属性
        self.entry.installEventFilter(self)
        self.img_btn.installEventFilter(self)
        if _VOICE_AVAILABLE:
            self.mic_btn.installEventFilter(self)
            self.tts_btn.installEventFilter(self)

        # @文件名补全器
        self._file_completer = FileCompleter(self)
        self._file_completer.item_selected.connect(self._on_file_completer_selected)
        self._file_completer.lister = self._list_project_dir  # 逐层浏览：列单层目录的回调
        self._file_completer.reposition = self._position_completer  # 高度变后重定位（底部贴输入框）
        self._file_completer_files = None  # 缓存的文件列表
        self._file_completer_cache_key = None  # 缓存对应的项目路径
        self._apply_completer_theme()  # 初始就给 delegate 设好 text_color/sel_bg，否则选中项白字看不清

        # 发送按钮
        self.send_btn = QPushButton()
        self.send_btn.setIcon(self._icon_arrow)
        self.send_btn.setIconSize(QSize(16, 16))
        self.send_btn.setObjectName("sendBtn")
        self.send_btn.setCursor(Qt.PointingHandCursor)
        self.send_btn.clicked.connect(self._on_send_click)
        # 底部留 8px 间距
        btn_wrapper = QWidget()
        btn_wrapper_layout = QVBoxLayout(btn_wrapper)
        btn_wrapper_layout.setContentsMargins(0, 0, 8, 6)
        btn_wrapper_layout.setSpacing(0)
        btn_wrapper_layout.addStretch()
        btn_wrapper_layout.addWidget(self.send_btn)
        container_layout.addWidget(btn_wrapper, 0, Qt.AlignBottom)
        # 初始化样式
        self._update_btn_state("disabled")

        wrapper_layout.addWidget(container, 0, Qt.AlignHCenter)
        parent_layout.addWidget(wrapper)

    def _resize_input_container(self):
        if not hasattr(self, "input_container") or not hasattr(self, "chat_area"):
            return
        available = max(0, self.chat_area.viewport().width() - 180)
        width = max(620, min(980, available))
        self.input_container.setFixedWidth(width)
        if hasattr(self, "command_confirm_bar"):
            self.command_confirm_bar.setFixedWidth(width)
        if hasattr(self, "edit_confirm_bar"):
            self.edit_confirm_bar.setFixedWidth(width)
        # 输入框宽度变了：补全浮窗若开着，等布局刷完（singleShot 0）再按 input_container
        # 的新尺寸/位置重对齐——立即调会拿到布局未稳定的旧几何，导致位置偏
        if hasattr(self, "_file_completer") and self._file_completer.isVisible():
            from PySide6.QtCore import QTimer as _QTimer
            _QTimer.singleShot(0, self._position_completer)


    def _build_project_indicator(self, parent_layout):
        """输入框下方显示当前项目路径，点击可切换。

        - 有项目时显示 "📁 D:/path/to/project"
        - 无项目时显示 "无项目 · 全局工作区"
        - 路径过长会做中部省略，鼠标 hover 看完整路径
        - 单击：弹出项目切换菜单（与侧栏右键的菜单一致）
        """
        wrap = QWidget()
        wrap.setObjectName("projectIndicatorWrap")
        h = QHBoxLayout(wrap)
        h.setContentsMargins(20, 4, 20, 0)
        h.setSpacing(0)

        # 用 QPushButton 是因为它天然支持 hover / cursor / clicked，比 QLabel 干净
        self.project_btn = QPushButton()
        self.project_btn.setObjectName("projectIndicatorBtn")
        self.project_btn.setCursor(Qt.PointingHandCursor)
        self.project_btn.setIconSize(QSize(14, 14))
        self.project_btn.clicked.connect(self._show_project_menu)
        h.addStretch()
        h.addWidget(self.project_btn)
        h.addStretch()

        parent_layout.addWidget(wrap)
        self._refresh_project_indicator()

    def _refresh_project_indicator(self):
        """根据当前项目刷新指示条的文本、图标、tooltip。
        在以下时机调用：__init__、_switch_project、_remove_current_project、_apply_theme。
        """
        if not hasattr(self, "project_btn"):
            return
        from .. import projects as _projects
        current = _projects.get_current()
        if current:
            display = self._abbreviate_path(current, max_chars=60)
            self.project_btn.setText(f"  {display}  ▾")
            self.project_btn.setIcon(self._svg_icon("folder_lucide.svg", self._t("text_dim")))
            self.project_btn.setToolTip(f"当前项目：{current}\n（点击切换 / 添加 / 移除项目）")
        else:
            self.project_btn.setText("  无项目 · 全局工作区  ▾")
            self.project_btn.setIcon(self._svg_icon("circle_lucide.svg", self._t("text_subtle")))
            self.project_btn.setToolTip("当前不在任何项目中\n（点击选择 / 添加项目）")
        # 内联样式（用 footer 的颜色 token，跟随主题）
        self.project_btn.setStyleSheet(
            f"QPushButton#projectIndicatorBtn {{"
            f"  background: transparent; border: 1px solid transparent; border-radius: 8px;"
            f"  color: {self._t('text_dim')}; font-size: 11px; padding: 3px 10px;"
            f"  text-align: left;"
            f"}}"
            f"QPushButton#projectIndicatorBtn:hover {{"
            f"  background: {self._t('history_hover_bg')};"
            f"  color: {self._t('text')};"
            f"  border-color: {self._t('sidebar_border')};"
            f"}}"
        )

    @staticmethod
    def _abbreviate_path(path, max_chars=60):
        """路径太长时做中部省略，让"盘符 + 项目名"两头都看得见。"""
        if not path or len(path) <= max_chars:
            return path
        keep_head = max_chars // 2 - 2
        keep_tail = max_chars - keep_head - 3
        return f"{path[:keep_head]}...{path[-keep_tail:]}"

    def _build_footer(self, parent_layout):
        from PySide6.QtWidgets import QHBoxLayout, QWidget as _W
        footer_widget = _W()
        footer_widget.setFixedHeight(28)
        footer_layout = QHBoxLayout(footer_widget)
        footer_layout.setContentsMargins(20, 0, 20, 0)
        footer_layout.setSpacing(0)

        footer = QLabel("灵犀 AI · local & cloud · tools enabled")
        footer.setObjectName("footerLabel")
        footer.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        footer_layout.addWidget(footer, 1)

        self.token_usage_label = QLabel("Token: -")
        self.token_usage_label.setObjectName("tokenUsageLabel")
        self.token_usage_label.setAlignment(Qt.AlignVCenter | Qt.AlignRight)
        footer_layout.addWidget(self.token_usage_label)

        parent_layout.addWidget(footer_widget)

    # ── 事件处理 ──

    def eventFilter(self, obj, event):
        # 确认条按键（1/2/3/Esc）派到 mixin 处理
        if self._handle_confirm_bar_keys(obj, event):
            return True

        if hasattr(self, 'settings_btn') and obj == self.settings_btn and self._settings_btn_icon:
            if event.type() == event.Type.Enter:
                self.settings_btn.setIcon(self._settings_btn_icon_hover)
                return False
            elif event.type() == event.Type.Leave:
                self.settings_btn.setIcon(self._settings_btn_icon)
                return False

        # img_btn hover 图标切换
        if hasattr(self, 'img_btn') and obj == self.img_btn and hasattr(self, '_img_btn_icon'):
            if event.type() == event.Type.Enter:
                self.img_btn.setIcon(self._img_btn_icon_hover)
                return False
            elif event.type() == event.Type.Leave:
                self.img_btn.setIcon(self._img_btn_icon)
                return False
        if hasattr(self, 'mic_btn') and obj == self.mic_btn and hasattr(self, '_mic_btn_icon'):
            if event.type() == event.Type.Enter:
                self.mic_btn.setIcon(self._mic_btn_icon_hover)
                return False
            elif event.type() == event.Type.Leave:
                self.mic_btn.setIcon(self._mic_btn_icon)
                return False
        if hasattr(self, 'tts_btn') and obj == self.tts_btn and hasattr(self, '_tts_btn_icon'):
            if event.type() == event.Type.Enter:
                self.tts_btn.setIcon(self._tts_btn_icon_hover)
                return False
            elif event.type() == event.Type.Leave:
                self.tts_btn.setIcon(self._tts_btn_icon)
                return False
        """Enter 发送，Shift+Enter 换行，Ctrl+V 粘贴图片"""
        if not hasattr(self, 'entry'):
            return super().eventFilter(obj, event)

        if obj == self.entry and event.type() == event.Type.Resize:
            y = self.entry.height() - 40
            self.img_btn.move(4, y)
            if hasattr(self, 'mic_btn'):
                self.mic_btn.move(36, y)
            if hasattr(self, 'tts_btn'):
                self.tts_btn.move(68, y)
        if obj == self.entry and event.type() == event.Type.KeyPress:
            # @文件补全浮窗激活时，拦截导航/确认/取消键，不触发发送或换行
            if (hasattr(self, '_file_completer')
                    and self._file_completer.isVisible()):
                key = event.key()
                if key == Qt.Key_Up:
                    self._file_completer.navigate_up()
                    return True
                if key == Qt.Key_Down:
                    self._file_completer.navigate_down()
                    return True
                if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab):
                    self._file_completer.confirm_selection()
                    return True
                if key == Qt.Key_Escape:
                    self._file_completer.hide()
                    return True
            if event.key() == Qt.Key_Return and not event.modifiers() & Qt.ShiftModifier:
                if self._has_input or self._pending_images:
                    self._send_message()
                return True
            # Ctrl+V 粘贴图片
            if event.key() == Qt.Key_V and event.modifiers() & Qt.ControlModifier:
                from PySide6.QtWidgets import QApplication
                clipboard = QApplication.clipboard()
                mime = clipboard.mimeData()
                if mime.hasImage():
                    img = clipboard.image()
                    if not img.isNull():
                        self._add_image_from_qimage(img)
                        return True
                # 粘贴的是文件路径（如从资源管理器复制的文件）
                if mime.hasUrls():
                    handled = False
                    for url in mime.urls():
                        path = url.toLocalFile()
                        if path and path.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp')):
                            self._add_pending_image(path)
                            handled = True
                    if handled:
                        return True
                # 普通文本粘贴，走默认处理
        return super().eventFilter(obj, event)

    def _add_image_from_qimage(self, qimage):
        """从 QImage（剪贴板截图等）添加待发送图片"""
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False, dir=os.environ.get("TEMP", "."))
        tmp.close()
        qimage.save(tmp.name, "PNG")
        self._add_pending_image(tmp.name)

    def _on_input_change(self):
        text = self.entry.toPlainText().strip()
        has_input = bool(text) or bool(self._pending_images)
        if has_input != self._has_input:
            self._has_input = has_input
            if not self.is_generating:
                self._update_btn_state("enabled" if has_input else "disabled")

        # 自动调整高度（不低于 80）
        doc_height = self.entry.document().size().height() + 16
        self.entry.setMinimumHeight(int(min(max(doc_height, 80), 150)))

        # @文件补全触发
        self._check_at_mention()

    # ── @文件名补全 ──

    def _check_at_mention(self):
        """检测输入框光标前是否有 @文件名 上下文，有则弹出补全浮窗。"""
        if not hasattr(self, '_file_completer'):
            return
        mention = self._get_active_mention()
        if mention is not None:
            _pos, partial = mention
            if not self._file_completer.isVisible():
                self._file_completer.open_root()   # 首次打 @：从项目根开始逐层浏览
            self._file_completer.filter_and_show(partial)
            self._position_completer()
        else:
            self._file_completer.hide()

    def _get_active_mention(self):
        """检测光标前是否有未完成的 @文件名 提及。

        规则：@ 前是行首/空白/非字母数字（排除 email 和装饰器），
        @ 后到光标间是路径字符（不含空白和 @），且后面要么是光标末尾，
        要么是非路径字符（已完成的 @path 后面跟空格则不算）。

        返回 (at_pos, partial_path) 或 None。
        """
        cursor = self.entry.textCursor()
        pos = cursor.position()
        if pos == 0:
            return None
        text_before = self.entry.toPlainText()[:pos]
        # 从光标往前找最近的 @
        idx = text_before.rfind('@')
        if idx < 0:
            return None
        # @ 前必须是行首 / 空白 / 非字母数字（排除 user@domain 和 @decorator）
        if idx > 0 and text_before[idx - 1].isalnum():
            return None
        partial = text_before[idx + 1:]
        # @ 后不能包含空白或 @
        if ' ' in partial or '\t' in partial or '\n' in partial or '@' in partial:
            return None
        return (idx, partial)

    def _list_project_dir(self, rel_dir):
        """列出 项目根/rel_dir 的直接子项 [(name, is_dir)]，跳噪声目录，文件夹优先。
        逐层浏览：@ 浮窗每次只列一层，选文件夹进入下一层。"""
        project_root = getattr(state, 'current_project', None) or os.getcwd()
        target = os.path.join(project_root, rel_dir) if rel_dir else project_root
        ignore = {
            ".git", ".hg", ".svn", "node_modules", "bower_components",
            "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
            ".venv", "venv", "env", ".env",
            "build", "dist", "target", "out",
            ".next", ".nuxt", ".idea", ".vscode",
        }
        try:
            names = os.listdir(target)
        except Exception:
            return []
        out = []
        for n in names:
            if n in ignore:
                continue
            is_dir = os.path.isdir(os.path.join(target, n))
            out.append((n, is_dir))
        out.sort(key=lambda t: (not t[1], t[0].lower()))  # 文件夹优先，再按名
        return out

    def _position_completer(self):
        """把补全浮窗定位到输入框【上方】，宽度/左右与输入框外框对齐。
        基准用 input_container（带圆角的可见外框），不能用 entry——entry 是框内的
        文本控件、比外框窄。浮窗是主窗口子控件，用相对主窗口的本地坐标 move + raise_。"""
        anchor = getattr(self, "input_container", self.entry)
        self._file_completer.setFixedWidth(anchor.width())  # 与输入框外框等宽
        ph = self._file_completer.height()
        top_left = anchor.mapTo(self, anchor.rect().topLeft())
        self._file_completer.move(top_left.x(), top_left.y() - ph - 4)
        self._file_completer.raise_()

    def _apply_completer_theme(self):
        """按当前主题给文件补全器涂色：外框 + hover 走 QSS，文字/选中色交给 delegate
        （delegate 自绘文字，所以颜色不能只靠 QSS 的 item color）。"""
        if self.theme == "dark":
            frame_bg, frame_border = "#1e1e2e", "#45475a"
            hover_bg, text_color, sel_bg = "#313244", "#cdd6f4", "#585b70"
        else:
            frame_bg, frame_border = "#ffffff", "#dddddd"
            hover_bg, text_color, sel_bg = "#e6f0ff", "#333333", "#cfe3ff"
        self._file_completer.setStyleSheet(
            f"FileCompleter {{ background: {frame_bg}; border: 1px solid {frame_border}; "
            f"border-radius: 12px; padding: 4px; }}")
        self._file_completer.list_widget.setStyleSheet(
            "QListWidget { background: transparent; border: none; outline: none; }"
            "QListWidget::item { border-radius: 6px; }"
            f"QListWidget::item:hover {{ background: {hover_bg}; }}")
        dlg = self._file_completer.list_widget.itemDelegate()
        if dlg is not None and hasattr(dlg, "text_color"):
            dlg.text_color = text_color
            dlg.sel_bg = sel_bg
            dlg.folder_icon = self._svg_icon("folder_lucide.svg", "#3b82f6").pixmap(16, 16)
            dlg.file_icon = self._svg_icon("file_text_lucide.svg", text_color).pixmap(16, 16)

    def _on_file_completer_selected(self, relative_path: str):
        """补全浮窗选中文件 → 替换输入框中的 @partial 为 @相对路径。"""
        mention = self._get_active_mention()
        if mention is None:
            return
        at_pos, _partial = mention
        cursor = self.entry.textCursor()
        # 选中从 @ 位置到当前光标之间的文本
        cursor.setPosition(at_pos)
        cursor.setPosition(self.entry.textCursor().position(), QTextCursor.KeepAnchor)
        # 替换为 "@相对路径 "，并给引用上强调色 + 加粗（视觉标识这是文件引用）；
        # 随后的空格用默认格式插入，避免用户接着打字时文字继续带色
        from PySide6.QtGui import QTextCharFormat, QColor
        ref_fmt = QTextCharFormat()
        ref_fmt.setForeground(QColor("#3b82f6"))
        ref_fmt.setFontWeight(700)
        self.entry.blockSignals(True)
        cursor.insertText(f"@{relative_path}", ref_fmt)
        cursor.insertText(" ", QTextCharFormat())
        self.entry.setTextCursor(cursor)                     # 同步光标到空格后
        self.entry.setCurrentCharFormat(QTextCharFormat())   # 重置输入框当前格式，后续打字恢复默认色
        self.entry.blockSignals(False)
        self.entry.setFocus()

    def _expand_file_mentions(self, text: str) -> str:
        """扫描 @相对路径，【不注入文件内容】，而是末尾追加强提示，让 AI 自己用
        read_file / list_directory 工具读取（历史干净 + 与工具体系一致）。"""
        import re as _re
        project_root = getattr(state, 'current_project', None) or os.getcwd()
        pattern = _re.compile(r'(?<!\S)@([^\s@]+)')
        refs = []
        for m in pattern.finditer(text):
            rel_path = m.group(1)
            abs_path = os.path.join(project_root, rel_path)
            if os.path.isdir(abs_path):
                refs.append((rel_path, "目录", "list_directory"))
            elif os.path.isfile(abs_path):
                refs.append((rel_path, "文件", "read_file"))
        if not refs:
            return text
        lines = [f"  - {rel}（{kind}）→ 用 {tool} 读取" for rel, kind, tool in refs]
        hint = (
            "\n\n[用户用 @ 引用了以下文件/目录，请【务必先调用对应工具读取其内容】"
            "再据此回答，不要凭空作答]：\n" + "\n".join(lines)
        )
        return text + hint

    def _force_stop_generation(self):
        """强制停止当前生成，立即更新 UI 状态。

        与 _on_send_click 中的 stop 不同：这里会同步把 is_generating 置 False
        并立即刷新按钮/输入框，这样调用方（切会话 / 新对话等）可以继续往下执行，
        而不用等 worker 线程退出。
        """
        if not self.is_generating:
            return
        state.stop_flag = True
        self._release_pending_confirm()
        self._release_pending_edit()
        # 立即标记生成结束——_on_finished 再被触发时会跳过重复处理
        self.is_generating = False
        self._ai_reply_start = None
        # 对齐 _on_finished 的按钮恢复（输入框有内容则 enabled）。
        # 注意：灵犀原写的 self.input_box 不存在（真名是 input_container），且那套
        # setProperty/unpolish 多余——_update_btn_state 已经够恢复按钮态。
        self._update_btn_state("enabled" if self._has_input else "disabled")

    def _on_send_click(self):
        if self.is_generating:
            self._force_stop_generation()
        elif self._has_input or self._pending_images:
            self._send_message()

    def _insert_image_path(self, path):
        """从本地路径加载图片并插入聊天区（带可点击缩略图）"""
        if not path or not os.path.exists(path):
            return
        if not hasattr(self, "_image_paths"):
            self._image_paths = {}
        img = QImage(path)
        if img.isNull():
            return
        scroll = self._scroll_guard()
        # 缩略图最长边 480px，保持原始比例和清晰度
        MAX_SIDE = 480
        if img.width() > MAX_SIDE or img.height() > MAX_SIDE:
            if img.width() >= img.height():
                img = img.scaledToWidth(MAX_SIDE, Qt.SmoothTransformation)
            else:
                img = img.scaledToHeight(MAX_SIDE, Qt.SmoothTransformation)
        import uuid as _uuid
        img_id = _uuid.uuid4().hex[:8]
        self._image_paths[img_id] = path
        name = f"ai_img_{img_id}"
        self.chat_area.document().addResource(
            self.chat_area.document().ResourceType.ImageResource,
            name, img
        )
        cursor = self.chat_area.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.chat_area.setTextCursor(cursor)
        cursor.insertHtml(
            f'<a href="action:show_image:{img_id}">'
            f'<img src="{name}" /></a><br>'
        )
        scroll()

    def _insert_images_in_chat(self, images):
        """在聊天区插入图片缩略图（点击可看原图）"""
        if not hasattr(self, "_image_paths"):
            self._image_paths = {}  # img_id -> 原图路径

        scroll = self._scroll_guard()
        cursor = self.chat_area.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.chat_area.setTextCursor(cursor)
        for path, _b64 in images:
            img = QImage(path)
            if img.isNull():
                continue
            # 缩略图最长边 480px，保持比例
            MAX_SIDE = 480
            if img.width() > MAX_SIDE or img.height() > MAX_SIDE:
                if img.width() >= img.height():
                    img = img.scaledToWidth(MAX_SIDE, Qt.SmoothTransformation)
                else:
                    img = img.scaledToHeight(MAX_SIDE, Qt.SmoothTransformation)
            import uuid as _uuid
            img_id = _uuid.uuid4().hex[:8]
            self._image_paths[img_id] = path
            name = f"user_img_{img_id}"
            self.chat_area.document().addResource(
                self.chat_area.document().ResourceType.ImageResource,
                name, img
            )
            # 用 <a><img></a> 让图片可点击
            cursor.insertHtml(
                f'<a href="action:show_image:{img_id}">'
                f'<img src="{name}" /></a><br>'
            )
        scroll()

    def _update_btn_state(self, state):
        self.send_btn.setProperty("state", state)
        if state == "stop":
            self.send_btn.setIcon(self._icon_pause)
        else:
            self.send_btn.setIcon(self._icon_arrow)
        self.send_btn.setIconSize(QSize(16, 16))
        # 强制刷新样式
        self.send_btn.style().unpolish(self.send_btn)
        self.send_btn.style().polish(self.send_btn)

    def _toggle_sidebar(self):
        self._sidebar_visible = not self._sidebar_visible
        self.sidebar.setVisible(self._sidebar_visible)
        for delay in (0, 30, 120):
            QTimer.singleShot(delay, self._refresh_responsive_layout)

    def _refresh_responsive_layout(self):
        self._resize_input_container()
        self._position_empty_state()
        if hasattr(self, 'scroll_bottom_btn'):
            self._position_scroll_btn()
        self._refresh_header_compactness()


    def _pick_image(self):
        if self.is_generating:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择图片", "",
            "图片文件 (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;所有文件 (*)"
        )
        for path in paths:
            self._add_pending_image(path)

    def _show_plus_menu(self):
        """点 + 按钮：弹菜单选「上传图片 / 导入项目」。"""
        if self.is_generating:
            return
        menu = QMenu(self)

        a_img = QAction("上传图片", menu)
        a_img.setIcon(self._svg_icon("image_lucide.svg", self._t("menu_text")))
        a_img.triggered.connect(self._pick_image)
        menu.addAction(a_img)

        a_proj = QAction("导入项目", menu)
        a_proj.setIcon(self._svg_icon("folder_open_lucide.svg", self._t("menu_text")))
        a_proj.triggered.connect(self._add_project)
        menu.addAction(a_proj)

        # 菜单弹在按钮上方（避免遮挡输入框）
        anchor = self.img_btn.mapToGlobal(self.img_btn.rect().topLeft())
        size_hint = menu.sizeHint()
        menu.exec(anchor - QPoint(0, size_hint.height()))

    # ── 语音 ──

    def _on_mic_click(self):
        if not _VOICE_AVAILABLE or self._recorder is None:
            return
        if self._recorder.is_recording:
            # 第二次点击：停止 → 识别
            audio = self._recorder.stop()
            self._style_mic_btn(recording=False)
            if audio is None or len(audio) < 16000 * 0.3:  # 少于 0.3s 视为误点
                self.show_message("\n⚠️ 录音过短\n", "tool_result")
                return
            # 用 thinking_indicator 显示"识别中..."；STT 完成时会自动移除
            self.bridge.append_signal.emit("识别中...\n", "thinking_indicator")
            self.mic_btn.setEnabled(False)
            self._stt.transcribe_async(audio)
        else:
            # 第一次点击：开始录音
            if self._recorder.start():
                self._style_mic_btn(recording=True)
            else:
                self.show_message("\n⚠️ 录音启动失败（麦克风不可用）\n", "tool_result")

    def _stt_done(self):
        """识别完成（成功或失败）：移除指示器、恢复按钮可点。"""
        self.bridge.remove_thinking.emit()
        if hasattr(self, "mic_btn"):
            self.mic_btn.setEnabled(True)

    def _on_stt_transcribed(self, text):
        self._stt_done()
        # 识别结果填进输入框
        cur = self.entry.toPlainText().rstrip()
        new_text = (cur + " " + text) if cur else text
        self.entry.setPlainText(new_text)
        self.entry.moveCursor(QTextCursor.End)
        self.entry.setFocus()

    def _on_stt_failed(self, err):
        self._stt_done()
        self.show_message(f"\n⚠️ 语音识别失败: {err}\n", "tool_result")

    def _on_tts_click(self):
        new_state = not self._tts_enabled
        from ..paths import logger as _lg
        _lg.info(f"[TTS click] new_state={new_state}")

        # 开启 TTS 时检测 GPT-SoVITS 服务
        if new_state:
            try:
                from ..config import GPT_SOVITS_URL
                alive = self._check_gpt_sovits_alive(GPT_SOVITS_URL)
                _lg.info(f"[TTS click] GPT-SoVITS alive @ {GPT_SOVITS_URL} = {alive}")
                if not alive:
                    # 服务没起 → 弹对话框询问是否启动
                    self._prompt_launch_gpt_sovits()
                    return  # 不管用户选启动还是取消，本次点击都不直接开 TTS
            except Exception as e:
                _lg.warning(f"[TTS click] 检测时异常: {e}")
                self._show_toast(f"⚠️ 检测 GPT-SoVITS 失败: {e}", duration=4000)
                return

        self._tts_enabled = new_state
        self._style_tts_btn(enabled=self._tts_enabled)
        if not self._tts_enabled and self._tts is not None:
            self._tts.stop()

    def _prompt_launch_gpt_sovits(self):
        """服务未启动时的对话框：询问是否现在启动 + 校验配置完整性。"""
        reply = QMessageBox.question(
            self,
            "GPT-SoVITS 未启动",
            "语音模块尚未启动。\n\n是否现在启动？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return

        # 校验配置是否齐全（从 config.json 实时读，避免依赖启动时常量）
        missing = self._check_gpt_sovits_config()
        if missing:
            QMessageBox.warning(
                self, "配置不完整",
                "无法启动，以下字段未在设置里填好：\n\n"
                + "\n".join(f"  • {m}" for m in missing)
                + "\n\n请先打开 ⚙ 设置 → 「语音模块（GPT-SoVITS）」 补齐后保存。",
            )
            return

        launcher = getattr(self, "_gpt_sovits_launcher", None)
        if launcher is None:
            QMessageBox.warning(self, "出错", "找不到语音模块启动器。")
            return

        # 一旦 launcher 进入 running 状态，自动启用 TTS 按钮（一次性钩子）
        launcher.status_changed.connect(self._on_launcher_status_for_tts)

        # 读最新 config 的路径
        cfg = self._read_config_now()
        launcher.start(
            install_dir=cfg.get("gpt_sovits_install_dir", ""),
            gpt_model=cfg.get("gpt_sovits_gpt_model", ""),
            sovits_model=cfg.get("gpt_sovits_sovits_model", ""),
        )
        self._show_toast("🟡 GPT-SoVITS 启动中…（约 30-60 秒）", duration=5000)

    def _on_launcher_status_for_tts(self, state, msg):
        """launcher 进入 running 时自动打开 TTS。"""
        if state == "running":
            self._tts_enabled = True
            self._style_tts_btn(enabled=True)
            self._show_toast("🟢 GPT-SoVITS 已启动，朗读已开启", duration=3000)
            # 一次性，断开钩子
            try:
                launcher = getattr(self, "_gpt_sovits_launcher", None)
                if launcher is not None:
                    launcher.status_changed.disconnect(self._on_launcher_status_for_tts)
            except Exception:
                pass
        elif state == "failed":
            self._show_toast(f"🔴 启动失败：{msg}", duration=6000)
            try:
                launcher = getattr(self, "_gpt_sovits_launcher", None)
                if launcher is not None:
                    launcher.status_changed.disconnect(self._on_launcher_status_for_tts)
            except Exception:
                pass

    def _read_config_now(self):
        """从磁盘实时读 config.json，避免依赖启动时缓存的常量。"""
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                return json.load(f)
        except Exception:
            return {}

    def _check_gpt_sovits_config(self):
        """返回缺失字段的中文名列表；空列表 = 配置齐。"""
        cfg = self._read_config_now()
        required = [
            ("gpt_sovits_install_dir", "GPT-SoVITS 安装目录"),
            ("gpt_sovits_gpt_model", "GPT 权重相对路径"),
            ("gpt_sovits_sovits_model", "SoVITS 权重相对路径"),
            ("gpt_sovits_ref_audio", "参考音频文件"),
            ("gpt_sovits_prompt_text", "参考音频对应文本"),
        ]
        missing = []
        for key, name in required:
            val = (cfg.get(key) or "").strip()
            if not val:
                missing.append(name)
                continue
            # 路径字段还要检查实际存在
            if key == "gpt_sovits_install_dir" and not os.path.isdir(val):
                missing.append(f"{name}（路径不存在: {val}）")
            elif key == "gpt_sovits_ref_audio" and not os.path.isfile(val):
                missing.append(f"{name}（文件不存在: {val}）")
        return missing

    def _check_gpt_sovits_alive(self, url, timeout=1.5):
        """快速 ping 一下 GPT-SoVITS API。任何响应都视为存活；连接错误视为死。"""
        from ..paths import logger as _lg
        try:
            import requests
            r = requests.get(url.rstrip("/") + "/", timeout=timeout)
            _lg.info(f"[ping] {url} → HTTP {r.status_code}")
            return True
        except Exception as e:
            _lg.info(f"[ping] {url} → 失败: {type(e).__name__}: {e}")
            return False

    # 注：_show_toast 在文件下方（line ~3011）已有完整实现（浮动 QLabel + duration 参数），不要在此重复定义

    def _add_pending_image(self, path):
        try:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
        except Exception:
            return

        self._pending_images.append((path, b64))
        self._refresh_image_preview()
        self._check_input_state()

    def _refresh_image_preview(self):
        # 清空旧预览
        while self.image_preview_layout.count() > 1:
            item = self.image_preview_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for i, (path, _b64) in enumerate(self._pending_images):
            thumb = QWidget()
            thumb_layout = QVBoxLayout(thumb)
            thumb_layout.setContentsMargins(0, 0, 0, 0)
            thumb_layout.setSpacing(2)

            # 缩略图
            lbl = QLabel()
            pix = QPixmap(path).scaled(60, 60, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            lbl.setPixmap(pix)
            lbl.setFixedSize(64, 64)
            lbl.setStyleSheet(
                f"border: 1px solid {self._t('img_thumb_border')}; border-radius: 8px; padding: 2px; "
                f"background: {self._t('img_thumb_bg')};"
            )
            lbl.setAlignment(Qt.AlignCenter)
            thumb_layout.addWidget(lbl)

            # 删除按钮
            del_btn = QPushButton("×")
            del_btn.setFixedSize(20, 20)
            del_btn.setCursor(Qt.PointingHandCursor)
            del_btn.setStyleSheet(
                f"QPushButton {{ background: {self._t('img_del_bg')}; color: {self._t('img_del_text')}; border: none; "
                f"border-radius: 10px; font-size: 12px; font-weight: bold; }}"
                f"QPushButton:hover {{ background: {self._t('img_del_hover_bg')}; }}"
            )
            idx = i
            del_btn.clicked.connect(lambda checked=False, ii=idx: self._remove_pending_image(ii))
            thumb_layout.addWidget(del_btn, 0, Qt.AlignCenter)

            self.image_preview_layout.insertWidget(self.image_preview_layout.count() - 1, thumb)

        self.image_preview_area.setVisible(len(self._pending_images) > 0)

    def _remove_pending_image(self, index):
        if 0 <= index < len(self._pending_images):
            self._pending_images.pop(index)
            self._refresh_image_preview()
            self._check_input_state()

    def _check_input_state(self):
        """文本或图片有任一存在即可发送"""
        text = self.entry.toPlainText().strip()
        has_input = bool(text) or bool(self._pending_images)
        if has_input != self._has_input:
            self._has_input = has_input
            if not self.is_generating:
                self._update_btn_state("enabled" if has_input else "disabled")

    # ── 发送消息 ──

    def submit_from_remote(self, text: str):
        """遥控消息注入入口（可从任意线程调用，通过 Signal 跨线程）。"""
        self.bridge.remote_submit.emit(text)

    def _on_remote_submit(self, text: str):
        """远程消息注入槽（主线程）。"""
        if not text or self.is_generating:
            return
        state.remote_session = True
        self._do_send(text)

    def _send_message(self):
        text = self.entry.toPlainText().strip()
        images = self._pending_images[:]
        if (not text and not images) or self.is_generating:
            return
        # GUI 专属清理
        self.entry.clear()
        self._pending_images.clear()
        self._refresh_image_preview()
        self._has_input = False
        self._do_send(text, images)

    def _append_user_text(self, text):
        """显示用户消息：@文件引用渲染成蓝色加粗，其余用普通 user_msg 样式。"""
        import re as _re
        from PySide6.QtGui import QTextCharFormat, QFont as QF
        scroll = self._scroll_guard()
        cursor = self.chat_area.textCursor()
        cursor.movePosition(QTextCursor.End)

        def _fmt(color, bold=False):
            f = QTextCharFormat()
            font = QF("Microsoft YaHei")
            font.setPixelSize(15)
            font.setWeight(QF.Weight.Bold if bold else QF.Weight.Normal)
            font.setStyleStrategy(QF.StyleStrategy.PreferAntialias)
            # 必须和聊天区基础字体 / _make_format 一样设 PreferNoHinting，否则用户消息
            # 用默认 hinting、AI 消息用 NoHinting，两套字脚渲染并排 → 高分屏下笔画粗细不一
            font.setHintingPreference(QF.HintingPreference.PreferNoHinting)
            f.setFont(font)
            f.setForeground(QColor(color))
            return f

        normal = _fmt(self._t("user_msg"))
        ref = _fmt("#3b82f6", bold=True)
        pattern = _re.compile(r'(?<!\S)@[^\s@]+')
        pos = 0
        for m in pattern.finditer(text):
            if m.start() > pos:
                cursor.insertText(text[pos:m.start()], normal)
            cursor.insertText(m.group(0), ref)
            pos = m.end()
        if pos < len(text):
            cursor.insertText(text[pos:], normal)
        scroll()

    def _do_send(self, text: str, images=None):
        """核心发送逻辑，GUI 和远程共用。"""
        images = images or []
        self._clear_empty_state()

        # @文件引用：聊天区只显示原文 display_text，完整文件内容只注入发给 AI 的 send_text
        # （避免把整个文件内容也刷在聊天界面上）
        display_text = text
        send_text = self._expand_file_mentions(text)

        # 带图片但当前模型不支持视觉时，不再把整轮任务切给弱视觉模型。
        # 改为先用视觉模型做识别/OCR，再把识别结果作为文本交回当前强模型。
        use_vision_bridge = bool(images) and not agent.current_model_supports_vision()
        original_model_name = agent.MODEL_LIST[agent.current_model_index][0]
        vision_model_name = ""
        if use_vision_bridge:
            vision_idx = agent.get_vision_model_index()
            if vision_idx < 0:
                self._append_html(
                    "\n⚠️ 当前模型不支持图片。请先在「设置 → 图片识别模型」里选一个能看图的模型。\n",
                    "tool_result",
                )
                return
            vision_model_name = agent.MODEL_LIST[vision_idx][0]

        self.is_generating = True
        self._update_btn_state("stop")

        # 显示用户消息
        self._append_html("\n", "spacer")
        self._append_html("你\n", "user_label")
        if images:
            self._insert_images_in_chat(images)
        if display_text:
            self._append_user_text(display_text + "\n\n")
        # 发消息后强制滚到底：看到自己刚发的 + 贴底后 AI 回复会自动跟随
        self._scroll_to_bottom()

        if use_vision_bridge:
            state.stop_flag = False
            threading.Thread(
                target=self._run_vision_bridge_agent,
                args=(send_text, images, vision_model_name, original_model_name),
                daemon=True,
            ).start()
            return

        # 构造消息
        if images:
            # Anthropic / MiMo 官方建议：图片在前、文字在后，模型才能正确关联问题与图片
            content = []
            for path, b64 in images:
                ext = os.path.splitext(path)[1].lower().lstrip(".")
                content.append(_build_image_content_block(ext, b64))
            if send_text:
                content.append({"type": "text", "text": send_text})
            agent.chat_history.append(HumanMessage(content=content))
        else:
            agent.chat_history.append(HumanMessage(content=send_text))

        state.stop_flag = False
        threading.Thread(target=self._run_agent, daemon=True).start()

    def _run_agent(self):
        agent.agent_loop(self)
        self.bridge.finished.emit()

    def _run_vision_bridge_agent(self, text, images, vision_model_name, original_model_name):
        """非视觉模型收到图片时：视觉模型只负责识别，原模型负责最终回答。"""
        try:
            self.show_message(
                f"\n🔎 使用「{vision_model_name}」识别图片，随后交给「{original_model_name}」继续处理\n",
                "tool_result",
            )
            detected_name, description = agent.describe_images_with_vision(text, images)
            if agent.stop_flag:
                return
            if not description:
                description = "图片识别未返回有效内容。"

            preview = description
            if len(preview) > 1200:
                preview = preview[:1200] + "\n... [识别结果较长，已折叠显示；完整内容会交给当前模型]"
            self.show_message(f"✅ 图片识别完成（{detected_name}）\n{preview}\n", "tool_result")

            bridge_text = (
                "[[LINGXI_INTERNAL_VISION_BRIDGE]]\n"
                f"[图片识别结果，由 {detected_name} 提供，供 {original_model_name} 继续处理]\n"
                f"{description}"
            )
            content = []
            for path, b64 in images:
                ext = os.path.splitext(path)[1].lower().lstrip(".")
                content.append(_build_image_content_block(ext, b64))
            if text:
                content.append({"type": "text", "text": text})
            agent.chat_history.append(HumanMessage(content=content))
            agent.chat_history.append(HumanMessage(content=bridge_text))
            agent.agent_loop(self)
        except Exception as e:
            self.show_retry(f"图片识别失败: {str(e)[:100]}")
        finally:
            self.bridge.finished.emit()

    def _on_finished(self):
        # 如果已经被 _force_stop_generation 提前处理过，跳过重复刷新
        if not self.is_generating and not state.stop_flag:
            return
        self.is_generating = False
        self._ai_reply_start = None
        self._update_btn_state("enabled" if self._has_input else "disabled")
        self._refresh_session_list()
        # AI 这一轮可能新建了 checkpoint，刷新撤销按钮状态
        if hasattr(self, "undo_btn"):
            self._style_undo_btn()
        state.remote_session = False

    def _scroll_guard(self):
        """智能滚动：返回一个回调函数，调用时仅当之前用户在底部才滚到新底部。
        用法：
            scroll = self._scroll_guard()
            # ... 插入内容 ...
            scroll()
        """
        sb = self.chat_area.verticalScrollBar()
        was_at_bottom = sb.value() >= sb.maximum() - 30  # 插入前是否贴底
        prev = sb.value()
        def _after():
            # 贴底 → 跟到新底部；不贴底 → 恢复到插入前位置。
            # 后者用来【主动抵消】QTextBrowser 在末尾 insertText 时自动把视口拉到
            # 底的默认行为——那才是"滚上去看历史却被流式追加拽回底部"的真因。
            if was_at_bottom:
                sb.setValue(sb.maximum())
            else:
                sb.setValue(prev)
            if hasattr(self, 'scroll_bottom_btn'):
                self.scroll_bottom_btn.raise_()
        return _after

    def _show_retry(self, error_msg):
        """在聊天区显示错误信息和重试链接"""
        scroll = self._scroll_guard()
        cursor = self.chat_area.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.chat_area.setTextCursor(cursor)

        # 错误信息
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(self._t("warn")))
        warn_font = QFont("Microsoft YaHei")
        warn_font.setPixelSize(14)
        warn_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        warn_font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        fmt.setFont(warn_font)
        self._insert_text_with_icons(cursor, f"\n⚠️ {error_msg}\n", fmt, size=15)

        # 重试链接（QTextBrowser 支持 anchorClicked）
        retry_icon = self._inline_svg_img("refresh_cw_lucide.svg", self._t("retry_link"), 16, "重试")
        cursor.insertHtml(
            f'<a href="action:retry" style="color:{self._t("retry_link")};font-size:16px;'
            f'text-decoration:none;background:{self._t("retry_link_bg")};padding:6px 18px;'
            f'border:1px solid {self._t("retry_link_border")};border-radius:8px;">{retry_icon} 重试</a><br><br>'
        )

        scroll()

    def _on_link_clicked(self, url):
        """处理聊天区内链接点击"""
        s = url.toString()
        if s == "action:retry":
            self._on_retry()
        elif s.startswith("action:show_thinking:"):
            think_id = s.rsplit(":", 1)[-1]
            self._show_thinking_dialog(think_id)
        elif s.startswith("action:show_image:"):
            img_id = s.rsplit(":", 1)[-1]
            self._show_image_dialog(img_id)
        # ---- #5 code block copy ----
        elif s.startswith("action:copy_code:"):
            idx = s.split(":")[-1]
            code = self._code_blocks.get(idx, "")
            if code:
                from PySide6.QtWidgets import QApplication
                QApplication.clipboard().setText(code)
                self._show_toast("Code copied!")
        # ---- #6 message copy ----
        elif s.startswith("action:copy_msg:"):
            idx = s.split(":")[-1]
            text = self._msg_buffers.get(idx, "")
            if text:
                from PySide6.QtWidgets import QApplication
                QApplication.clipboard().setText(text)
                self._show_toast("Message copied!")
        # ---- #6 regenerate ----
        elif s == "action:regenerate":
            self._on_retry()

    def _show_image_dialog(self, img_id):
        """点击聊天区图片：在应用内显示半透明遮罩 + 居中大图"""
        path = getattr(self, "_image_paths", {}).get(img_id)
        if not path or not os.path.exists(path):
            return

        # 已有遮罩则先关闭
        if getattr(self, "_image_overlay", None) is not None:
            self._image_overlay.deleteLater()
            self._image_overlay = None

        from PySide6.QtWidgets import QLabel as _QLabel

        overlay = QWidget(self)
        overlay.setObjectName("imageOverlay")
        overlay.setStyleSheet(
            "#imageOverlay { background-color: rgba(0, 0, 0, 160); }"
        )
        overlay.setGeometry(0, 0, self.width(), self.height())

        # 居中放图片
        label = _QLabel(overlay)
        pixmap = QPixmap(path)
        max_w = int(self.width() * 0.85)
        max_h = int(self.height() * 0.85)
        if pixmap.width() > max_w or pixmap.height() > max_h:
            pixmap = pixmap.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        label.setPixmap(pixmap)
        label.setAlignment(Qt.AlignCenter)
        # 居中定位
        lw, lh = pixmap.width(), pixmap.height()
        label.setGeometry(
            (overlay.width() - lw) // 2,
            (overlay.height() - lh) // 2,
            lw, lh
        )

        # 点击遮罩任意位置关闭
        def _close(event):
            overlay.deleteLater()
            self._image_overlay = None
        overlay.mousePressEvent = _close

        overlay.show()
        overlay.raise_()
        self._image_overlay = overlay


    # ---- #8 Drag & Drop ----
    def _show_drag_overlay(self):
        """Show fullscreen semi-transparent drag overlay"""
        if hasattr(self, '_drag_overlay') and self._drag_overlay is not None:
            return
        overlay = QWidget(self)
        overlay.setObjectName("dragOverlay")
        overlay.setGeometry(0, 0, self.width(), self.height())
        overlay.setStyleSheet(
            f"QWidget#dragOverlay {{"
            f"  background-color: {self._t('drag_bg')};"
            f"  border: {self._t('drag_border_style')} {self._t('drag_border')};"
            f"  border-radius: 18px;"
            f"}}"
        )
        layout = QVBoxLayout(overlay)
        layout.setAlignment(Qt.AlignCenter)
        icon_label = QLabel(overlay)
        icon_label.setPixmap(self._svg_icon("folder_open_lucide.svg", self._t("drag_text")).pixmap(QSize(64, 64)))
        icon_label.setFixedSize(72, 72)
        icon_label.setAlignment(Qt.AlignCenter)
        icon_label.setStyleSheet("background: transparent; border: none; padding: 0;")
        text_label = QLabel("拖拽文件到这里", overlay)
        text_label.setAlignment(Qt.AlignCenter)
        text_label.setStyleSheet(
            f"font-size: 22px; font-weight: bold; color: {self._t('drag_text')}; "
            f"background: transparent; border: none; padding: 0; letter-spacing: 4px;"
        )
        sub_label = QLabel("支持图片和文本文件", overlay)
        sub_label.setAlignment(Qt.AlignCenter)
        sub_label.setStyleSheet(
            f"font-size: 12px; color: {self._t('drag_subtext')}; background: transparent; "
            f"border: none; padding: 0; letter-spacing: 1px;"
        )
        layout.addWidget(icon_label)
        layout.addWidget(text_label)
        layout.addWidget(sub_label)
        overlay.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        overlay.show()
        overlay.raise_()
        self._drag_overlay = overlay

    def _hide_drag_overlay(self):
        """Hide drag overlay"""
        ov = getattr(self, '_drag_overlay', None)
        if ov is not None:
            ov.deleteLater()
            self._drag_overlay = None

    def dragEnterEvent(self, event):
        """Accept drag if it contains files or images, show overlay"""
        if event.mimeData().hasImage() or event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._show_drag_overlay()
        else:
            super().dragEnterEvent(event)

    def dragLeaveEvent(self, event):
        """Hide drag overlay when leaving"""
        self._hide_drag_overlay()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        """Handle dropped files and images"""
        self._hide_drag_overlay()

        if event.mimeData().hasImage():
            img = event.mimeData().imageData()
            if img and not img.isNull():
                self._add_image_from_qimage(img)
                event.acceptProposedAction()
                return

        if event.mimeData().hasUrls():
            handled = False
            for url in event.mimeData().urls():
                path = url.toLocalFile()
                if not path:
                    continue
                lower = path.lower()
                if lower.endswith(('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp')):
                    self._add_pending_image(path)
                    handled = True
                elif lower.endswith(('.txt', '.md', '.py', '.js', '.ts', '.html', '.css',
                                     '.json', '.xml', '.yaml', '.yml', '.toml', '.ini',
                                     '.cfg', '.sh', '.bat', '.ps1', '.c', '.cpp', '.h',
                                     '.java', '.go', '.rs', '.rb', '.php', '.sql', '.csv',
                                     '.log')):
                    try:
                        limit = 200000  # 字符上限（不是字节——中文一个字符 3 字节）
                        with open(path, "r", encoding="utf-8", errors="replace") as f:
                            # 多读 1 个字符探测是否真被截断，避免拿字节数跟字符上限比
                            content = f.read(limit + 1)
                        truncated = len(content) > limit
                        content = content[:limit]
                        fname = os.path.basename(path)
                        if truncated:
                            limit_k = limit // 1000
                            insert_text = (
                                f"[File: {fname}]\n"
                                f"[文件过长，仅插入前 {limit_k}K 字符]\n"
                                f"{content}"
                            )
                        else:
                            insert_text = f"[File: {fname}]\n{content}"
                        cursor = self.entry.textCursor()
                        cursor.movePosition(QTextCursor.End)
                        cursor.insertText(insert_text)
                        handled = True
                    except Exception:
                        pass
            if handled:
                event.acceptProposedAction()
                return
        super().dropEvent(event)


    def resizeEvent(self, event):
        """窗口尺寸变化时，让图片遮罩跟随"""
        super().resizeEvent(event)
        self._refresh_responsive_layout()
        ov = getattr(self, "_image_overlay", None)
        if ov is not None:
            ov.setGeometry(0, 0, self.width(), self.height())
            # 重新居中图片
            for child in ov.findChildren(QLabel):
                pm = child.pixmap()
                if pm and not pm.isNull():
                    child.setGeometry(
                        (ov.width() - pm.width()) // 2,
                        (ov.height() - pm.height()) // 2,
                        pm.width(), pm.height()
                    )

    def closeEvent(self, event):
        # 关窗前先唤醒任何还挂着的命令 / edit diff 确认请求——否则 worker 线程
        # 会卡满 5 分钟才超时，期间 agent 线程整段挂死
        self._release_pending_confirm()
        self._release_pending_edit()

        # main.py 在挂上桌宠+托盘后会把 _hide_on_close 置 True
        if not getattr(self, "_hide_on_close", False):
            super().closeEvent(event)
            return

        # 读已保存的选择（"hide" / "quit"），若有则跳过弹窗
        prefs = _load_ui_prefs()
        saved = prefs.get("close_action")
        if saved == "hide":
            event.ignore()
            self.hide()
            return
        if saved == "quit":
            super().closeEvent(event)
            from PySide6.QtWidgets import QApplication
            QApplication.quit()
            return

        dialog = CloseConfirmDialog(self)
        if dialog.exec() != QDialog.Accepted or dialog.action is None:
            event.ignore()
            return

        action = dialog.action
        if dialog.remember_check.isChecked():
            prefs["close_action"] = action
            _save_ui_prefs(prefs)

        if action == CloseConfirmDialog.ACTION_HIDE:
            event.ignore()
            self.hide()
        else:
            super().closeEvent(event)
            from PySide6.QtWidgets import QApplication
            QApplication.quit()

    def keyPressEvent(self, event):
        """Esc / Ctrl+F / F3 / F12 key handler"""
        if event.key() == Qt.Key_Escape:
            if getattr(self, "_image_overlay", None) is not None:
                self._image_overlay.deleteLater()
                self._image_overlay = None
                return
            if self._search_widget is not None and self._search_widget.isVisible():
                self._close_search()
                return
        # ---- #7 Ctrl+F ----
        if event.key() == Qt.Key_F and event.modifiers() & Qt.ControlModifier:
            self._toggle_search()
            return
        if event.key() == Qt.Key_F3:
            if self._search_widget and self._search_widget.isVisible():
                text = self._search_widget._input.text()
                if event.modifiers() & Qt.ShiftModifier:
                    self._search_prev(text)
                else:
                    self._search_next(text)
                return
        # ---- F12: Debug Inspector ----
        if event.key() == Qt.Key_F12:
            self._toggle_debug_inspector()
            return
        super().keyPressEvent(event)

    def _toggle_debug_inspector(self):
        """F12：唤出 / 关闭 Debug Inspector（非模态，懒构造）。"""
        insp = getattr(self, "_debug_inspector", None)
        if insp is None:
            from .debug_inspector import DebugInspector
            insp = DebugInspector(self)
            self._debug_inspector = insp
        if insp.isVisible():
            insp.hide()
        else:
            insp.show()
            insp.raise_()
            insp.activateWindow()


    # ---- #7 Ctrl+F in-chat search ----


    def _on_retry(self):
        """点击重试：重新执行 agent_loop"""
        if self.is_generating:
            return
        # 移除上次失败的 AI 消息（如果最后一条是 AI 的空消息）
        from langchain_core.messages import AIMessage
        if agent.chat_history and isinstance(agent.chat_history[-1], AIMessage):
            _c = agent.chat_history[-1].content
            # content 可能是 str 或 list（含 thinking blocks）
            if isinstance(_c, list):
                _text = "".join(
                    b.get("text", "") for b in _c
                    if isinstance(b, dict) and b.get("type") == "text"
                )
                if not _text.strip():
                    agent.chat_history.pop()
            elif isinstance(_c, str) and not _c.strip():
                agent.chat_history.pop()

        self.is_generating = True
        self._update_btn_state("stop")
        state.stop_flag = False
        threading.Thread(target=self._run_agent, daemon=True).start()


    def _show_toast(self, text, duration=1500):
        """Brief toast notification that auto-disappears"""
        from PySide6.QtWidgets import QLabel
        from PySide6.QtCore import QTimer
        if hasattr(self, '_toast_label') and self._toast_label is not None:
            try:
                self._toast_label.deleteLater()
            except Exception:
                pass

        toast = QLabel(self)
        # 已映射的 emoji（🧠/⚡ 等）换成内联 SVG；未映射的（🟡🟢🔴⚫ 状态点靠颜色表意）原样保留
        toast.setTextFormat(Qt.RichText)
        toast.setText(self._emoji_to_svg_html(text, self._t("toast_text"), size=13))
        toast.setStyleSheet(
            f"QLabel {{ background: {self._t('toast_bg')}; color: {self._t('toast_text')}; "
            f"padding: 8px 18px; border: 1px solid {self._t('toast_border')}; border-radius: 10px; "
            f"font-size: 11px; letter-spacing: 1px; }}"
        )
        toast.setAlignment(Qt.AlignCenter)
        toast.adjustSize()
        x = (self.width() - toast.width()) // 2
        y = self.height() - 80
        toast.move(x, y)
        toast.show()
        toast.raise_()
        self._toast_label = toast
        QTimer.singleShot(duration, lambda: self._dismiss_toast(toast))

    def _dismiss_toast(self, toast):
        try:
            if toast:
                toast.deleteLater()
        except Exception:
            pass


    def show_message(self, text, tag):
        """线程安全：从 agent 线程发送信号到 UI 线程"""
        # 桌宠思考动画：thinking_indicator 出现时切 think
        if tag == "thinking_indicator":
            pet = getattr(self, "pet", None)
            if pet is not None:
                pet.set_thinking(True)
        self.bridge.append_signal.emit(text, tag)

    def show_retry(self, error_msg):
        """线程安全：显示错误信息和重试按钮"""
        pet = getattr(self, "pet", None)
        if pet is not None:
            pet.set_thinking(False)
        self.bridge.show_retry.emit(error_msg)

    def remove_thinking_indicator(self):
        pet = getattr(self, "pet", None)
        if pet is not None:
            pet.set_thinking(False)
        self.bridge.remove_thinking.emit()

    def update_thinking_indicator(self, text):
        """线程安全：更新等待指示器文本"""
        self.bridge.update_thinking.emit(text)

    def _append_html(self, text, tag):
        if tag not in ("thinking_indicator",) and getattr(self, "_empty_state_visible", False):
            self._clear_empty_state()
        scroll = self._scroll_guard()
        cursor = self.chat_area.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.chat_area.setTextCursor(cursor)

        from PySide6.QtGui import QTextCharFormat, QFont as QF

        def _make_format(color, pixel_size, weight=QF.Weight.Normal, bg=None, family="Microsoft YaHei"):
            fmt = QTextCharFormat()
            font = QF(family)
            font.setPixelSize(pixel_size)
            font.setWeight(weight)
            font.setStyleStrategy(QF.StyleStrategy.PreferAntialias)
            if family == "Microsoft YaHei":
                font.setHintingPreference(QF.HintingPreference.PreferNoHinting)
            fmt.setFont(font)
            fmt.setForeground(QColor(color))
            if bg is not None:
                fmt.setBackground(QColor(bg))
            return fmt

        if tag == "user_label":
            fmt = _make_format(self._t("user_label"), 16, QF.Weight.Bold)
            cursor.insertText(text, fmt)
        elif tag == "ai_label":
            fmt = _make_format(self._t("ai_label"), 16, QF.Weight.Bold)
            cursor.insertText(text, fmt)
        elif tag == "user_msg":
            fmt = _make_format(self._t("user_msg"), 15)
            cursor.insertText(text, fmt)
        elif tag == "ai_msg":
            if self._ai_reply_start is None:
                self._ai_reply_start = cursor.position()
            fmt = _make_format(self._t("ai_msg"), 15)
            cursor.insertText(text, fmt)
        elif tag == "think_header":
            self._think_block_start = cursor.position()
            self._think_block_chars = 0
            self._think_block_text = ""
            fmt = _make_format(self._t("thinking"), 14, QF.Weight.Bold, self._t("thinking_bg"))
            cursor.insertText(text, fmt)
        elif tag == "think_msg":
            fmt = _make_format(self._t("thinking_msg"), 14, bg=self._t("thinking_msg_bg"))
            cursor.insertText(text, fmt)
            self._think_block_chars += len(text)
            self._think_block_text += text
        elif tag == "think_collapse":
            # 思考结束，把整段思考块替换为可点击折叠链接
            if self._think_block_start is not None:
                import uuid as _uuid
                think_id = _uuid.uuid4().hex[:8]
                self._thinking_history[think_id] = self._think_block_text
                cursor.setPosition(self._think_block_start)
                cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
                cursor.removeSelectedText()
                cursor.insertHtml(
                    f'<a href="action:show_thinking:{think_id}" '
                    f'style="color:{self._t("thinking")};text-decoration:none;'
                    f'background:{self._t("thinking_bg")};'
                    f'padding:4px 12px;font-weight:bold;font-size:13px;border-radius:6px;">'
                    f'思考 · {self._think_block_chars} 字 ▶</a>'
                )
                cursor.insertText("\n")
                self._think_block_start = None
                self._think_block_chars = 0
                self._think_block_text = ""
        elif tag == "reply_header":
            fmt = _make_format(self._t("ai_label"), 15, QF.Weight.Bold)
            cursor.insertText(text, fmt)
        elif tag == "thinking_indicator":
            self._thinking_start = cursor.position()
            fmt = _make_format(self._t("thinking"), 14, bg=self._t("thinking_bg"))
            cursor.insertText(text, fmt)
            self._thinking_end = cursor.position()
        elif tag == "tool_tag":
            # emoji 换成内联 SVG（见 _EMOJI_ICON / docs/emoji_inventory.md），其余保持加粗 + tool 配色 + 背景。
            # 首尾换行用纯文本插入（HTML 里换行会被折叠丢失），只有中间主体走 insertHtml。
            lead = len(text) - len(text.lstrip("\n"))
            trail = len(text) - len(text.rstrip("\n"))
            core = text[lead: len(text) - trail]
            tool_fmt = _make_format(self._t("tool"), 14)
            if lead:
                cursor.insertText("\n" * lead, tool_fmt)
            if core:
                color = self._t("tool")
                body = self._emoji_to_svg_html(core, color, size=15)
                cursor.insertHtml(
                    f'<span style="color:{color};font-weight:bold;'
                    f'background-color:{self._t("tool_bg")};">{body}</span>'
                )
            if trail:
                cursor.insertText("\n" * trail, tool_fmt)
        elif tag == "tool_detail":
            fmt = _make_format(self._t("tool"), 14, bg=self._t("tool_bg"), family="Consolas")
            cursor.insertText(text, fmt)
        elif tag == "tool_result":
            fmt = _make_format(self._t("tool_result"), 13, bg=self._t("tool_result_bg"), family="Consolas")
            self._insert_text_with_icons(cursor, text, fmt, size=14)
        elif tag == "spacer":
            cursor.insertText("\n")
        elif tag == "ai_image":
            # text 是图片本地路径，AI 工具生成后通知 UI 显示
            self._insert_image_path(text)
        elif tag == "reset_ai_reply":
            # 工具调用结束后，让下一轮 ai_msg 重置起点，避免最终 markdown 渲染覆盖工具结果和图片
            self._ai_reply_start = None
        else:
            cursor.insertText(text)

        scroll()


    def show_token_usage(self, session_usage, round_usage):
        """线程安全：从 agent 线程通知 UI 更新 token 用量"""
        self.bridge.token_usage.emit(session_usage, round_usage)


    def _update_token_usage(self, session_usage, round_usage):
        """UI 线程：更新底部 token 用量显示"""
        if not hasattr(self, 'token_usage_label'):
            return
        inp = session_usage.get('input', 0)
        out = session_usage.get('output', 0)
        total = session_usage.get('total', 0)
        r_total = round_usage.get('total', 0)

        def _fmt(n):
            if n >= 1000:
                return f"{n/1000:.1f}k"
            return str(n)

        text = f"Token 输入 {_fmt(inp)} · 输出 {_fmt(out)} · 总计 {_fmt(total)}"
        if r_total > 0:
            text += f"  (本轮 {_fmt(r_total)})"
        self.token_usage_label.setText(text)
        self.token_usage_label.setVisible(True)
