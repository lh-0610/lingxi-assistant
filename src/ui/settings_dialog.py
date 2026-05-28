"""设置弹窗（VSCode 风格的可编辑配置面板）。

直接读写 config.json，保存后提示重启生效。
也负责发起 / 停止 GPT-SoVITS 启动器，并订阅状态变化。
"""
import json
import os
import sys

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QScrollArea, QTextEdit, QVBoxLayout, QWidget,
)

from ._base import BASE_DIR, CONFIG_PATH
from ..paths import APP_DIR as _APP_DIR


class SettingsDialog(QDialog):
    """直接在 UI 里编辑 config.json。保存后写回文件，提示重启生效。

    布局：
      [⚠ 重启提示 banner]
      ┌──────────────────────┐
      │ 滚动表单（按分类）    │
      │  - 大模型 API 密钥    │
      │  - 本地服务           │
      │  - ComfyUI 出图       │
      └──────────────────────┘
      [打开 roles] [打开 logs] [关于]  .....  [取消] [保存]
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.resize(680, 640)
        self.setModal(True)
        self._parent_window = parent

        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                self.config = json.load(f)
        except Exception:
            self.config = {}

        self.fields = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        banner = QLabel("⚠ 修改密钥 / Base URL 后需重启应用才能生效")
        banner.setObjectName("settingsBanner")
        banner.setWordWrap(True)
        root.addWidget(banner)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        form = QWidget()
        form_layout = QVBoxLayout(form)
        form_layout.setContentsMargins(28, 20, 28, 20)
        form_layout.setSpacing(18)

        # ── 大模型配置：按 provider 分卡片 ──
        self._add_section(form_layout, "大模型配置")

        self._add_provider_card(
            form_layout, "通义千问 (Qwen)",
            [
                ("qwen_api_key",  "API Key",  "sk-...",                                                True),
                ("qwen_base_url", "Base URL", "https://dashscope.aliyuncs.com/compatible-mode/v1",     False),
            ],
        )
        self._add_provider_card(
            form_layout, "Anthropic Claude",
            [("anthropic_api_key", "API Key", "sk-ant-...", True)],
            hint="API 官方端点，无需 Base URL",
        )
        self._add_provider_card(
            form_layout, "MiMo（Anthropic 兼容）",
            [
                ("mimo_api_key",  "API Key",  "tp-...",                                                True),
                ("mimo_base_url", "Base URL", "https://token-plan-sgp.xiaomimimo.com/anthropic",       False),
            ],
        )
        self._add_provider_card(
            form_layout, "DeepSeek",
            [
                ("deepseek_api_key",  "API Key",  "sk-...",                       True),
                ("deepseek_base_url", "Base URL", "https://api.deepseek.com",     False),
            ],
        )
        self._add_provider_card(
            form_layout, "Google Gemini",
            [("google_api_key", "API Key", "AIza...", True)],
        )
        self._add_provider_card(
            form_layout, "Ollama 本地",
            [("ollama_base_url", "Base URL", "http://127.0.0.1:11434", False)],
            hint="本机部署，无需 API Key",
        )

        # ── 自定义模型 ──
        self._add_custom_models_section(form_layout)

        # ── MCP（高级可选）──
        self._add_section(form_layout, "MCP（高级 · 可选）")
        self._add_bool(form_layout, "mcp_enabled",
                       "启用 MCP（连接外部工具服务器）", default=True)
        mcp_hint = QLabel(
            "连外部 MCP server（filesystem / fetch / github 等），把它们的工具给 AI 用。\n"
            "• 关掉这个开关 = 完全不连 MCP，灵犀内置工具照常用\n"
            "• server 列表在 config.json 的 mcp_servers 里编辑（暂不支持 UI 增删）\n"
            "  —— config.json 在哪？点本弹窗左下角「config」按钮直达目录\n"
            "• stdio 类型的 server 需要你的机器装了 Node.js\n"
            "• 改动需重启应用才生效"
        )
        mcp_hint.setObjectName("providerCardHint")
        mcp_hint.setWordWrap(True)
        form_layout.addWidget(mcp_hint)

        self._add_section(form_layout, "ComfyUI 出图")
        self._add_text(form_layout, "comfy_base_url", "ComfyUI Base URL", "http://127.0.0.1:8188")
        self._add_text(form_layout, "comfy_checkpoint", "默认 Checkpoint",
                       "例如：autismmixSDXL_autismmixPony.safetensors")
        self._add_text(form_layout, "comfy_workflow_path",
                       "自定义工作流 JSON（API 格式，可选）", "留空 = 用内置模板")
        self._add_bool(form_layout, "comfy_face_detailer",
                       "启用 FaceDetailer 修脸（需在 ComfyUI 装 Impact Pack）")

        self._add_gpt_sovits_section(form_layout)

        form_layout.addStretch()
        scroll.setWidget(form)
        root.addWidget(scroll, 1)

        # 底部按钮区
        bottom = QWidget()
        bottom.setObjectName("settingsBottom")
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(16, 10, 16, 12)
        bottom_layout.setSpacing(8)

        open_roles = QPushButton("roles")
        open_roles.setIcon(self._settings_icon("folder_lucide.svg"))
        open_roles.setIconSize(QSize(17, 17))
        open_roles.setToolTip("打开角色卡目录")
        open_roles.setCursor(Qt.PointingHandCursor)
        open_roles.clicked.connect(lambda: self._open_path(os.path.join(BASE_DIR, "roles")))

        open_logs = QPushButton("logs")
        open_logs.setIcon(self._settings_icon("logs_lucide.svg"))
        open_logs.setIconSize(QSize(17, 17))
        open_logs.setToolTip("打开日志目录")
        open_logs.setCursor(Qt.PointingHandCursor)
        # logs 是可写数据，在 APP_DIR（%APPDATA%\灵犀），不是只读资源 BASE_DIR
        open_logs.clicked.connect(lambda: self._open_path(os.path.join(_APP_DIR, "logs")))

        # 打开 config.json 所在目录（打包后在 %APPDATA%\灵犀，用户找不到，给个直达按钮）
        open_config = QPushButton("config")
        open_config.setIcon(self._settings_icon("folder_lucide.svg"))
        open_config.setIconSize(QSize(17, 17))
        open_config.setToolTip("打开 config.json 所在目录（编辑 mcp_servers 等）")
        open_config.setCursor(Qt.PointingHandCursor)
        open_config.clicked.connect(lambda: self._open_path(_APP_DIR))

        about_btn = QPushButton("关于")
        about_btn.setIcon(self._settings_icon("info_lucide.svg"))
        about_btn.setIconSize(QSize(17, 17))
        about_btn.setCursor(Qt.PointingHandCursor)
        about_btn.clicked.connect(self._about)

        cancel_btn = QPushButton("取消")
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.clicked.connect(self.reject)

        save_btn = QPushButton("保存")
        save_btn.setObjectName("settingsSaveBtn")
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._save)

        bottom_layout.addWidget(open_roles)
        bottom_layout.addWidget(open_logs)
        bottom_layout.addWidget(open_config)
        bottom_layout.addWidget(about_btn)
        bottom_layout.addStretch()
        bottom_layout.addWidget(cancel_btn)
        bottom_layout.addWidget(save_btn)
        root.addWidget(bottom)

        self._apply_dialog_style()

    # ── 表单原子 ──

    def _settings_icon(self, filename):
        """渲染设置弹窗底部按钮的单色线性图标。"""
        svg_path = os.path.join(BASE_DIR, "icons", filename)
        if not os.path.exists(svg_path):
            return QIcon()

        is_dark = bool(self._parent_window and getattr(self._parent_window, "theme", "light") == "dark")
        normal = "#9ca3af" if is_dark else "#4b5563"
        active = "#60a5fa" if is_dark else "#3b82f6"

        from PySide6.QtSvg import QSvgRenderer

        def _render(color, size=20):
            with open(svg_path, "r", encoding="utf-8") as f:
                svg = f.read().replace("currentColor", color)
            renderer = QSvgRenderer(svg.encode("utf-8"))
            dpr = self.devicePixelRatioF() if hasattr(self, "devicePixelRatioF") else 1.0
            px = QPixmap(int(size * dpr), int(size * dpr))
            px.fill(Qt.transparent)
            painter = QPainter(px)
            renderer.render(painter)
            painter.end()
            px.setDevicePixelRatio(dpr)
            return px

        icon = QIcon()
        icon.addPixmap(_render(normal), QIcon.Normal)
        icon.addPixmap(_render(active), QIcon.Active)
        return icon

    def _add_section(self, layout, title):
        lbl = QLabel(title)
        lbl.setObjectName("settingsSection")
        layout.addWidget(lbl)

    def _add_provider_card(self, layout, provider_name, fields, hint=None):
        """加一个 provider 配置卡片（带边框 + 标题 + 多个字段）。

        fields: [(config_key, label_text, placeholder, is_password), ...]
        hint: 可选的灰色辅助说明（如"无需 Base URL"）
        """
        card = QFrame()
        card.setObjectName("providerCard")
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(8)

        # 标题
        title = QLabel(provider_name)
        title.setObjectName("providerCardTitle")
        v.addWidget(title)
        if hint:
            hint_lbl = QLabel(hint)
            hint_lbl.setObjectName("providerCardHint")
            v.addWidget(hint_lbl)

        # 字段
        for key, label_text, placeholder, password in fields:
            self._add_text(v, key, label_text, placeholder, password=password)

        layout.addWidget(card)

    def _add_custom_models_section(self, layout):
        """自定义模型区：列出现有条目 + 添加按钮。

        数据存到 self._custom_models（list of dict），保存时整体写回 config.json
        的 custom_models 字段。重启应用后才在顶栏下拉里看到新模型——所以加完
        会提示用户重启。
        """
        from .. import config as _cfg
        # 拷贝出来本地编辑，避免直接改 config 模块全局
        self._custom_models = [dict(cm) for cm in (_cfg.CUSTOM_MODELS or [])]

        self._add_section(layout, "自定义模型（OpenAI / Anthropic 兼容）")

        # 容器：动态摆放每个 model 卡片
        self._custom_container = QWidget()
        self._custom_container_layout = QVBoxLayout(self._custom_container)
        self._custom_container_layout.setContentsMargins(0, 0, 0, 0)
        self._custom_container_layout.setSpacing(8)
        layout.addWidget(self._custom_container)

        # 添加按钮
        add_btn = QPushButton("+ 添加自定义模型")
        add_btn.setObjectName("customModelAddBtn")
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.setMinimumHeight(34)
        add_btn.clicked.connect(self._on_add_custom_model)
        layout.addWidget(add_btn)

        # 重启提示（小灰字，不显眼但能看见）
        tip = QLabel("提示：新增 / 修改自定义模型后需要重启应用才会出现在顶栏下拉里")
        tip.setObjectName("customModelTip")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        self._refresh_custom_models_ui()

    def _refresh_custom_models_ui(self):
        """重新摆放自定义模型卡片到容器里。"""
        # 清空
        while self._custom_container_layout.count():
            item = self._custom_container_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if not self._custom_models:
            empty = QLabel("（还没有自定义模型，点下面的按钮添加）")
            empty.setObjectName("providerCardHint")
            empty.setAlignment(Qt.AlignCenter)
            self._custom_container_layout.addWidget(empty)
            return

        for i, cm in enumerate(self._custom_models):
            self._custom_container_layout.addWidget(self._build_custom_model_card(i, cm))

    def _build_custom_model_card(self, index, cm):
        """单个自定义模型的可视化卡片：摘要 + 编辑/删除按钮。"""
        card = QFrame()
        card.setObjectName("providerCard")
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 10, 14, 10)
        v.setSpacing(4)

        # 标题行
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        title = QLabel(f"⚙ {cm.get('name', cm.get('model_id', '?'))}")
        title.setObjectName("providerCardTitle")
        head.addWidget(title, 1)

        edit_btn = QPushButton("编辑")
        edit_btn.setCursor(Qt.PointingHandCursor)
        edit_btn.setFixedHeight(28)
        edit_btn.clicked.connect(lambda _=False, i=index: self._on_edit_custom_model(i))
        head.addWidget(edit_btn)

        del_btn = QPushButton("删除")
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.setFixedHeight(28)
        del_btn.clicked.connect(lambda _=False, i=index: self._on_delete_custom_model(i))
        head.addWidget(del_btn)
        v.addLayout(head)

        # 摘要
        protocol = cm.get("protocol", "openai")
        info = (
            f"<span style='color:#888;'>"
            f"model_id: <code>{cm.get('model_id', '?')}</code> · "
            f"protocol: <code>{protocol}</code>"
        )
        if cm.get("base_url"):
            info += f" · base_url: <code>{cm.get('base_url')}</code>"
        flags = []
        if cm.get("supports_vision"):
            flags.append("视觉")
        if cm.get("supports_thinking"):
            flags.append("思考")
        if flags:
            info += " · " + " · ".join(flags)
        info += "</span>"
        info_lbl = QLabel(info)
        info_lbl.setObjectName("providerCardHint")
        info_lbl.setTextFormat(Qt.RichText)
        info_lbl.setWordWrap(True)
        v.addWidget(info_lbl)

        return card

    def _on_add_custom_model(self):
        cm = self._open_custom_model_editor({})
        if cm:
            self._custom_models.append(cm)
            self._refresh_custom_models_ui()

    def _on_edit_custom_model(self, index):
        if not (0 <= index < len(self._custom_models)):
            return
        cm = self._open_custom_model_editor(self._custom_models[index])
        if cm:
            self._custom_models[index] = cm
            self._refresh_custom_models_ui()

    def _on_delete_custom_model(self, index):
        if not (0 <= index < len(self._custom_models)):
            return
        cm = self._custom_models[index]
        reply = QMessageBox.question(
            self, "删除自定义模型",
            f"确认删除「{cm.get('name', cm.get('model_id', '?'))}」？\n"
            "这一项会从设置移除，重启后顶栏下拉里也消失。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            del self._custom_models[index]
            self._refresh_custom_models_ui()

    def _open_custom_model_editor(self, initial: dict):
        """弹一个小对话框编辑单个自定义模型。返回完整 dict（成功）或 None（取消）。"""
        dlg = _CustomModelEditor(self, initial)
        if dlg.exec() == QDialog.Accepted:
            return dlg.result_data
        return None

    def _add_text(self, layout, key, label_text, placeholder="", password=False):
        wrapper = QWidget()
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(5)

        lbl = QLabel(label_text)
        lbl.setObjectName("settingsLabel")
        wl.addWidget(lbl)

        edit = QLineEdit()
        edit.setText(str(self.config.get(key, "")))
        edit.setPlaceholderText(placeholder)
        edit.setObjectName("settingsInput")
        edit.setMinimumHeight(32)

        if password:
            edit.setEchoMode(QLineEdit.Password)
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            row.addWidget(edit, 1)

            eye = QPushButton("show")
            eye.setObjectName("settingsEye")
            eye.setFixedSize(48, 32)
            eye.setCursor(Qt.PointingHandCursor)
            eye.setCheckable(True)
            eye.setToolTip("显示 / 隐藏")

            def _toggle_eye(checked, e=edit, b=eye):
                e.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)
                b.setText("hide" if checked else "show")
            eye.toggled.connect(_toggle_eye)

            row.addWidget(eye)

            row_wrap = QWidget()
            row_wrap.setLayout(row)
            wl.addWidget(row_wrap)
        else:
            wl.addWidget(edit)

        layout.addWidget(wrapper)
        self.fields[key] = edit

    def _add_bool(self, layout, key, label_text, default=False):
        cb = QCheckBox(label_text)
        cb.setObjectName("settingsCheck")
        cb.setChecked(bool(self.config.get(key, default)))
        cb.setCursor(Qt.PointingHandCursor)
        layout.addWidget(cb)
        self.fields[key] = cb

    def _add_path_picker(self, layout, key, label_text, placeholder="",
                         file_dialog=False, file_filter="所有文件 (*)"):
        """带"浏览"按钮的路径输入。file_dialog=False 选目录，True 选文件。"""
        wrapper = QWidget()
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(5)

        lbl = QLabel(label_text)
        lbl.setObjectName("settingsLabel")
        wl.addWidget(lbl)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        edit = QLineEdit()
        edit.setText(str(self.config.get(key, "")))
        edit.setPlaceholderText(placeholder)
        edit.setObjectName("settingsInput")
        edit.setMinimumHeight(32)
        row.addWidget(edit, 1)

        browse = QPushButton("浏览...")
        browse.setFixedHeight(32)
        browse.setCursor(Qt.PointingHandCursor)

        def _on_browse():
            if file_dialog:
                path, _ = QFileDialog.getOpenFileName(self, label_text, edit.text(), file_filter)
            else:
                path = QFileDialog.getExistingDirectory(self, label_text, edit.text())
            if path:
                edit.setText(path)
        browse.clicked.connect(_on_browse)

        row.addWidget(browse)

        row_wrap = QWidget()
        row_wrap.setLayout(row)
        wl.addWidget(row_wrap)

        layout.addWidget(wrapper)
        self.fields[key] = edit

    def _add_textarea(self, layout, key, label_text, placeholder=""):
        wrapper = QWidget()
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(5)

        lbl = QLabel(label_text)
        lbl.setObjectName("settingsLabel")
        wl.addWidget(lbl)

        edit = QTextEdit()
        edit.setPlainText(str(self.config.get(key, "")))
        edit.setPlaceholderText(placeholder)
        edit.setObjectName("settingsInput")
        edit.setFixedHeight(70)
        wl.addWidget(edit)

        layout.addWidget(wrapper)
        self.fields[key] = edit  # 注意：QTextEdit，保存时要用 toPlainText()

    def _add_gpt_sovits_section(self, layout):
        """语音模块设置 + 启动 / 停止按钮 + 状态指示。"""
        self._add_section(layout, "语音模块（GPT-SoVITS）")

        self._add_path_picker(
            layout, "gpt_sovits_install_dir",
            "GPT-SoVITS 安装目录",
            placeholder="例如：D:/语音/GPT-SoVITS-v3lora-20250228/GPT-SoVITS-v3lora-20250228",
            file_dialog=False,
        )
        self._add_text(
            layout, "gpt_sovits_gpt_model", "GPT 权重（相对路径）",
            "例如：GPT_weights_v2/你的GPT权重.ckpt",
        )
        self._add_text(
            layout, "gpt_sovits_sovits_model", "SoVITS 权重（相对路径）",
            "例如：SoVITS_weights_v2/你的SoVITS权重.pth",
        )
        self._add_path_picker(
            layout, "gpt_sovits_ref_audio", "参考音频（WAV）",
            placeholder="例如：D:/你的/参考音频.wav",
            file_dialog=True, file_filter="音频文件 (*.wav *.mp3 *.flac);;所有文件 (*)",
        )
        self._add_textarea(
            layout, "gpt_sovits_prompt_text", "参考音频对应文本（必须一字不差）",
            placeholder="例如：哎呀，看到美少女突然来访...",
        )

        # 控制 + 状态行
        ctrl_wrap = QWidget()
        ctrl_layout = QHBoxLayout(ctrl_wrap)
        ctrl_layout.setContentsMargins(0, 8, 0, 0)
        ctrl_layout.setSpacing(10)

        self._gpt_sovits_start_btn = QPushButton("启动语音模块")
        self._gpt_sovits_start_btn.setCursor(Qt.PointingHandCursor)
        self._gpt_sovits_start_btn.setMinimumHeight(34)
        self._gpt_sovits_start_btn.clicked.connect(self._on_gpt_sovits_start)

        self._gpt_sovits_stop_btn = QPushButton("停止")
        self._gpt_sovits_stop_btn.setCursor(Qt.PointingHandCursor)
        self._gpt_sovits_stop_btn.setMinimumHeight(34)
        self._gpt_sovits_stop_btn.clicked.connect(self._on_gpt_sovits_stop)

        self._gpt_sovits_status_label = QLabel("⚫ 未启动")
        self._gpt_sovits_status_label.setObjectName("settingsLabel")

        ctrl_layout.addWidget(self._gpt_sovits_start_btn)
        ctrl_layout.addWidget(self._gpt_sovits_stop_btn)
        ctrl_layout.addStretch()
        ctrl_layout.addWidget(self._gpt_sovits_status_label)

        layout.addWidget(ctrl_wrap)

        # 订阅 launcher 状态变化
        launcher = self._get_launcher()
        if launcher is not None:
            launcher.status_changed.connect(self._on_launcher_status)
            # 初始化显示
            self._on_launcher_status(launcher.state, "")

    def _get_launcher(self):
        """从主窗口拿到 launcher 实例（懒）。"""
        pw = self._parent_window
        if pw is None:
            return None
        return getattr(pw, "_gpt_sovits_launcher", None)

    def _on_gpt_sovits_start(self):
        # 先把当前表单里输的路径/权重写回内存，让 launcher 用最新的
        install_dir = self.fields["gpt_sovits_install_dir"].text().strip()
        gpt_model = self.fields["gpt_sovits_gpt_model"].text().strip()
        sovits_model = self.fields["gpt_sovits_sovits_model"].text().strip()
        if not install_dir:
            QMessageBox.warning(self, "缺少配置", "请先填 GPT-SoVITS 安装目录。")
            return
        launcher = self._get_launcher()
        if launcher is None:
            QMessageBox.warning(self, "出错", "找不到语音模块启动器。")
            return
        launcher.start(install_dir, gpt_model, sovits_model)

    def _on_gpt_sovits_stop(self):
        launcher = self._get_launcher()
        if launcher is None:
            return
        launcher.stop()

    def _on_launcher_status(self, state, msg):
        text_map = {
            "stopped":  "⚫ 未启动",
            "starting": f"🟡 启动中... {msg}",
            "running":  "🟢 已启动",
            "failed":   f"🔴 失败：{msg}",
        }
        if hasattr(self, "_gpt_sovits_status_label"):
            self._gpt_sovits_status_label.setText(text_map.get(state, state))

    # ── 操作 ──

    def _save(self):
        for key, widget in self.fields.items():
            if isinstance(widget, QCheckBox):
                self.config[key] = widget.isChecked()
            elif isinstance(widget, QTextEdit):
                self.config[key] = widget.toPlainText().strip()
            else:
                self.config[key] = widget.text().strip()

        # 自定义模型列表整体写回（_open_custom_model_editor 已经把字段都填好了）
        if hasattr(self, "_custom_models"):
            self.config["custom_models"] = [
                {k: v for k, v in cm.items() if v not in ("", None)}
                for cm in self._custom_models
            ]

        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return

        QMessageBox.information(
            self, "已保存",
            "配置已写入 config.json\n密钥 / Base URL 改动需要重启应用才能生效。",
        )
        self.accept()

    def _open_path(self, path):
        import subprocess
        if not os.path.exists(path):
            QMessageBox.warning(self, "找不到", f"路径不存在：\n{path}")
            return
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.run(["open", path], check=False)
            else:
                subprocess.run(["xdg-open", path], check=False)
        except Exception as e:
            QMessageBox.warning(self, "打开失败", str(e))

    def _about(self):
        from .. import __version__ as _ver
        QMessageBox.about(
            self, "关于灵犀",
            "<div style='min-width:280px'>"
            "<h2 style='margin:0'>灵犀 AI 助手</h2>"
            "<p style='color:#888;margin:4px 0 14px 0'>多模型 AI 桌面客户端</p>"
            f"<p><b>版本</b>：{_ver}</p>"
            "<p><b>技术栈</b>：LangChain + PySide6</p>"
            "<p><b>支持上游</b>：MiMo · Qwen · Claude · DeepSeek · Ollama · Claude Code</p>"
            "<p><b>本地能力</b>：ComfyUI 出图 · 多角色卡 · 持久化记忆</p>"
            "<p style='margin-top:16px'><a href='https://github.com/'>GitHub 源码</a></p>"
            "</div>"
        )

    # ── 样式 ──

    def _apply_dialog_style(self):
        is_dark = bool(self._parent_window and getattr(self._parent_window, "theme", "light") == "dark")
        bg = "#0f1318" if is_dark else "#ffffff"
        fg = "#e6eaf2" if is_dark else "#1f2329"
        muted = "#7a8794" if is_dark else "#6b7280"
        border = "#1f2937" if is_dark else "#e5e7eb"
        accent = "#3b82f6"
        accent_hover = "#2563eb"
        banner_bg = "#332615" if is_dark else "#fff7ed"
        banner_fg = "#fbbf24" if is_dark else "#9a3412"
        banner_border = "#653e15" if is_dark else "#fed7aa"
        input_bg = "#171b22" if is_dark else "#fafbfc"
        input_focus_bg = "#1a1f27" if is_dark else "#ffffff"
        bottom_bg = "#0c1015" if is_dark else "#f9fafb"
        check_icon = os.path.join(BASE_DIR, "icons", "check_white.svg").replace("\\", "/")

        self.setStyleSheet(
            f"QDialog {{ background: {bg}; color: {fg}; }}\n"
            f"#settingsBanner {{ background: {banner_bg}; color: {banner_fg};"
            f" border-bottom: 1px solid {banner_border}; padding: 9px 18px; font-size: 12px; }}\n"
            f"#settingsSection {{ color: {muted}; font-size: 11px; font-weight: 700;"
            f" letter-spacing: 1px; padding: 6px 0 0 0; }}\n"
            f"#settingsLabel {{ color: {fg}; font-size: 13px; font-weight: 500; }}\n"
            f"#settingsInput {{ background: {input_bg}; border: 1px solid {border};"
            f" border-radius: 6px; padding: 6px 10px; color: {fg}; font-size: 13px;"
            f" font-family: \"Consolas\", \"Microsoft YaHei UI\", monospace; }}\n"
            f"#settingsInput:focus {{ border-color: {accent}; background: {input_focus_bg}; }}\n"
            f"#settingsEye {{ background: {input_bg}; border: 1px solid {border};"
            f" border-radius: 6px; color: {muted}; font-size: 11px; }}\n"
            f"#settingsEye:checked {{ color: {accent}; }}\n"
            f"#settingsEye:hover {{ border-color: {accent}; }}\n"
            f"#settingsCheck {{ color: {fg}; font-size: 13px; spacing: 8px; }}\n"
            f"#settingsCheck::indicator {{ width: 16px; height: 16px;"
            f" border-radius: 4px; border: 1px solid {border}; background: {input_focus_bg}; }}\n"
            f"#settingsCheck::indicator:hover {{ border-color: {accent}; }}\n"
            f"#settingsCheck::indicator:checked {{ background: {accent}; border-color: {accent};"
            f" image: url(\"{check_icon}\"); }}\n"
            f"#settingsCheck::indicator:checked:hover {{ background: {accent_hover};"
            f" border-color: {accent_hover}; }}\n"
            f"#settingsBottom {{ background: {bottom_bg}; border-top: 1px solid {border}; }}\n"
            f"#settingsBottom QPushButton {{ background: transparent; border: 1px solid {border};"
            f" border-radius: 6px; padding: 7px 14px; color: {fg}; font-size: 13px; }}\n"
            f"#settingsBottom QPushButton:hover {{ background: {input_bg};"
            f" border-color: {accent}; color: {accent}; }}\n"
            f"#settingsSaveBtn {{ background: {accent}; border-color: {accent}; color: #ffffff; }}\n"
            f"#settingsSaveBtn:hover {{ background: {accent_hover}; border-color: {accent_hover};"
            f" color: #ffffff; }}\n"
            # ── provider 卡片 + 自定义模型卡片样式 ──
            f"#providerCard {{ background: {input_bg}; border: 1px solid {border};"
            f" border-radius: 8px; }}\n"
            f"#providerCardTitle {{ color: {fg}; font-size: 13px; font-weight: 600;"
            f" background: transparent; padding: 0; }}\n"
            f"#providerCardHint {{ color: {muted}; font-size: 11px;"
            f" background: transparent; padding: 0; }}\n"
            f"#customModelAddBtn {{ background: transparent; border: 1.5px dashed {border};"
            f" border-radius: 8px; padding: 8px; color: {muted}; font-size: 13px; }}\n"
            f"#customModelAddBtn:hover {{ border-color: {accent}; color: {accent};"
            f" background: {input_bg}; }}\n"
            f"#customModelTip {{ color: {muted}; font-size: 11px;"
            f" background: transparent; padding: 4px 2px; }}\n"
            f"QScrollArea {{ background: {bg}; border: none; }}\n"
            f"QScrollBar:vertical {{ width: 8px; background: transparent; }}\n"
            f"QScrollBar::handle:vertical {{ background: {border};"
            f" border-radius: 4px; min-height: 32px; }}\n"
            f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}\n"
        )


class _CustomModelEditor(QDialog):
    """编辑单个自定义模型的小对话框。

    必填：display_name / model_id / protocol
    可选：api_key / base_url / supports_vision / supports_thinking
    取消返回，结果存放在 `result_data`（成功后是完整 dict）。
    """
    def __init__(self, parent, initial: dict):
        super().__init__(parent)
        self.setWindowTitle("自定义模型")
        self.setModal(True)
        self.resize(520, 460)
        self.result_data = None
        self._parent_window = parent

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 14)
        root.setSpacing(12)

        # 字段
        self.f_name = self._add_row(root, "显示名（顶栏下拉看到的）",
                                    initial.get("name", ""), "GPT-4 Turbo / 我的私有部署")
        self.f_model_id = self._add_row(root, "Model ID（API 调用时的 model 字段）",
                                        initial.get("model_id", ""),
                                        "gpt-4-turbo / claude-sonnet-4 / deepseek-chat ...")

        # protocol 下拉
        proto_wrap = QWidget()
        pwl = QVBoxLayout(proto_wrap)
        pwl.setContentsMargins(0, 0, 0, 0)
        pwl.setSpacing(5)
        pwl.addWidget(self._mk_label("API 协议"))
        self.f_protocol = QComboBox()
        self.f_protocol.addItem("OpenAI 兼容（默认 — 适配大多数第三方 API）", "openai")
        self.f_protocol.addItem("Anthropic 兼容（Claude 系 / MiMo 风）", "anthropic")
        # 设当前值
        protocol_val = (initial.get("protocol") or "openai").lower()
        idx = self.f_protocol.findData(protocol_val)
        if idx >= 0:
            self.f_protocol.setCurrentIndex(idx)
        pwl.addWidget(self.f_protocol)
        root.addWidget(proto_wrap)

        self.f_api_key = self._add_row(root, "API Key", initial.get("api_key", ""),
                                       "sk-...", password=True)
        self.f_base_url = self._add_row(root, "Base URL（可选）",
                                        initial.get("base_url", ""),
                                        "https://api.openai.com/v1 / 留空 = 用 SDK 默认")

        # 能力 flags
        flags_wrap = QWidget()
        fwl = QHBoxLayout(flags_wrap)
        fwl.setContentsMargins(0, 0, 0, 0)
        fwl.setSpacing(20)
        self.f_vision = QCheckBox("支持图片输入（视觉）")
        self.f_vision.setChecked(bool(initial.get("supports_vision", False)))
        self.f_thinking = QCheckBox("支持思考模式（reasoning）")
        self.f_thinking.setChecked(bool(initial.get("supports_thinking", False)))
        fwl.addWidget(self.f_vision)
        fwl.addWidget(self.f_thinking)
        fwl.addStretch()
        root.addWidget(flags_wrap)

        root.addStretch()

        # 按钮
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("保存")
        save_btn.setObjectName("settingsSaveBtn")
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        root.addLayout(btn_row)

        self._apply_style()

    def _add_row(self, parent_layout, label_text, value, placeholder, password=False):
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(5)
        wl.addWidget(self._mk_label(label_text))
        edit = QLineEdit()
        edit.setText(str(value or ""))
        edit.setPlaceholderText(placeholder)
        edit.setObjectName("settingsInput")
        edit.setMinimumHeight(32)
        if password:
            edit.setEchoMode(QLineEdit.Password)
        wl.addWidget(edit)
        parent_layout.addWidget(wrap)
        return edit

    @staticmethod
    def _mk_label(text):
        lbl = QLabel(text)
        lbl.setObjectName("settingsLabel")
        return lbl

    def _on_save(self):
        name = self.f_name.text().strip()
        model_id = self.f_model_id.text().strip()
        if not name:
            QMessageBox.warning(self, "缺字段", "请填「显示名」。")
            return
        if not model_id:
            QMessageBox.warning(self, "缺字段", "请填「Model ID」。")
            return
        self.result_data = {
            "name": name,
            "model_id": model_id,
            "protocol": self.f_protocol.currentData() or "openai",
            "api_key": self.f_api_key.text().strip(),
            "base_url": self.f_base_url.text().strip(),
            "supports_vision": self.f_vision.isChecked(),
            "supports_thinking": self.f_thinking.isChecked(),
        }
        self.accept()

    def _apply_style(self):
        # 复用父对话框（SettingsDialog）的配色变量
        pw = self._parent_window
        is_dark = bool(pw and getattr(pw, "_parent_window", None)
                       and getattr(pw._parent_window, "theme", "light") == "dark")
        bg = "#0f1318" if is_dark else "#ffffff"
        fg = "#e6eaf2" if is_dark else "#1f2329"
        muted = "#7a8794" if is_dark else "#6b7280"
        border = "#1f2937" if is_dark else "#e5e7eb"
        accent = "#3b82f6"
        accent_hover = "#2563eb"
        input_bg = "#171b22" if is_dark else "#fafbfc"
        input_focus_bg = "#1a1f27" if is_dark else "#ffffff"
        self.setStyleSheet(
            f"QDialog {{ background: {bg}; color: {fg}; }}\n"
            f"#settingsLabel {{ color: {fg}; font-size: 13px; font-weight: 500; }}\n"
            f"#settingsInput {{ background: {input_bg}; border: 1px solid {border};"
            f" border-radius: 6px; padding: 6px 10px; color: {fg}; font-size: 13px;"
            f" font-family: 'Consolas', 'Microsoft YaHei UI', monospace; }}\n"
            f"#settingsInput:focus {{ border-color: {accent}; background: {input_focus_bg}; }}\n"
            f"QComboBox {{ background: {input_bg}; border: 1px solid {border};"
            f" border-radius: 6px; padding: 6px 10px; color: {fg}; font-size: 13px; min-height: 20px; }}\n"
            f"QComboBox:hover {{ border-color: {accent}; }}\n"
            f"QCheckBox {{ color: {fg}; font-size: 13px; spacing: 6px; }}\n"
            f"QPushButton {{ background: transparent; border: 1px solid {border};"
            f" border-radius: 6px; padding: 7px 16px; color: {fg}; font-size: 13px; }}\n"
            f"QPushButton:hover {{ background: {input_bg}; border-color: {accent}; color: {accent}; }}\n"
            f"#settingsSaveBtn {{ background: {accent}; border-color: {accent}; color: #ffffff; }}\n"
            f"#settingsSaveBtn:hover {{ background: {accent_hover}; border-color: {accent_hover}; }}\n"
        )
