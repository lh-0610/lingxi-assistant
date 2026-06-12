"""桌面悬浮宠物 + 系统托盘 + 动画。

动画优先级：
  1. GIF 动画（assets/desktop_pet/*_desktop_pet_final.gif）—— PIL 预提取成 QPixmap 序列
  2. 静态立绘（assets/desktop_pet/lingxi_pet.png）—— GIF 全坏时的兜底

交互：
  - 左键单击：唤起/隐藏主对话 + 播放一次 wave 打招呼
  - 左键拖动：移动位置（释放时持久化）
  - 右键：菜单（显示/隐藏对话、重置位置、退出）
  - Ctrl+Q：兜底退出
  - set_thinking(True/False)：外部（agent_loop）切换 think 动画
"""
import json
import os
import sys

from PySide6.QtCore import Qt, QSize, QTimer, Signal
from PySide6.QtGui import QPixmap, QAction, QIcon, QKeySequence, QShortcut, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QMenu,
    QSystemTrayIcon,
    QWidget,
)

from .paths import MEMORY_DIR, RESOURCE_DIR, logger
try:
    from .config import PET_ANIMATION_SPEED
except ImportError:
    PET_ANIMATION_SPEED = 0.5


def _restore_window(win):
    """显示主窗口，但**保留它原本的最大化/全屏状态**。

    别直接用 showNormal()——它会把最大化/全屏的窗口强制还原成普通尺寸（缩回默认
    1000×700）。这里去掉最小化标志、保留 Maximized/FullScreen，再 show()。
    """
    st = win.windowState()
    # 清掉最小化标志，保留最大化 / 全屏
    if st & Qt.WindowMinimized:
        win.setWindowState(st & ~Qt.WindowMinimized)
    win.show()  # show() 按当前 windowState 显示，最大化/全屏都不动


PET_MAX_HEIGHT = 320  # 桌面上显示的最终逻辑高度（像素）
PET_CONFIG_FILE = os.path.join(MEMORY_DIR, "pet_config.json")
PET_DIR = os.path.join(RESOURCE_DIR, "assets", "desktop_pet")  # 打包资源走 _MEIPASS，不是 exe 目录
GIF_FILES = {
    "idle":  os.path.join(PET_DIR, "idle_desktop_pet_final.gif"),
    "think": os.path.join(PET_DIR, "thinking_desktop_pet_final.gif"),
    "wave":  os.path.join(PET_DIR, "wave_desktop_pet_final.gif"),
}
FALLBACK_IMAGE = os.path.join(PET_DIR, "lingxi_pet.png")
DRAG_THRESHOLD = 5  # 像素，超过算拖动而不是点击


def _load_pet_config():
    try:
        if os.path.exists(PET_CONFIG_FILE):
            with open(PET_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"读取 pet_config 失败: {e}")
    return {}


def _save_pet_config(cfg):
    try:
        os.makedirs(MEMORY_DIR, exist_ok=True)
        with open(PET_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"保存 pet_config 失败: {e}")


