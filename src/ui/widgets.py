"""自定义 Qt 控件 + 线程通信桥。

- SignalBridge：worker 线程通过它把渲染请求 emit 到 UI 线程
- DragDropTextBrowser / DragDropTextEdit：把拖拽事件转发给主窗口（避免子控件吞掉）
- HistoryRow：侧栏会话条容器（删除按钮通过布局排在标题右侧）
- CloseConfirmDialog：关闭软件时的"最小化到托盘 / 退出"二选一对话框
"""
import os

from PySide6.QtCore import Qt, Signal, QObject, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QTextBrowser,
    QTextEdit, QVBoxLayout, QWidget,
)

from ._base import BASE_DIR


class SignalBridge(QObject):
    append_signal = Signal(str, str)       # (text, tag)
    remove_thinking = Signal()
    update_thinking = Signal(str)          # 更新等待指示器文
    render_md = Signal(str)                # 渲染 Markdown 替换最后的纯文
    show_retry = Signal(str)               # 显示重试按钮 + 错误信息
    finished = Signal()
    token_usage = Signal(dict, dict)   # (session_usage, round_usage)
    sessions_refresh = Signal()        # 异步标题生成完后刷新侧栏会话列表
    # 让 worker 线程能阻塞式请求 UI 弹确认框：发 (命令文本, 用于回传结果的 dict,
    # threading.Event)。槽运行在 UI 主线程，调完 QMessageBox 后写 dict + 唤醒 Event
    confirm_request = Signal(str, object, object)
    # edit_file 之前弹 diff 预览：发 (path, diff_text, result_dict, event)
    edit_confirm_request = Signal(str, str, object, object)
    remote_submit = Signal(str)        # Telegram 遥控消息注入（跨线程 → 主线程）
    # 手机端点完确认后，让主线程隐藏可能还挂着的 PC 确认卡（仅 UI，result/done 已由远程写好）
    dismiss_confirm = Signal()


class DragDropTextBrowser(QTextBrowser):
    """QTextBrowser: forward file drag/drop to parent window"""

    def dragEnterEvent(self, event):
        if event.mimeData().hasImage() or event.mimeData().hasUrls():
            self.window().dragEnterEvent(event)
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasImage() or event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasImage() or event.mimeData().hasUrls():
            self.window().dropEvent(event)
        else:
            super().dropEvent(event)


