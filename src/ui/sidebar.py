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
        from .. import session as _session
        for s in sessions[:30]:
            sid = s["id"]
            title = s["title"]
            is_current = (sid == agent.current_session_id)
            # 运行态：生成中 → loading 转圈；后台完成、尚未查看（needs_redraw）→ 绿点绿字
            _so = _session.get(sid)
            is_gen = bool(_so and _so.is_generating)
            is_done_unseen = bool(_so and not is_gen and getattr(_so, "needs_redraw", False))

            row = HistoryRow()
            row.setStyleSheet("background: transparent;")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(14, 0, 2, 0)   # 左缩进让会话视觉上"嵌"在项目下
            row_layout.setSpacing(4)

            display_title = title if len(title) <= 12 else title[:12] + "..."
            if is_done_unseen:
                display_title = "● " + display_title   # 绿点（颜色由 historyItemDone class 给）
            btn = QPushButton(display_title)
            if is_current:
                _cls = "historyItemActive"
            elif is_done_unseen:
                _cls = "historyItemDone"
            else:
                _cls = "historyItem"
            btn.setProperty("class", _cls)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(title)
            # 注意：不要在这里 setStyleSheet。给 btn 设自己的 stylesheet 会切断
            # app 级 QToolTip 规则继承，tooltip 会变成系统默认黑底。
            # text-align / padding 已经写在 theme.py 的 historyItem 类选择器里。
            btn.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            btn.setMinimumWidth(0)
            btn.clicked.connect(lambda checked=False, s=sid: self._load_session(s))

            row_layout.addWidget(btn, 1)
            if is_gen:
                # 生成中：删除位置改放 loading 转圈（不显示删除，避免误删正在跑的会话）
                row_layout.addWidget(self._make_loading_spinner(), 0, Qt.AlignVCenter)
            else:
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
                row_layout.addWidget(del_btn, 0, Qt.AlignVCenter)
            self.history_layout.insertWidget(self.history_layout.count() - 1, row)

    def _make_loading_spinner(self):
        """侧栏会话行"生成中"指示：旋转的缺口圆环（widgets.LoadingSpinner）。"""
        from .widgets import LoadingSpinner
        spin = LoadingSpinner(size=16, color="#3b82f6")
        spin.setToolTip("生成中…")
        return spin

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
        from .. import session as _session
        _prev = _session.get_active()  # 切换前的会话，新建会话继承它的 model/mode
        # 存当前 active（不打断正在后台跑的会话；save 内部会把它 re-key 进注册表）
        agent.save_session()
        # 命中注册表 → 该会话已在内存（可能正在后台跑），直接切 active，绝不重读盘覆盖它
        target = _session.get(session_id)
        if target is None:
            # 未打开：新建 Session 读盘填充并注册（key = session_id）
            target = _session.Session()
            # 历史会话不持久化 model → 继承当前会话的 model/mode 作起点（之后随该会话独立）
            target.current_model_index = _prev.current_model_index
            target.agent_mode = _prev.agent_mode
            target.reasoning_enabled = _prev.reasoning_enabled
            if not agent.load_session(session_id, session=target):
                return
            _session.register(target)
        _session.set_active(target)
        target.needs_redraw = False  # 切过去查看了 → 清"完成未读"标记（侧栏绿点消失）
        self._sync_header_from_session()  # 顶栏 model/Plan-Act/思考 同步到该会话
        self._refresh_token_label_from_session()  # 底部 token 刷成该会话的累计用量

        # 加载的会话属于另一个项目 → 跟随切项目（同步 current_project + system prompt）
        from .. import projects as _projects
        session_project = self._get_session_project(session_id)
        project_changed = session_project != _projects.get_current()
        if project_changed:
            _projects.set_current(session_project)
            state.current_project = session_project
            state.shell_cwd = None  # 切项目时 cd 上下文回新项目根
            from ..roles import get_system_prompt
            if state.chat_history and isinstance(state.chat_history[0], SystemMessage):
                state.chat_history[0] = SystemMessage(content=get_system_prompt())

        # 重绘目标会话（state.chat_history 已代理到新 active=target）
        self._reset_render_state()
        self._redraw_chat()
        self._scroll_to_bottom()
        self._refresh_session_list()
        if project_changed:
            self._refresh_project_indicator()
        # 同步前台按钮态：目标会话正在后台跑 → 停止态；否则按输入框恢复
        if target.is_generating:
            self._update_btn_state("stop")
        else:
            self._update_btn_state("enabled" if self._has_input else "disabled")

    def _get_session_project(self, session_id):
        """从 index 取指定 session 的 project 字段（不在 index 中返回 None）。"""
        for s in agent.list_sessions("__all__"):
            if s["id"] == session_id:
                return s.get("project")
        return None

    def _new_chat(self):
        from .. import session as _session
        from ..roles import get_system_prompt
        _prev = _session.get_active()  # 新会话继承当前会话的 model/mode
        # 存当前 active（不打断正在后台跑的会话），再新建一个空会话切过去；
        # 旧会话留在注册表，可从侧栏切回（若在跑则继续后台跑）。
        agent.save_session()
        new_sess = _session.Session()
        new_sess.chat_history.append(SystemMessage(content=get_system_prompt()))
        new_sess.current_model_index = _prev.current_model_index  # 继承 model/mode/思考
        new_sess.agent_mode = _prev.agent_mode
        new_sess.reasoning_enabled = _prev.reasoning_enabled
        _session.register(new_sess)   # 临时 key（无 id，存盘后由 save 的 re-key 换成 id）
        _session.set_active(new_sess)
        self.chat_area.clear()
        self._reset_render_state()
        self._refresh_session_list()
        self._show_empty_state()
        self._update_btn_state("enabled" if self._has_input else "disabled")
        self._sync_header_from_session()
        self._refresh_token_label_from_session()

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
        from .. import projects as _projects
        from ..roles import get_system_prompt
        from .. import session as _session

        # 1. 先存当前会话（用它自己锚定的 project；set_current 不会影响它的 tag）
        agent.save_session()

        # 2. 切换项目（全局当前项目 + tools 项目根）
        _projects.set_current(path)
        state.current_project = path
        state.shell_cwd = None  # 切项目时 cd 上下文回新项目根

        # 3. 新建空会话切过去（它的 project 首次 save 时锚定为新项目）。旧会话留注册表，
        #    若正在后台跑则继续——不再 _force_stop_generation、也不清空它的 chat_history
        #    （那会和正在跑的 worker 抢同一个 list，正是"无项目对话被归到新项目"的来源）。
        _prev = _session.get_active()  # 继承当前会话的 model/mode（切项目不改这些）
        new_sess = _session.Session()
        new_sess.chat_history.append(SystemMessage(content=get_system_prompt()))
        new_sess.current_model_index = _prev.current_model_index
        new_sess.agent_mode = _prev.agent_mode
        new_sess.reasoning_enabled = _prev.reasoning_enabled
        _session.register(new_sess)
        _session.set_active(new_sess)

        # 4. UI
        self.chat_area.clear()
        self._reset_render_state()
        self._refresh_session_list()
        self._refresh_project_indicator()
        self._show_empty_state()
        self._update_btn_state("enabled" if self._has_input else "disabled")
        self._sync_header_from_session()
        self._refresh_token_label_from_session()

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