class DesktopPet(QWidget):
    # 让 set_thinking 在任意线程都能安全调：自身 emit 给主线程处理。
    # AutoConnection 在同线程是直接调，跨线程会 queue 到 UI 线程事件队列。
    _thinking_signal = Signal(bool)

    def __init__(self, chat_window=None):
        # Qt.SplashScreen 比 Qt.Tool 更"无身份"，DWM 通常不会给它描边/圆角
        super().__init__(
            None,
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.SplashScreen
            | Qt.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAutoFillBackground(False)
        self.setMouseTracking(True)
        self.setToolTip("单击：唤起/隐藏对话\n右键：菜单（含退出）\n拖动：移动位置")
        self.chat_window = chat_window

        QShortcut(QKeySequence("Ctrl+Q"), self, activated=QApplication.quit)

        # ── 动画状态 ──
        # 每个动作存一组 (QPixmap, duration_ms)，由 _load_gifs 从 GIF 预提取填充
        self._frames = {}          # {action_name: [(QPixmap, duration_ms), ...]}
        self._action_loop = {}
        self._action = None
        self._frame_idx = 0
        self._force_once = False
        self._has_advanced = False  # 当前 action 是否已推进过帧（用于 once 检测循环回到起点）
        self._on_done = None
        # 排队的下一动作（action, once, on_done）；当前动作播完本轮再消费
        self._pending_action = None
        self._thinking = False
        self._pixmap = QPixmap()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._advance_frame)

        # 跨线程 set_thinking 投递：worker 线程 emit → 主线程槽实际切动画
        self._thinking_signal.connect(self._apply_thinking)

        # 加载动画
        self._load_gifs()

        # ── 拖动状态 ──
        self._press_pos = None
        self._drag_offset = None
        self._was_dragged = False

        # 启动播放
        if self._frames:
            self.play("idle")
        else:
            # 静态立绘 fallback
            pix = QPixmap(FALLBACK_IMAGE)
            if not pix.isNull():
                self._pixmap = pix.scaledToHeight(PET_MAX_HEIGHT, Qt.SmoothTransformation)
                self.resize(self._pixmap.size())
            else:
                self.resize(QSize(200, PET_MAX_HEIGHT))

        # 恢复位置
        cfg = _load_pet_config()
        if "x" in cfg and "y" in cfg:
            self.move(cfg["x"], cfg["y"])
        else:
            self._move_to_default()

    # ─────────────────────────────
    #  GIF 加载（首选，用 PIL 预提取完整 canvas 帧 → QPixmap 序列）
    # ─────────────────────────────
    def _load_gifs(self):
        missing = [n for n, p in GIF_FILES.items() if not os.path.exists(p)]
        if missing:
            logger.info(f"GIF 缺失: {missing}，将走 sprite 后端")
            return

        try:
            from PIL import Image
        except ImportError:
            logger.warning("PIL 未安装，无法预提取 GIF 帧，将走 sprite 后端")
            return

        from PySide6.QtGui import QImage

        dpr = self.devicePixelRatioF() or 1.0

        for name, path in GIF_FILES.items():
            try:
                pil = Image.open(path)
                n = getattr(pil, "n_frames", 1)
                if n == 0:
                    continue

                # 第一帧定基准尺寸
                pil.seek(0)
                base_rgba = pil.convert("RGBA")
                src_w, src_h = base_rgba.size
                target_physical_h = min(int(PET_MAX_HEIGHT * dpr), src_h)
                target_physical_w = max(1, src_w * target_physical_h // src_h)

                # disposal=2：PIL 会自动合成全 canvas，直接 convert RGBA 拿完整帧
                # 不指望 PIL 的内置合成器，这里手工管理一个全 canvas 缓冲
                canvas = Image.new("RGBA", (src_w, src_h), (0, 0, 0, 0))

                action_frames = []
                for i in range(n):
                    pil.seek(i)
                    # disposal=2 → 每帧绘制前把上一帧的 bbox 清掉；
                    # 我们这里直接每帧重置 canvas 然后贴新帧（等价 disposal=2 的视觉结果）
                    canvas = Image.new("RGBA", (src_w, src_h), (0, 0, 0, 0))
                    frame_rgba = pil.convert("RGBA")
                    canvas.alpha_composite(frame_rgba)
                    # 下采样到目标物理尺寸
                    if (target_physical_w, target_physical_h) != (src_w, src_h):
                        scaled = canvas.resize(
                            (target_physical_w, target_physical_h),
                            Image.LANCZOS,
                        )
                    else:
                        scaled = canvas
                    # PIL → QImage → QPixmap
                    data = scaled.tobytes("raw", "RGBA")
                    qimg = QImage(data, scaled.width, scaled.height,
                                  scaled.width * 4, QImage.Format.Format_RGBA8888)
                    pix = QPixmap.fromImage(qimg.copy())  # copy 让 buffer 独立
                    pix.setDevicePixelRatio(dpr)
                    raw_duration = int(pil.info.get("duration", 120)) or 120
                    # PET_ANIMATION_SPEED: 1.0=原速；<1=慢；>1=快
                    speed = PET_ANIMATION_SPEED if PET_ANIMATION_SPEED > 0 else 1.0
                    duration = max(20, int(raw_duration / speed))
                    action_frames.append((pix, duration))

                if action_frames:
                    # 复用 sprite 后端的播放机制（统一 _render_current_frame / _advance_frame）
                    self._frames[name] = action_frames
                    self._action_loop[name] = True
                    logger.info(
                        f"GIF [{name}]: {n} 帧，{target_physical_w}x{target_physical_h} 物理, dpr={dpr}"
                    )
            except Exception as e:
                logger.warning(f"GIF 加载失败 [{name}]: {e}", exc_info=True)

        # 用 idle 第一帧定 widget 尺寸（逻辑像素）
        if "idle" in self._frames:
            pix0 = self._frames["idle"][0][0]
            size = pix0.deviceIndependentSize()
            self.resize(int(size.width()), int(size.height()))

    def play(self, action, once=False, on_done=None):
        """切换并开始播放某个动作。once=True 则强制只播一遍后回 idle。

        排队语义：当前有动画在播时，新请求会**等当前这一轮播完**才切。这样
        即梦的 5 秒小动作不会被半路打断显得"抽搐"。最后到达的请求覆盖之前
        pending（last-write-wins），适合 set_thinking(True/False) 这种状态信号。
        """
        if action not in self._frames:
            logger.debug(f"动作不存在: {action}")
            return

        # 当前没动画在跑 → 立即启动
        if self._action is None or not self._timer.isActive():
            self._start_action(action, once, on_done)
            return

        # 当前就是要切到的动作、且没有未决切换 → 啥也不做
        if (action == self._action and not once and not self._force_once
                and self._pending_action is None):
            return

        # 否则排队，_advance_frame 在本轮末尾消费它
        self._pending_action = (action, once, on_done)

    def _start_action(self, action, once, on_done):
        """直接启动动作（不走排队）。仅在 play() 的"当前空闲"分支和
        _advance_frame 消费 pending 时使用。"""
        self._action = action
        self._frame_idx = 0
        self._force_once = once
        self._on_done = on_done
        self._timer.stop()
        self._render_current_frame()

    def _render_current_frame(self):
        frames = self._frames.get(self._action)
        if not frames:
            return
        pix, duration = frames[self._frame_idx]
        self._pixmap = pix
        self.update()
        self._timer.start(duration)

    def _advance_frame(self):
        frames = self._frames.get(self._action)
        if not frames:
            return
        nxt = self._frame_idx + 1
        if nxt >= len(frames):
            # 本轮播完——先消费 pending（如有），否则按 loop / once 默认处理
            if self._pending_action is not None:
                pending = self._pending_action
                self._pending_action = None
                # 当前是 once 动作：先回 once 完成回调（让外部知道动画跑完）
                if self._force_once:
                    cb = self._on_done
                    self._on_done = None
                    self._force_once = False
                    if cb:
                        cb()
                action, once, on_done = pending
                self._start_action(action, once, on_done)
                return

            loop = self._action_loop.get(self._action, True) and not self._force_once
            if loop:
                nxt = 0
            else:
                cb = self._on_done
                self._on_done = None
                self._force_once = False
                if cb:
                    cb()
                # 默认回到 idle（如果当前不是 idle）
                if self._action != "idle":
                    self._start_action("idle", False, None)
                return
        self._frame_idx = nxt
        self._render_current_frame()

    # 外部 API：让 agent_loop 调用以切换 think 动画。线程安全。
    def set_thinking(self, on: bool):
        # 直接 emit signal——同线程时 Qt 会 DirectConnection 立即调用 _apply_thinking，
        # 跨线程时会 QueuedConnection 投递到 pet 所在的 UI 线程。worker 线程绝不
        # 能直接动 QTimer / widget.update()，否则 timer 会失活、动画卡死。
        self._thinking_signal.emit(on)

    def _apply_thinking(self, on: bool):
        if on == self._thinking:
            return
        self._thinking = on
        if on and "think" in self._frames:
            self.play("think")
        elif not on:
            self.play("idle")

    # ─────────────────────────────
    #  渲染
    # ─────────────────────────────
    def paintEvent(self, event):
        p = QPainter(self)
        # SOURCE 模式冲掉缓冲区残留（解决 Windows 11 脏像素问题）
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.transparent)
        if getattr(self, "_pixmap", None) and not self._pixmap.isNull():
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            # 用 deviceIndependentSize 拿逻辑尺寸，避免高 DPI 上 physical/logical 混用
            size = self._pixmap.deviceIndependentSize()
            x = int((self.width() - size.width()) / 2)
            y = int(self.height() - size.height())  # bottom-center
            p.drawPixmap(x, y, self._pixmap)

    def showEvent(self, event):
        super().showEvent(event)
        if sys.platform == "win32":
            self._kill_dwm_decorations()

    def _kill_dwm_decorations(self):
        """关掉 Windows 11 DWM 给本窗口加的描边/圆角/阴影。"""
        try:
            import ctypes
            hwnd = int(self.winId())
            dwmapi = ctypes.windll.dwmapi

            # 1) 关掉非客户区渲染（去描边/阴影）
            DWMWA_NCRENDERING_POLICY = 2
            DWMNCRP_DISABLED = 1
            policy = ctypes.c_int(DWMNCRP_DISABLED)
            dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_NCRENDERING_POLICY,
                ctypes.byref(policy), ctypes.sizeof(policy),
            )

            # 2) Windows 11：强制方角（去 DWM 圆角）
            DWMWA_WINDOW_CORNER_PREFERENCE = 33
            DWMWCP_DONOTROUND = 1
            corner = ctypes.c_int(DWMWCP_DONOTROUND)
            dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(corner), ctypes.sizeof(corner),
            )

            # 3) Windows 11：把边框颜色设成完全透明
            DWMWA_BORDER_COLOR = 34
            DWMWA_COLOR_NONE = 0xFFFFFFFE
            color = ctypes.c_uint(DWMWA_COLOR_NONE)
            dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_BORDER_COLOR,
                ctypes.byref(color), ctypes.sizeof(color),
            )
        except Exception as e:
            logger.debug(f"DWM 装饰移除失败（不影响功能）: {e}")

    # ─────────────────────────────
    #  位置 / 鼠标事件
    # ─────────────────────────────
    def attach_chat_window(self, win):
        self.chat_window = win

    def _move_to_default(self):
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(
            screen.right() - self.width() - 40,
            screen.bottom() - self.height() - 40,
        )

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press_pos = e.globalPosition().toPoint()
            self._drag_offset = self._press_pos - self.frameGeometry().topLeft()
            self._was_dragged = False
            e.accept()
        elif e.button() == Qt.RightButton:
            self._show_menu(e.globalPosition().toPoint())
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag_offset is None or not (e.buttons() & Qt.LeftButton):
            return
        cur = e.globalPosition().toPoint()
        if not self._was_dragged:
            if (cur - self._press_pos).manhattanLength() < DRAG_THRESHOLD:
                return
            self._was_dragged = True
        self.move(cur - self._drag_offset)

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        if not self._was_dragged:
            self._toggle_chat()
            # 单击 → 挥手打个招呼，再回 idle（若正在 thinking 则跳过避免视觉冲突）
            if not self._thinking and "wave" in self._frames:
                self.play("wave", once=True)
        else:
            _save_pet_config({"x": self.x(), "y": self.y()})
        self._press_pos = None
        self._drag_offset = None
        self._was_dragged = False

    def _toggle_chat(self):
        win = self.chat_window
        if win is None:
            return
        if win.isVisible() and not win.isMinimized() and win.isActiveWindow():
            win.hide()
        else:
            _restore_window(win)
            win.raise_()
            win.activateWindow()

    def _show_menu(self, pos):
        menu = QMenu(self)
        a_toggle = QAction("显示/隐藏对话", menu)
        a_toggle.triggered.connect(self._toggle_chat)
        menu.addAction(a_toggle)

        a_wave = QAction("挥手", menu)
        a_wave.triggered.connect(lambda: self.play("wave", once=True))
        menu.addAction(a_wave)

        a_reset = QAction("重置位置", menu)
        a_reset.triggered.connect(self._reset_position)
        menu.addAction(a_reset)

        menu.addSeparator()

        # 隐藏桌宠（之后从右下角托盘图标 → "显示/隐藏桌宠" 可恢复）
        a_hide = QAction("隐藏桌宠", menu)
        a_hide.triggered.connect(self.hide)
        menu.addAction(a_hide)

        a_exit = QAction("退出", menu)
        a_exit.triggered.connect(QApplication.quit)
        menu.addAction(a_exit)
        menu.exec(pos)

    def _reset_position(self):
        self._move_to_default()
        _save_pet_config({"x": self.x(), "y": self.y()})