class DragDropTextEdit(QTextEdit):
    """QTextEdit: forward file drag/drop to parent window。

    同时强制粘贴为纯文本——否则粘进带样式的富文本（如带红底的字）后，
    光标会继承那段格式，后续打字全是那个样式。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setAcceptRichText(False)

    def insertFromMimeData(self, source):
        # 只取纯文本，丢弃所有富文本格式（颜色 / 背景 / 字体等）
        if source.hasText():
            self.insertPlainText(source.text())
        else:
            super().insertFromMimeData(source)

    def dragEnterEvent(self, event):
        if event.mimeData().hasImage() or event.mimeData().hasUrls():
            self.window().dragEnterEvent(event)
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasImage() or event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasImage() or event.mimeData().hasUrls():
            self.window().dropEvent(event)
        else:
            super().dropEvent(event)


class FileCompleter(QWidget):
    """@文件名补全浮窗。

    在输入框中键入 "@" 后弹出，列出项目根下的文件。
    支持键盘上下移动、Enter/Tab 选中、Esc 关闭。
    """

    item_selected = Signal(str)  # 选中时发射完整相对路径

    def __init__(self, parent=None):
        super().__init__(parent, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        from PySide6.QtWidgets import QVBoxLayout as _VL, QListWidget as _LW
        layout = _VL(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.list_widget = _LW()
        self.list_widget.setFocusPolicy(Qt.NoFocus)
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_widget.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.list_widget.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self.list_widget)

        self._all_items = []   # 全量候选（相对路径列表）
        self._colorized = False

    # ── 样式 ──
    def apply_theme(self, bg, border, text, sel_bg, sel_text, hover_bg, hover_text):
        """按当前主题调色。由 ChatUI 在初始化和切主题时调用。"""
        self._colorized = True
        self.setStyleSheet(
            f"FileCompleter {{ background: {bg}; border: 1px solid {border}; "
            f"border-radius: 8px; }}"
            f"QListWidget {{ background: transparent; border: none; "
            f"outline: none; color: {text}; font-size: 13px; "
            f"padding: 4px 0; }}"
            f"QListWidget::item {{ padding: 6px 14px; border-radius: 4px; }}"
            f"QListWidget::item:selected {{ background: {sel_bg}; color: {sel_text}; }}"
            f"QListWidget::item:hover {{ background: {hover_bg}; color: {hover_text}; }}"
        )

    # ── 显示 / 过滤 ──
    def set_files(self, files: list):
        """设置全量候选列表（相对路径列表）。"""
        self._all_items = files
        self.filter_and_show("")

    def filter_and_show(self, query: str):
        """按 query 过滤候选并刷新列表。query 为空时显示全部。"""
        q = query.lower()
        matches = [f for f in self._all_items if q in f.lower()][:20]
        self.list_widget.clear()
        if not matches:
            self.hide()
            return
        self.list_widget.addItems(matches)
        self.list_widget.setCurrentRow(0)
        self._adjust_size()
        self.show()

    def _adjust_size(self):
        """根据当前项目数动态调整浮窗高度。"""
        count = self.list_widget.count()
        row_h = 30  # 估算行高
        h = min(count * row_h, 300) + 8
        self.setFixedHeight(h)
        self.setFixedWidth(max(320, self.width()))

    # ── 键盘导航 ──
    def navigate_up(self):
        row = self.list_widget.currentRow()
        if row > 0:
            self.list_widget.setCurrentRow(row - 1)

    def navigate_down(self):
        row = self.list_widget.currentRow()
        if row < self.list_widget.count() - 1:
            self.list_widget.setCurrentRow(row + 1)

    def confirm_selection(self):
        """确认当前选中项。"""
        item = self.list_widget.currentItem()
        if item:
            self.item_selected.emit(item.text())
            self.hide()

    # ── 信号槽 ──
    def _on_item_clicked(self, item):
        self.item_selected.emit(item.text())
        self.hide()

    # ── 失焦关闭 ──
    def focusOutEvent(self, event):
        # 延迟关闭，让 click 事件有时间触发
        QTimer.singleShot(150, self._close_if_no_focus)
        super().focusOutEvent(event)

    def _close_if_no_focus(self):
        from PySide6.QtWidgets import QApplication
        focused = QApplication.focusWidget()
        # 如果焦点还在 entry 或 list_widget 上，不关
        if focused == self.list_widget or (self.parent() and
                hasattr(self.parent(), 'entry') and focused == self.parent().entry):
            return
        self.hide()

    # ── 排序支持 ──
    @staticmethod
    def fuzzy_score(query: str, text: str) -> int:
        """简单的子序列模糊匹配得分，用于后续排序（阶段2预置接口）。"""
        qi = 0
        score = 0
        prev_match = False
        for ch in text.lower():
            if qi < len(query) and ch == query[qi]:
                score += 10 if prev_match else 5  # 连续匹配加分
                # 路径分隔符后匹配额外加分（更像文件名匹配）
                if not prev_match and score > 5:
                    score += 2
                prev_match = True
                qi += 1
            else:
                prev_match = False
        return score if qi == len(query) else -1


class HistoryRow(QWidget):
    """Sidebar history row（删除按钮通过布局排在标题右侧，永远可见）。"""

    def __init__(self, parent=None):
        super().__init__(parent)

    def watch_hover(self, widget):
        # 兼容旧调用，无操作
        pass


class CloseConfirmDialog(QDialog):
    ACTION_HIDE = "hide"
    ACTION_QUIT = "quit"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("关闭灵犀")
        self.setModal(True)
        self.setFixedSize(420, 200)
        self.action = None

        icon_path = os.path.join(BASE_DIR, "icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 20, 26, 18)
        root.setSpacing(12)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(8)

        title = QLabel("关闭灵犀？")
        title.setObjectName("closeTitle")
        title.setWordWrap(True)
        text_col.addWidget(title)

        desc = QLabel(
            "最小化后，灵犀立绘与托盘图标会继续保留。"
            "退出软件将完全关闭灵犀。"
        )
        desc.setObjectName("closeDescription")
        desc.setWordWrap(True)
        text_col.addWidget(desc)

        self.remember_check = QCheckBox("记住我的选择，下次不再询问")
        self.remember_check.setObjectName("closeRemember")
        text_col.addWidget(self.remember_check)
        root.addLayout(text_col)
        root.addStretch()

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(10)
        buttons.addStretch()

        hide_btn = QPushButton("最小化到托盘")
        hide_btn.setObjectName("closePrimaryButton")
        hide_btn.setDefault(True)
        hide_btn.clicked.connect(self._choose_hide)

        quit_btn = QPushButton("退出软件")
        quit_btn.setObjectName("closeSecondaryButton")
        quit_btn.clicked.connect(self._choose_quit)

        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("closeSecondaryButton")
        cancel_btn.clicked.connect(self.reject)

        for btn in (hide_btn, quit_btn, cancel_btn):
            btn.setCursor(Qt.PointingHandCursor)
            btn.setMinimumSize(92, 32)
            buttons.addWidget(btn)

        root.addLayout(buttons)
        self._apply_style()

    def _choose_hide(self):
        self.action = self.ACTION_HIDE
        self.accept()

    def _choose_quit(self):
        self.action = self.ACTION_QUIT
        self.accept()

    def _apply_style(self):
        is_dark = bool(self.parent() and getattr(self.parent(), "theme", "light") == "dark")
        bg = "#11151b" if is_dark else "#f7f9fc"
        fg = "#eef2f7" if is_dark else "#111827"
        muted = "#b9c2cf" if is_dark else "#262b33"
        border = "#2b3440" if is_dark else "#d8e0ec"
        button_bg = "#171c23" if is_dark else "#ffffff"
        button_hover = "#202733" if is_dark else "#f4f7fb"
        accent = "#1687d9"
        accent_hover = "#0d74c2"
        check_icon = os.path.join(BASE_DIR, "icons", "check_white.svg").replace("\\", "/")

        self.setStyleSheet(
            f"CloseConfirmDialog {{ background: {bg}; color: {fg}; }}\n"
            f"#closeTitle {{ color: {fg}; font-size: 16px; font-weight: 600;"
            f" line-height: 1.35; }}\n"
            f"#closeDescription {{ color: {muted}; font-size: 13px;"
            f" line-height: 1.35; }}\n"
            f"#closeRemember {{ color: {fg}; font-size: 13px; spacing: 8px; }}\n"
            f"#closeRemember::indicator {{ width: 16px; height: 16px;"
            f" border-radius: 4px; border: 1px solid {border}; background: {button_bg}; }}\n"
            f"#closeRemember::indicator:hover {{ border-color: {accent}; }}\n"
            f"#closeRemember::indicator:checked {{ background: {accent}; border-color: {accent};"
            f" image: url(\"{check_icon}\"); }}\n"
            f"#closeRemember::indicator:checked:hover {{ background: {accent_hover};"
            f" border-color: {accent_hover}; }}\n"
            f"#closePrimaryButton, #closeSecondaryButton {{ border-radius: 6px;"
            f" padding: 5px 10px; font-size: 13px; background: {button_bg};"
            f" color: {fg}; border: 1px solid {border}; }}\n"
            f"#closePrimaryButton {{ color: {accent}; border-color: {accent}; }}\n"
            f"#closePrimaryButton:hover {{ background: rgba(22, 135, 217, 0.10);"
            f" border-color: {accent_hover}; color: {accent_hover}; }}\n"
            f"#closeSecondaryButton:hover {{ background: {button_hover};"
            f" border-color: {accent}; }}\n"
        )
