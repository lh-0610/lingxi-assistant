"""侧栏 + 会话列表 + 项目切换（mixin for ChatUI）。

抽出来的两块紧密耦合逻辑：

- 左侧侧栏构造（品牌头、"+ 新对话"、按项目分组的历史会话列表、底部齿轮按钮）
- 项目管理（添加 / 移除 / 切换 / 当前会话归属判定）

会话列表按项目分组渲染：注册项目 → 游离项目 → "历史会话"（无项目）。
切项目时同步 state.current_project + system prompt（让角色卡 / .lingxirules
跟着重新加载）。

依赖宿主：self._t / self._svg_icon / self._style_settings_btn /
self.chat_area / self.is_generating / self._reset_render_state /
self._redraw_chat / self._refresh_project_indicator / self._show_empty_state /
self._open_settings_menu
"""
import os

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QMenu, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)
from langchain_core.messages import SystemMessage

from .. import agent
from .. import state
from ._base import BASE_DIR
from .widgets import HistoryRow


class SidebarMixin:
    """左侧栏所有 UI + 会话/项目状态管理。"""

    # ── 构造 ──

    def _build_sidebar(self):
        self.sidebar = QWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(248)

        layout = QVBoxLayout(self.sidebar)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(8)

        brand = QWidget()
        brand.setObjectName("sidebarBrand")
        brand_layout = QHBoxLayout(brand)
        brand_layout.setContentsMargins(6, 0, 6, 4)
        brand_layout.setSpacing(10)

        icon_label = QLabel()
        icon_label.setFixedSize(30, 30)
        icon_path = os.path.join(BASE_DIR, "icon.ico")
        if os.path.exists(icon_path):
            # 先问 QIcon 要一张大尺寸的源位图（.ico 里通常有 256×256），再用
            # KeepAspectRatio + SmoothTransformation 缩到目标物理像素。直接调
            # QIcon.pixmap(target) 会按"最接近的内嵌位图"返回，可能拿到比例不
            # 对的那张，导致在 QLabel 里只显示一部分。
            dpr = self.devicePixelRatioF() if hasattr(self, "devicePixelRatioF") else 1.0
            target = 30
            phys = int(round(target * dpr))
            src = QIcon(icon_path).pixmap(QSize(256, 256))
            pix = src.scaled(phys, phys, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            pix.setDevicePixelRatio(dpr)
            icon_label.setPixmap(pix)
            icon_label.setAlignment(Qt.AlignCenter)
        brand_text = QWidget()
        brand_text_layout = QVBoxLayout(brand_text)
        brand_text_layout.setContentsMargins(0, 0, 0, 0)
        brand_text_layout.setSpacing(0)
        brand_title = QLabel("灵犀")
        brand_title.setObjectName("sidebarBrandTitle")
        brand_sub = QLabel("local & cloud")
        brand_sub.setObjectName("sidebarBrandSub")
        brand_text_layout.addWidget(brand_title)
        brand_text_layout.addWidget(brand_sub)
        brand_layout.addWidget(icon_label)
        brand_layout.addWidget(brand_text, 1)
        layout.addWidget(brand)

        new_btn = QPushButton("+ 新对话")
        new_btn.setObjectName("newChatBtn")
        new_btn.setCursor(Qt.PointingHandCursor)
        new_btn.clicked.connect(self._new_chat)
        layout.addWidget(new_btn)

        label = QLabel("历史记录")
        label.setObjectName("historyLabel")
        layout.addWidget(label)

        # 历史列表（可滚动）
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.history_scroll = scroll

        self.history_widget = QWidget()
        self.history_layout = QVBoxLayout(self.history_widget)
        self.history_layout.setContentsMargins(0, 6, 0, 0)
        self.history_layout.setSpacing(3)
        self.history_layout.addStretch()

        scroll.setWidget(self.history_widget)
        layout.addWidget(scroll, 1)

        # 底部工具栏：齿轮按钮放左下角，右侧预留用户 / 角色区
        footer = QWidget()
        footer.setObjectName("sidebarFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(6, 4, 6, 4)
        footer_layout.setSpacing(8)

        self.settings_btn = QPushButton()
        self.settings_btn.setObjectName("sidebarSettingsBtn")
        self.settings_btn.setFixedSize(34, 34)
        self.settings_btn.setCursor(Qt.PointingHandCursor)
        self.settings_btn.setToolTip("设置")
        self.settings_btn.clicked.connect(self._open_settings_menu)
        self._style_settings_btn()
        self.settings_btn.installEventFilter(self)
        footer_layout.addWidget(self.settings_btn)
        footer_layout.addStretch()  # 右侧留白，后续可加用户 / 角色信息

        layout.addWidget(footer)
        self._style_sidebar_scroll()

    def _style_sidebar_scroll(self):
        self.history_scroll.setStyleSheet(
            f"QScrollArea {{ background: {self._t('sidebar_bg')}; border: none; }}"
            f"QScrollBar:vertical {{ width: 5px; background: transparent; }}"
            f"QScrollBar::handle:vertical {{ background: {self._t('scrollbar_handle')}; border-radius: 2px; min-height: 32px; }}"
            f"QScrollBar::handle:vertical:hover {{ background: {self._t('scrollbar_handle_hover')}; }}"
            f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}"
        )
        self.history_widget.setStyleSheet(f"background: {self._t('sidebar_bg')};")

    # ── 会话列表 ──

    def _refresh_session_list(self):
        """按项目分组渲染：项目在上、无项目在下；每组的会话列表显示在组下方。"""
        # 清空
        while self.history_layout.count() > 1:
            item = self.history_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        from .. import projects as _projects
        all_sessions = agent.list_sessions("__all__")

        # 按 project 分组
        grouped = {}  # path -> list[session]
        for s in all_sessions:
            grouped.setdefault(s.get("project"), []).append(s)

        # 项目列表（用户主动添加的，保持顺序）；不在列表里但有 session 的项目作为"游离项目"附加
        registered = _projects.list_projects()
        registered_paths = [p["path"] for p in registered]
        orphan_paths = [k for k in grouped.keys() if k and k not in registered_paths]

        active_project = _projects.get_current()

        # 渲染顺序：注册项目 → 游离项目 → 无项目
        for p in registered:
            self._render_project_group(p["path"], p["name"], grouped.get(p["path"], []),
                                        is_active=(active_project == p["path"]))
        for path in orphan_paths:
            name = os.path.basename(path) or path
            self._render_project_group(path, name, grouped[path],
                                        is_active=(active_project == path))
        # 无项目放最下面
        no_proj = grouped.get(None, [])
        self._render_project_group(None, "历史会话", no_proj,
                                    is_active=(active_project is None))

        # 刷新样式
        self.history_widget.setStyleSheet(self.history_widget.styleSheet())

    def _render_project_group(self, project_path, project_name, sessions, is_active):
        """渲染一个项目分组：标题 + 该项目下的会话列表。"""
        title_btn = QPushButton(project_name)
        title_btn.setObjectName("projectHeaderActive" if is_active else "projectHeader")
        icon_file = "folder_lucide.svg" if project_path else "circle_lucide.svg"
        icon_color = self._t("new_chat_hover_text") if is_active else self._t("history_label")
        title_btn.setIcon(self._svg_icon(icon_file, icon_color))
        title_btn.setIconSize(QSize(15, 15))
        title_btn.setCursor(Qt.PointingHandCursor)
        title_btn.setToolTip(
            (project_path or "无项目（全局）") + "\n（点击切换到此项目，新对话会归属此项目）"
        )

        def _on_header_click(checked=False, p=project_path):
            self._switch_project(p)
        title_btn.clicked.connect(_on_header_click)

        if project_path:
            # 项目标题右键菜单（移除项目）
            title_btn.setContextMenuPolicy(Qt.CustomContextMenu)
            def _on_right(pos, p=project_path):
                self._show_project_header_menu(title_btn, p)
            title_btn.customContextMenuRequested.connect(_on_right)

        self.history_layout.insertWidget(self.history_layout.count() - 1, title_btn)

        # 会话列表
        for s in sessions[:30]:
            sid = s["id"]
            title = s["title"]
            is_current = (sid == agent.current_session_id)

            row = HistoryRow()
            row.setStyleSheet("background: transparent;")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(14, 0, 2, 0)   # 左缩进让会话视觉上"嵌"在项目下
            row_layout.setSpacing(4)

            display_title = title if len(title) <= 12 else title[:12] + "..."
            btn = QPushButton(display_title)
            btn.setProperty("class", "historyItemActive" if is_current else "historyItem")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(title)
            # 注意：不要在这里 setStyleSheet。给 btn 设自己的 stylesheet 会切断
            # app 级 QToolTip 规则继承，tooltip 会变成系统默认黑底。
            # text-align / padding 已经写在 theme.py 的 historyItem 类选择器里。
            btn.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            btn.setMinimumWidth(0)
            btn.clicked.connect(lambda checked=False, s=sid: self._load_session(s))

            del_btn = QPushButton("×")
            del_btn.setObjectName("historyDeleteBtn")
            del_btn.setFixedSize(22, 22)
            del_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            del_btn.setCursor(Qt.PointingHandCursor)
            del_btn.setStyleSheet(
                f"QPushButton#historyDeleteBtn {{ background: transparent; border: none; "
                f"color: {self._t('del_btn')}; font-size: 16px; font-weight: bold; "
                f"border-radius: 12px; padding: 0; }}"
                f"QPushButton#historyDeleteBtn:hover {{ color: {self._t('del_btn_hover')}; "
                f"background: {self._t('del_btn_hover_bg')}; }}"
            )
            del_btn.clicked.connect(lambda checked=False, s=sid: self._delete_session(s))

            row_layout.addWidget(btn, 1)
            row_layout.addWidget(del_btn, 0, Qt.AlignVCenter)
            self.history_layout.insertWidget(self.history_layout.count() - 1, row)

    def _show_project_header_menu(self, anchor_widget, project_path):
        """项目标题右键菜单：移除项目（仅注册项目可移除）。"""
        from .. import projects as _projects
        if project_path not in [p["path"] for p in _projects.list_projects()]:
            return
        menu = QMenu(self)
        a_remove = QAction("从列表移除此项目", menu)
        a_remove.setIcon(self._svg_icon("trash_lucide.svg", self._t("menu_text")))

        def _do_remove():
            reply = QMessageBox.question(
                self, "移除项目",
                f"从列表移除「{_projects.get_current_name() if _projects.get_current() == project_path else os.path.basename(project_path)}」？\n\n"
                "（不会删除磁盘上的项目文件；该项目下的历史会话会一并归到「无项目（全局）」。）",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            # 先把这个项目下的所有会话改成"无项目"，再从注册表移除项目
            agent.move_sessions_to_no_project(project_path)
            _projects.remove_project(project_path)
            # 如果删除的是当前激活项目，切到无项目
            if _projects.get_current() is None:
                self._switch_project(None)
            else:
                self._refresh_session_list()
        a_remove.triggered.connect(_do_remove)
        menu.addAction(a_remove)
        menu.exec(anchor_widget.mapToGlobal(anchor_widget.rect().bottomLeft()))

    def _delete_session(self, session_id):
        if agent.current_session_id == session_id:
            self._force_stop_generation()
        agent.delete_session(session_id)
        if agent.current_session_id == session_id:
            from ..roles import get_system_prompt
            agent.chat_history.clear()
            # 用 get_system_prompt() 而不是裸 SYSTEM_PROMPT，保留角色卡 / 项目上下文 / .lingxirules
            agent.chat_history.append(SystemMessage(content=get_system_prompt()))
            state.current_session_id = None
            state.current_session_title = None
            self.chat_area.clear()
        self._refresh_session_list()

    def _load_session(self, session_id):
        self._force_stop_generation()
        agent.save_session()          # 先保存当前会话，再加载目标
        if agent.load_session(session_id):
            # 如果加载的会话属于另一个项目，自动跟随切到那个项目（同步 current_project + system prompt）
            from .. import projects as _projects
            session_project = self._get_session_project(session_id)
            project_changed = session_project != _projects.get_current()
            if project_changed:
                _projects.set_current(session_project)
                state.current_project = session_project
                from ..roles import get_system_prompt
                if state.chat_history and isinstance(state.chat_history[0], SystemMessage):
                    state.chat_history[0] = SystemMessage(content=get_system_prompt())
            self._reset_render_state()
            self._redraw_chat()
            self._refresh_session_list()
            # 工作目录可能已跟着会话切走，刷新底部项目指示条（之前漏了这步，
            # 导致点 A 项目的会话、底部还显示上一个项目的路径）
            if project_changed:
                self._refresh_project_indicator()

    def _get_session_project(self, session_id):
        """从 index 取指定 session 的 project 字段（不在 index 中返回 None）。"""
        for s in agent.list_sessions("__all__"):
            if s["id"] == session_id:
                return s.get("project")
        return None

    def _new_chat(self):
        self._force_stop_generation()
        agent.reset_history()
        self.chat_area.clear()
        self._reset_render_state()
        self._refresh_session_list()
        self._show_empty_state()

    # ── 项目切换器 ──

    def _show_project_menu(self):
        from .. import projects as _projects
        menu = QMenu(self)

        current = _projects.get_current()
        all_projects = _projects.list_projects()

        # "无项目（全局）" 永远在最上
        a_none = QAction("无项目（全局）", menu)
        a_none.setIcon(self._svg_icon("circle_lucide.svg", self._t("menu_text")))
        a_none.setCheckable(True)
        a_none.setChecked(current is None)
        a_none.triggered.connect(lambda: self._switch_project(None))
        menu.addAction(a_none)

        if all_projects:
            menu.addSeparator()
            for p in all_projects:
                a = QAction(p["name"], menu)
                a.setIcon(self._svg_icon("folder_lucide.svg", self._t("menu_text")))
                a.setCheckable(True)
                a.setChecked(current == p["path"])
                a.setToolTip(p["path"])
                a.triggered.connect(lambda checked=False, path=p["path"]: self._switch_project(path))
                menu.addAction(a)

        menu.addSeparator()

        a_add = QAction("添加项目...", menu)
        a_add.setIcon(self._svg_icon("plus_lucide.svg", self._t("menu_text")))
        a_add.triggered.connect(self._add_project)
        menu.addAction(a_add)

        if current is not None:
            a_remove = QAction("从列表移除当前项目", menu)
            a_remove.setIcon(self._svg_icon("trash_lucide.svg", self._t("menu_text")))
            a_remove.triggered.connect(self._remove_current_project)
            menu.addAction(a_remove)

        menu.exec(self.project_btn.mapToGlobal(self.project_btn.rect().bottomLeft()))

    def _switch_project(self, path):
        self._force_stop_generation()
        from .. import projects as _projects
        from ..roles import get_system_prompt

        # 1. 先保存当前会话（用「旧」project tag）
        #    必须早于 set_current；否则 save_session 会用新 tag 把旧会话覆盖
        agent.save_session()

        # 2. 切换项目
        _projects.set_current(path)
        state.current_project = path

        # 3. 手动清空（不调 agent.reset_history，因为它内部还会再次 save_session）
        state.chat_history.clear()
        state.chat_history.append(SystemMessage(content=get_system_prompt()))
        state.current_session_id = None
        state.current_session_title = None
        state.session_token_usage = {"input": 0, "output": 0, "total": 0}

        # 4. UI
        self.chat_area.clear()
        self._reset_render_state()
        self._refresh_session_list()
        self._refresh_project_indicator()
        self._show_empty_state()

    def _add_project(self):
        from .. import projects as _projects
        path = QFileDialog.getExistingDirectory(self, "选择项目根目录", "")
        if not path:
            return
        if _projects.add_project(path):
            # 添加完成后自动切过去
            normalized = os.path.normpath(path).replace("\\", "/")
            self._switch_project(normalized)
        else:
            QMessageBox.warning(self, "添加失败", "该项目已存在或路径无效。")

    def _remove_current_project(self):
        from .. import projects as _projects
        current = _projects.get_current()
        if not current:
            return
        reply = QMessageBox.question(
            self, "移除项目",
            f"从列表移除项目「{_projects.get_current_name()}」？\n\n"
            "（不会删除磁盘上的项目文件；该项目下的历史会话会一并归到「无项目（全局）」。）",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        agent.move_sessions_to_no_project(current)
        _projects.remove_project(current)
        self._switch_project(None)