def create_tray(app, pet, chat_window, icon_path=None):
    """创建系统托盘图标，主聊天窗口关闭后由托盘维持后台。"""
    tray = QSystemTrayIcon(app)
    if icon_path and os.path.exists(icon_path):
        tray.setIcon(QIcon(icon_path))
    elif os.path.exists(FALLBACK_IMAGE):
        tray.setIcon(QIcon(FALLBACK_IMAGE))
    else:
        tray.setIcon(app.style().standardIcon(app.style().StandardPixmap.SP_ComputerIcon))
    tray.setToolTip("灵犀 AI 助手（双击唤起对话，右键退出）")

    def _show_chat():
        _restore_window(chat_window)
        chat_window.raise_()
        chat_window.activateWindow()

    def _toggle_pet():
        pet.setVisible(not pet.isVisible())

    # parent=chat_window 让 menu 生命周期跟主窗口绑定，否则函数返回后 menu 被 GC，托盘右键就没反应
    menu = QMenu(chat_window)

    a_chat = QAction("打开对话", menu)
    a_chat.triggered.connect(_show_chat)
    menu.addAction(a_chat)

    a_pet = QAction("显示/隐藏桌宠", menu)
    a_pet.triggered.connect(_toggle_pet)
    menu.addAction(a_pet)

    menu.addSeparator()

    a_exit = QAction("退出", menu)
    a_exit.triggered.connect(app.quit)
    menu.addAction(a_exit)

    tray.setContextMenu(menu)
    tray._menu = menu  # 双保险

    def _on_activated(reason):
        if reason == QSystemTrayIcon.Trigger:
            _show_chat()
    tray.activated.connect(_on_activated)

    tray.show()
    return tray
