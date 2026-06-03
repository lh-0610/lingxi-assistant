"""顶栏构造 + 按钮样式（mixin for ChatUI）。

抽出来的整块顶栏 + 散落各处的按钮样式代码：

- 顶栏构造：模型选择 / Plan-Act 切换 / 撤销 / 思考 / 角色卡 / 主题切换
- 顶栏响应式：窗口窄到一定宽度时按钮压缩成"图标 + 短词"或纯图标
- 按钮样式：所有 `_style_*_btn` 都在这里（含输入区的 img/mic/tts 按钮）
- 角色卡 UI：加载 / 清除 / 状态恢复

依赖宿主：self._t / self._svg_icon / self.theme / self._toggle_sidebar /
self._toggle_theme / self._show_toast / self._append_html /
self._refresh_session_list / self._refresh_header_compactness
"""
import os

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout, QLabel, QMenu, QMessageBox,
    QPushButton, QWidget,
)

from .. import agent
from .. import state
from ._base import BASE_DIR
from .helpers import _make_upload_icon


class HeaderMixin:
    """顶栏 + 全部按钮样式 + 角色卡 UI。"""

    # ── 顶栏构造 ──

    def _build_header(self, parent_layout):
        header = QWidget()
        header.setObjectName("header")
        header.setFixedHeight(56)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(16, 0, 18, 0)
        layout.setSpacing(6)  # 缩紧按钮间距，留点喘息空间给窄窗口

        toggle_btn = QPushButton("☰")
        toggle_btn.setObjectName("toggleBtn")
        toggle_btn.setCursor(Qt.PointingHandCursor)
        toggle_btn.clicked.connect(self._toggle_sidebar)
        layout.addWidget(toggle_btn)

        # 品牌字符 — 灵犀 (KaiTi 笔意，仅夜间主题显示)
        self.header_brand = QLabel("灵犀")
        self.header_brand.setObjectName("headerBrand")
        layout.addWidget(self.header_brand)
        self.header_brand_dot = QLabel("·")
        self.header_brand_dot.setObjectName("headerBrandDot")
        layout.addWidget(self.header_brand_dot)
        brand_visible = self._t("brand_visible") == "true"
        self.header_brand.setVisible(brand_visible)
        self.header_brand_dot.setVisible(brand_visible)

        # 模型选择下拉框
        self.model_combo = QComboBox()
        self.model_combo.setCursor(Qt.PointingHandCursor)
        for name, _, _, _ in agent.MODEL_LIST:
            self.model_combo.addItem(name)
        # 跟启动时解析的默认模型（agent 里按 default_model_id 设的 current_model_index）
        # 同步，而不是写死 0（0 是 Claude Code）。在 connect 之前设，不触发回调。
        self.model_combo.setCurrentIndex(agent.current_model_index)
        self._style_model_combo()
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        layout.addWidget(self.model_combo)

        layout.addStretch()

        # 撤销按钮：把 AI 上次对文件的改动用 git stash 复原。无 checkpoint 时按钮禁用
        self.undo_btn = QPushButton("↶ 撤销")
        self.undo_btn.setCursor(Qt.PointingHandCursor)
        self.undo_btn.setToolTip("撤销 AI 最近一次对文件的修改（git stash 恢复）\n仅 git 项目可用")
        self.undo_btn.clicked.connect(self._on_undo_click)
        self._style_undo_btn()
        layout.addWidget(self.undo_btn)

        # Plan / Act 模式切换按钮（Act 默认，Plan 时 AI 只调研不动手）
        self.mode_btn = QPushButton("Act")
        self.mode_btn.setCursor(Qt.PointingHandCursor)
        self.mode_btn.setCheckable(True)
        self.mode_btn.setChecked(False)  # False = Act, True = Plan
        self._style_mode_btn()
        self.mode_btn.clicked.connect(self._toggle_agent_mode)
        layout.addWidget(self.mode_btn)

        # 思考模式开关
        self.think_btn = QPushButton("思考")
        self.think_btn.setCursor(Qt.PointingHandCursor)
        self.think_btn.setCheckable(True)
        self.think_btn.setChecked(True)
        self._style_think_btn()
        self.think_btn.toggled.connect(lambda _: self._style_think_btn())
        self.think_btn.clicked.connect(self._toggle_thinking)
        layout.addWidget(self.think_btn)

        # 角色卡按钮
        self.role_btn = QPushButton("角色卡")
        self.role_btn.setCursor(Qt.PointingHandCursor)
        self._style_role_btn(active=False)
        self.role_btn.clicked.connect(self._load_role_card)
        layout.addWidget(self.role_btn)

        # 主题切换按钮
        self.theme_btn = QPushButton("☀" if self.theme == "dark" else "☾")
        self.theme_btn.setObjectName("themeBtn")
        self.theme_btn.setCursor(Qt.PointingHandCursor)
        self.theme_btn.setToolTip("切到白天模式" if self.theme == "dark" else "切到夜间模式")
        self.theme_btn.clicked.connect(self._toggle_theme)
        layout.addWidget(self.theme_btn)

        parent_layout.addWidget(header)

    # ── 顶栏响应式 ──

    def _refresh_header_compactness(self):
        """窗口窄到一定宽度时，把顶栏按钮压成"图标 + 短文字" / "纯图标"两档，避免互相重叠。

        阈值：
          - >= 1100 px：正常模式，全文字
          - 900 ~ 1100：紧凑模式，关键按钮（角色卡 / 思考 / Act / 撤销）只显示图标 + 短词
          - < 900     ：超紧凑，纯图标
        """
        w = self.width()
        if w >= 1100:
            level = 0   # 正常
        elif w >= 900:
            level = 1   # 紧凑
        else:
            level = 2   # 超紧凑

        # think_btn
        if hasattr(self, "think_btn"):
            self.think_btn.setText("" if level == 2 else "思考")
            self.think_btn.setToolTip("思考模式：让模型显式输出 reasoning 过程")
        # mode_btn（Plan/Act）—— 紧凑时仍保留单字方便认出当前模式
        if hasattr(self, "mode_btn"):
            txt = "Plan" if self.mode_btn.isChecked() else "Act"
            self.mode_btn.setText("" if level == 2 else txt)
        # undo_btn
        if hasattr(self, "undo_btn"):
            if level == 0:
                self.undo_btn.setText("↶ 撤销")
            elif level == 1:
                self.undo_btn.setText("↶")
            else:
                self.undo_btn.setText("↶")
        # role_btn 保留角色名（信息密度高，比按钮文字本身重要）
        if hasattr(self, "role_btn"):
            # 紧凑模式下截短到 4 个字
            full = agent.get_current_role_name() or "角色卡"
            if level >= 1 and len(full) > 4:
                self.role_btn.setText(full[:4])
                self.role_btn.setToolTip(f"当前角色：{full}")
            else:
                self.role_btn.setText(full)
                self.role_btn.setToolTip("")
        # model_combo 紧凑时让最小宽度松一点
        if hasattr(self, "model_combo"):
            min_w = 210 if level == 0 else (160 if level == 1 else 130)
            # 通过 stylesheet 改 min-width，需要重 polish
            ss = self.model_combo.styleSheet()
            import re as _re
            ss = _re.sub(r"min-width:\s*\d+px;", f"min-width: {min_w}px;", ss)
            self.model_combo.setStyleSheet(ss)

    # ── 顶栏按钮交互 ──

    def _on_model_changed(self, index):
        from .. import session as _session
        if _session.get_active().is_generating:
            self._force_stop_generation()
        agent.switch_model(index)
        # 根据模型是否支持思考，更新开关状态
        _, _, _, supports_think = agent.MODEL_LIST[index]
        self.think_btn.setEnabled(supports_think)
        if not supports_think:
            self.think_btn.setChecked(False)
            agent.set_reasoning(False)
        self._show_current_model_config_warning()

    def _toggle_thinking(self):
        enabled = self.think_btn.isChecked()
        agent.set_reasoning(enabled)

    def _on_undo_click(self):
        """撤销按钮回调：调 checkpoint.undo_last_checkpoint。"""
        from .. import checkpoint as _cp
        ok, msg = _cp.undo_last_checkpoint()
        self._show_toast(("✓ " if ok else "⚠ ") + msg, duration=3000 if ok else 5000)
        self._style_undo_btn()  # 刷新按钮状态（栈空了就灰掉）

    def _toggle_agent_mode(self):
        """点击 Plan / Act 按钮：切换 state.agent_mode 并刷新提示"""
        new_mode = "plan" if self.mode_btn.isChecked() else "act"
        state.agent_mode = new_mode
        self._style_mode_btn()
        self._refresh_header_compactness()  # 让 mode_btn 的文字按当前宽度更新
        # 提示用户切换效果（一闪即过的 toast）
        if new_mode == "plan":
            self._show_toast("🧠 已切到 Plan 模式：AI 只给方案不动手")
        else:
            self._show_toast("⚡ 已切到 Act 模式：AI 可直接执行工具")

    def _sync_header_from_session(self):
        """切会话后把顶栏（模型下拉 / Plan-Act / 思考）同步到当前会话的状态。
        model/mode/思考 现在是会话级——切到哪个会话，顶栏就显示那个会话的选择。
        setCurrentIndex 会触发 _on_model_changed（含 force_stop），切会话时必须 blockSignals 屏蔽。"""
        from .. import session as _session
        sess = _session.get_active()
        if hasattr(self, "model_combo"):
            self.model_combo.blockSignals(True)
            self.model_combo.setCurrentIndex(sess.current_model_index)
            self.model_combo.blockSignals(False)
            _, _, _, supports_think = agent.MODEL_LIST[sess.current_model_index]
            if hasattr(self, "think_btn"):
                self.think_btn.setEnabled(supports_think)
                self.think_btn.setChecked(bool(sess.reasoning_enabled and supports_think))
        if hasattr(self, "mode_btn"):
            self.mode_btn.setChecked(sess.agent_mode == "plan")
            self._style_mode_btn()
        if hasattr(self, "_refresh_header_compactness"):
            self._refresh_header_compactness()

    # ── 角色卡 ──

    def _restore_role_card_ui(self):
        """启动时恢复角色卡按钮状态"""
        name = agent.get_current_role_name()
        if name:
            self.role_btn.setText(name)
            self._style_role_btn(active=True)
        else:
            self.role_btn.setText("角色卡")
            self._style_role_btn(active=False)
        # 让窗口窄的时候角色名也跟着截断
        if hasattr(self, "model_combo"):  # 主 UI 已构造完
            self._refresh_header_compactness()

    def _load_role_card(self):
        from .. import session as _session
        if _session.get_active().is_generating:
            self._force_stop_generation()

        def _apply_role_card(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                role_name = os.path.splitext(os.path.basename(path))[0]
                agent.set_role_card(content, role_name, path)

                # 新建对话应用角色
                agent.reset_history()
                self.chat_area.clear()
                self._refresh_session_list()

                # 更新按钮样式
                display_name = agent.get_current_role_name() or role_name
                self.role_btn.setText(display_name)
                self._style_role_btn(active=True)
                self._refresh_header_compactness()
                self._append_html(f"✅ 已加载角色卡: {display_name}\n\n", "tool_result")
            except Exception as e:
                QMessageBox.critical(self, "错误", f"读取角色卡失败: {e}")

        # 弹出菜单：roles/ 快捷切换 / 导入 / 清除
        menu = QMenu(self)
        role_actions = {}
        roles_dir = os.path.join(BASE_DIR, "roles")
        current = agent.get_current_role_name()
        current_path = os.path.normcase(os.path.abspath(agent.get_current_role_path() or ""))
        # 模板 / 说明类文件名不当作可切换角色（example.md / README.md / 模板.md）
        _ROLE_SKIP = {"example", "readme", "template", "模板", "示例"}
        role_files = []
        if os.path.isdir(roles_dir):
            try:
                role_files = sorted(
                    os.path.join(roles_dir, name)
                    for name in os.listdir(roles_dir)
                    if name.lower().endswith(".md")
                    and os.path.isfile(os.path.join(roles_dir, name))
                    and os.path.splitext(name)[0].lower() not in _ROLE_SKIP
                )
            except Exception:
                role_files = []

        for path in role_files:
            name = os.path.splitext(os.path.basename(path))[0]
            action = menu.addAction(self._svg_icon("id_card_lucide.svg", self._t("menu_text")), name)
            action.setCheckable(True)
            action.setChecked(os.path.normcase(os.path.abspath(path)) == current_path)
            role_actions[action] = path

        if role_files:
            menu.addSeparator()
        load_action = menu.addAction(self._svg_icon("folder_open_lucide.svg", self._t("menu_text")), "导入角色卡 (.md)")
        clear_action = menu.addAction(self._svg_icon("rotate_ccw_lucide.svg", self._t("menu_text")), "恢复默认角色")

        # 显示当前角色
        if current:
            menu.addSeparator()
            info = menu.addAction(f"当前: {current}")
            info.setEnabled(False)

        action = menu.exec(self.role_btn.mapToGlobal(self.role_btn.rect().bottomLeft()))

        if action in role_actions:
            _apply_role_card(role_actions[action])

        elif action == load_action:
            path, _ = QFileDialog.getOpenFileName(
                self, "选择角色卡文件", "",
                "Markdown 文件 (*.md);;文本文件 (*.txt);;所有文件 (*)"
            )
            if path:
                _apply_role_card(path)

        elif action == clear_action:
            agent.clear_role_card()
            agent.reset_history()
            self.chat_area.clear()
            self._refresh_session_list()
            self.role_btn.setText("角色卡")
            self._style_role_btn(active=False)
            self._append_html("✅ 已恢复默认角色\n\n", "tool_result")

    # ══════════════════════════════════════
    # 按钮样式（顶栏 + 输入区）
    # ══════════════════════════════════════

    def _style_model_combo(self):
        arrow_path = os.path.join(BASE_DIR, "icons", "chevron_down.svg").replace("\\", "/")
        self.model_combo.setStyleSheet(
            f"QComboBox {{ background: {self._t('combo_bg')}; border: 1px solid {self._t('combo_border')}; border-radius: 8px; "
            f"padding: 7px 38px 7px 14px; font-size: 13px; color: {self._t('combo_text')}; min-width: 210px; }}"
            f"QComboBox:hover {{ border-color: {self._t('combo_hover_border')}; color: {self._t('combo_hover_text')}; }}"
            f"QComboBox::drop-down {{ border: none; width: 34px; subcontrol-origin: padding; subcontrol-position: top right; }}"
            f"QComboBox::down-arrow {{ image: url({arrow_path}); width: 16px; height: 16px; margin-right: 10px; }}"
            f"QComboBox QAbstractItemView {{ background: {self._t('combo_view_bg')}; border: 1px solid {self._t('combo_view_border')}; "
            f"color: {self._t('combo_view_text')}; selection-background-color: {self._t('combo_view_sel_bg')}; "
            f"selection-color: {self._t('combo_view_sel_text')}; padding: 4px; outline: 0; }}"
        )

    def _style_think_btn(self):
        color = self._t("think_on_text") if self.think_btn.isChecked() else self._t("think_off_text")
        self.think_btn.setIcon(self._svg_icon("brain_lucide.svg", color))
        self.think_btn.setIconSize(QSize(16, 16))
        self.think_btn.setStyleSheet(
            f"QPushButton {{ border-radius: 8px; padding: 6px 14px; font-size: 12px; }}"
            f"QPushButton:checked {{ background: {self._t('think_on_bg')}; border: 1px solid {self._t('think_on_border')}; color: {self._t('think_on_text')}; }}"
            f"QPushButton:!checked {{ background: {self._t('think_off_bg')}; border: 1px solid {self._t('think_off_border')}; color: {self._t('think_off_text')}; }}"
            f"QPushButton:hover:checked {{ background: {self._t('think_on_hover')}; border-color: {self._t('think_on_hover_border')}; }}"
            f"QPushButton:hover:!checked {{ border-color: {self._t('think_off_hover_border')}; color: {self._t('think_off_hover_text')}; }}"
        )

    def _style_undo_btn(self):
        """撤销按钮配色：有 checkpoint 时高亮可点，无则灰禁。"""
        from .. import checkpoint as _cp
        has_cp = _cp.has_undoable_checkpoint()
        self.undo_btn.setEnabled(has_cp)
        if has_cp:
            self.undo_btn.setStyleSheet(
                f"QPushButton {{ background: {self._t('think_off_bg')};"
                f"  border: 1px solid {self._t('think_off_border')};"
                f"  border-radius: 8px; padding: 6px 12px; font-size: 12px;"
                f"  color: {self._t('warn')}; font-weight: 600;"
                f"}}"
                f"QPushButton:hover {{ background: {self._t('history_hover_bg')};"
                f"  border-color: {self._t('warn')}; }}"
            )
            info = _cp.latest_checkpoint_info() or {}
            tool = info.get("tool", "")
            path = info.get("path", "")
            name = path.split("/")[-1].split("\\")[-1] if path else ""
            self.undo_btn.setToolTip(
                f"撤销 AI 最近一次对文件的修改\n上次：{tool} → {name}"
            )
        else:
            self.undo_btn.setStyleSheet(
                f"QPushButton {{ background: transparent;"
                f"  border: 1px solid {self._t('input_border')};"
                f"  border-radius: 8px; padding: 6px 12px; font-size: 12px;"
                f"  color: {self._t('text_subtle')};"
                f"}}"
            )
            self.undo_btn.setToolTip("还没有可撤销的 AI 改动")

    def _style_mode_btn(self):
        """Plan / Act 切换按钮配色：Act = 想到就动手，Plan = 先想后动。
        注：文字内容由 _refresh_header_compactness 根据窗口宽度统一管理，这里不设 text。"""
        is_plan = self.mode_btn.isChecked()
        if is_plan:
            # Plan 模式：用 think_on 的紫色系，提示"在思考"
            self.mode_btn.setIcon(self._svg_icon("brain_lucide.svg", self._t("thinking")))
            self.mode_btn.setToolTip("Plan 模式：AI 只调研给方案，不动手改东西\n点击切回 Act 模式（直接执行）")
        else:
            # Act 模式：橙色提示"在动手"
            self.mode_btn.setIcon(self._svg_icon("sparkles_lucide.svg", self._t("ai_label")))
            self.mode_btn.setToolTip("Act 模式：AI 可直接执行工具\n点击切到 Plan 模式（先给方案再确认执行）")
        self.mode_btn.setIconSize(QSize(16, 16))
        self.mode_btn.setStyleSheet(
            f"QPushButton {{ border-radius: 8px; padding: 6px 14px; font-size: 12px;"
            f"  background: {self._t('think_on_bg') if is_plan else self._t('think_off_bg')};"
            f"  border: 1px solid {self._t('think_on_border') if is_plan else self._t('think_off_border')};"
            f"  color: {self._t('thinking') if is_plan else self._t('ai_label')};"
            f"  font-weight: 600;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: {self._t('think_on_hover') if is_plan else self._t('history_hover_bg')};"
            f"}}"
        )

    def _style_role_btn(self, active):
        color = self._t("role_active_text") if active else self._t("role_text")
        self.role_btn.setIcon(self._svg_icon("id_card_lucide.svg", color))
        self.role_btn.setIconSize(QSize(16, 16))
        if active:
            self.role_btn.setStyleSheet(
                f"QPushButton {{ background: {self._t('role_active_bg')}; border: 1px solid {self._t('role_active_border')}; border-radius: 8px; "
                f"padding: 6px 14px; font-size: 12px; color: {self._t('role_active_text')}; font-weight: {self._t('role_active_weight')}; }}"
                f"QPushButton:hover {{ background: {self._t('role_active_hover_bg')}; border-color: {self._t('role_active_hover_border')}; color: {self._t('role_active_hover_text')}; }}"
            )
        else:
            self.role_btn.setStyleSheet(
                f"QPushButton {{ background: {self._t('role_bg')}; border: 1px solid {self._t('role_border')}; border-radius: 8px; "
                f"padding: 6px 14px; font-size: 12px; color: {self._t('role_text')}; }}"
                f"QPushButton:hover {{ background: {self._t('role_hover_bg')}; border-color: {self._t('role_hover_border')}; color: {self._t('role_hover_text')}; }}"
            )

    def _style_settings_btn(self):
        color = self._t('text_dim')
        hover_color = self._t('text')
        svg_path = os.path.join(BASE_DIR, "icons", "settings_lucide.svg")

        def _svg_to_icon(svg_str, clr, size=20):
            from PySide6.QtSvg import QSvgRenderer
            svg_filled = svg_str.replace('currentColor', clr)
            renderer = QSvgRenderer(svg_filled.encode('utf-8'))
            dpr = self.devicePixelRatioF() if hasattr(self, 'devicePixelRatioF') else 1.0
            px = QPixmap(int(size * dpr), int(size * dpr))
            px.fill(Qt.transparent)
            painter = QPainter(px)
            renderer.render(painter)
            painter.end()
            px.setDevicePixelRatio(dpr)
            return QIcon(px)

        if os.path.exists(svg_path):
            with open(svg_path, 'r', encoding='utf-8') as f:
                svg_tpl = f.read()
            self._settings_btn_icon = _svg_to_icon(svg_tpl, color)
            self._settings_btn_icon_hover = _svg_to_icon(svg_tpl, hover_color)
        else:
            self._settings_btn_icon = QIcon()
            self._settings_btn_icon_hover = QIcon()

        self.settings_btn.setText("")
        self.settings_btn.setIcon(self._settings_btn_icon)
        self.settings_btn.setIconSize(QSize(19, 19))

    def _style_img_btn(self):
        color = self._t('img_btn')
        hover_color = self._t('img_btn_hover')
        # 用 plus 图标（点击弹菜单：上传图片 / 导入项目）
        svg_path = os.path.join(BASE_DIR, "icons", "plus_lucide.svg")
        if os.path.exists(svg_path):
            from PySide6.QtSvg import QSvgRenderer
            with open(svg_path, 'r', encoding='utf-8') as f:
                svg_tpl = f.read()
            def _svg_to_icon(svg_str, clr):
                svg_filled = svg_str.replace('currentColor', clr)
                renderer = QSvgRenderer(svg_filled.encode('utf-8'))
                # 取设备像素比，画布渲染高分屏才不糊
                dpr = self.devicePixelRatioF() if hasattr(self, 'devicePixelRatioF') else 1.0
                px = QPixmap(int(24 * dpr), int(24 * dpr))
                px.fill(Qt.transparent)
                painter = QPainter(px)
                renderer.render(painter)
                painter.end()
                px.setDevicePixelRatio(dpr)
                return QIcon(px)
            self._img_btn_icon = _svg_to_icon(svg_tpl, color)
            self._img_btn_icon_hover = _svg_to_icon(svg_tpl, hover_color)
        else:
            self._img_btn_icon = _make_upload_icon(color)
            self._img_btn_icon_hover = _make_upload_icon(hover_color)
        self.img_btn.setIcon(self._img_btn_icon)
        self.img_btn.setIconSize(QSize(20, 20))
        self.img_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; padding: 4px; border-radius: 4px; }"
            "QPushButton:hover { background: rgba(0,0,0,0.06); }"
        )

    def _style_mic_btn(self, recording=False):
        self._mic_recording = recording
        base_color = self._t('img_btn')
        hover_color = self._t('img_btn_hover')
        active_color = "#ef4444"
        color = active_color if recording else base_color
        self._mic_btn_icon = self._svg_icon("mic_lucide.svg", color)
        self._mic_btn_icon_hover = self._svg_icon("mic_lucide.svg", active_color if recording else hover_color)
        self.mic_btn.setIcon(self._mic_btn_icon)
        self.mic_btn.setIconSize(QSize(20, 20))
        self.mic_btn.setToolTip("语音输入：点击开始录音，再次点击结束识别")
        if recording:
            self.mic_btn.setStyleSheet(
                "QPushButton { background: rgba(239,68,68,0.13); border: none; padding: 4px; border-radius: 8px; }"
                "QPushButton:hover { background: rgba(239,68,68,0.22); }"
                "QPushButton:disabled { background: rgba(148,163,184,0.12); }"
            )
        else:
            self.mic_btn.setStyleSheet(
                "QPushButton { background: transparent; border: none; padding: 4px; border-radius: 8px; }"
                "QPushButton:hover { background: rgba(91,102,214,0.10); }"
                "QPushButton:disabled { background: rgba(148,163,184,0.12); }"
            )

    def _style_tts_btn(self, enabled=False):
        self._tts_enabled = enabled
        base_color = self._t('img_btn')
        hover_color = self._t('img_btn_hover')
        active_color = "#22c55e"
        filename = "speaker_on_lucide.svg" if enabled else "speaker_off_lucide.svg"
        color = active_color if enabled else base_color
        self._tts_btn_icon = self._svg_icon(filename, color)
        self._tts_btn_icon_hover = self._svg_icon(filename, active_color if enabled else hover_color)
        self.tts_btn.setIcon(self._tts_btn_icon)
        self.tts_btn.setIconSize(QSize(20, 20))
        self.tts_btn.setChecked(enabled)
        self.tts_btn.setToolTip("模型语音输出：开启后自动朗读 AI 回复" if enabled else "模型语音输出：当前关闭")
        if enabled:
            self.tts_btn.setStyleSheet(
                "QPushButton { background: rgba(34,197,94,0.13); border: none; padding: 4px; border-radius: 8px; }"
                "QPushButton:hover { background: rgba(34,197,94,0.22); }"
            )
        else:
            self.tts_btn.setStyleSheet(
                "QPushButton { background: transparent; border: none; padding: 4px; border-radius: 8px; }"
                "QPushButton:hover { background: rgba(91,102,214,0.10); }"
            )
