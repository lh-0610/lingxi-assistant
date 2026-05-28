"""GPT-SoVITS 启动器：从主程序里 subprocess 拉起 api_v2.py + 自动切换权重。

行为流程：
  start() 后台线程:
    1. spawn `<install_dir>/runtime/python.exe <install_dir>/api_v2.py -a 127.0.0.1 -p 9880`
    2. 轮询 http://127.0.0.1:9880/ 直到返回（最多等 90s）
    3. POST /set_gpt_weights + /set_sovits_weights 切到配置的 GPT/SoVITS 权重
    4. emit started 信号

状态机：
  STOPPED → STARTING → RUNNING（绿）
                   ↓
              STOP_FAILED（红）
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

from PySide6.QtCore import QObject, Signal

from .paths import logger


READY_TIMEOUT_S = 90      # 最多等多少秒 API 起来
POLL_INTERVAL_S = 1.5


# ─────────────────────────────────────────────────────────────
# Windows Job Object：父进程死亡（含 crash / 强杀）时自动 kill 子进程
# ─────────────────────────────────────────────────────────────
def _create_kill_on_close_job():
    """创建一个绑 KILL_ON_JOB_CLOSE 的 Job Object，返回 HANDLE 或 None。"""
    import ctypes
    from ctypes import wintypes

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_void_p),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimit),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectExtendedLimitInformation = 9

    k = ctypes.windll.kernel32
    job = k.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _ExtendedLimit()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = k.SetInformationJobObject(
        job, JobObjectExtendedLimitInformation,
        ctypes.byref(info), ctypes.sizeof(info),
    )
    if not ok:
        k.CloseHandle(job)
        return None
    return job


def _attach_pid_to_job(job_handle, pid: int) -> bool:
    """把 PID 加入到 Job Object 里。"""
    import ctypes
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001
    k = ctypes.windll.kernel32
    h = k.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
    if not h:
        return False
    try:
        return bool(k.AssignProcessToJobObject(job_handle, h))
    finally:
        k.CloseHandle(h)


def _close_job(job_handle):
    """关掉 Job Object 句柄。KILL_ON_JOB_CLOSE 会让 OS 立即终止所有组内进程。"""
    import ctypes
    try:
        ctypes.windll.kernel32.CloseHandle(job_handle)
    except Exception:
        pass


class GPTSoVITSLauncher(QObject):
    """状态变化通过 status_changed 信号通知 UI。"""

    status_changed = Signal(str, str)   # (state, message)  state ∈ {"stopped","starting","running","failed"}
    output_line = Signal(str)           # 子进程输出（可用于日志面板，可选）

    def __init__(self, url: str = "http://127.0.0.1:9880"):
        super().__init__()
        self.url = url.rstrip("/")
        self._proc: subprocess.Popen | None = None
        self._state = "stopped"
        self._stop_polling = threading.Event()
        # Windows Job Object，让父进程崩了/被强杀也能自动清理子进程
        self._job_handle = None

    # ────────────────────────────────────────
    #  公开属性
    # ────────────────────────────────────────
    @property
    def state(self) -> str:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state == "running"

    def _set_state(self, state: str, msg: str = ""):
        self._state = state
        logger.info(f"[GPT-SoVITS launcher] {state}: {msg}")
        self.status_changed.emit(state, msg)

    # ────────────────────────────────────────
    #  外部调用
    # ────────────────────────────────────────
    def start(self, install_dir: str, gpt_model: str, sovits_model: str):
        """非阻塞：返回后还在后台启动中。通过 status_changed 信号关注进展。"""
        if self._state in ("starting", "running"):
            logger.info("启动器已在运行中，忽略 start()")
            return

        if not install_dir or not os.path.isdir(install_dir):
            self._set_state("failed", f"安装目录无效: {install_dir}")
            return

        python_exe = os.path.join(install_dir, "runtime", "python.exe")
        api_script = os.path.join(install_dir, "api_v2.py")
        if not os.path.exists(python_exe):
            self._set_state("failed", f"找不到 runtime/python.exe: {python_exe}")
            return
        if not os.path.exists(api_script):
            self._set_state("failed", f"找不到 api_v2.py: {api_script}")
            return

        self._set_state("starting", "拉起 api_v2.py...")
        threading.Thread(
            target=self._start_worker,
            args=(install_dir, python_exe, api_script, gpt_model, sovits_model),
            daemon=True,
        ).start()

    def stop(self):
        """同步终止子进程。
        优先关 Job Object 句柄（OS 会立刻 kill 组内进程）；句柄关失败再走 subprocess API 兜底。
        """
        self._stop_polling.set()
        proc = self._proc

        # 第一招：关 Job Object 句柄 → OS 自动 kill 子进程
        if self._job_handle is not None:
            try:
                _close_job(self._job_handle)
            except Exception:
                pass
            self._job_handle = None

        # 第二招：subprocess API 兜底
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            except Exception as e:
                logger.warning(f"停止 GPT-SoVITS 失败: {e}")

        self._proc = None
        self._set_state("stopped", "")

    # ────────────────────────────────────────
    #  内部：spawn + poll + switch weights
    # ────────────────────────────────────────
    def _start_worker(self, install_dir, python_exe, api_script, gpt_model, sovits_model):
        try:
            self._stop_polling.clear()
            # spawn 子进程；-u 强制 Python 不缓冲 stdout，便于实时看进度
            cmd = [python_exe, "-u", api_script, "-a", "127.0.0.1", "-p", "9880"]
            logger.info(f"[GPT-SoVITS launcher] spawn: {cmd}")

            # Windows 上用 CREATE_NEW_PROCESS_GROUP，方便单独 kill
            kwargs = {
                "cwd": install_dir,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "bufsize": 1,
            }
            if sys.platform == "win32":
                # 不弹黑窗口
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            self._proc = subprocess.Popen(cmd, **kwargs)

            # Windows：把子进程挂进 Job Object（父进程死了 OS 会自动杀子进程）
            if sys.platform == "win32":
                try:
                    if self._job_handle is None:
                        self._job_handle = _create_kill_on_close_job()
                    if self._job_handle:
                        if _attach_pid_to_job(self._job_handle, self._proc.pid):
                            logger.info("[GPT-SoVITS launcher] 已加入 Job Object（kill-on-close）")
                        else:
                            logger.warning("AssignProcessToJobObject 失败（不影响功能，仅 crash 时可能残留）")
                    else:
                        logger.warning("CreateJobObject 失败（不影响功能，仅 crash 时可能残留）")
                except Exception as e:
                    logger.warning(f"Job Object 集成异常: {e}")

            # 读输出线程
            threading.Thread(target=self._reader, args=(self._proc,), daemon=True).start()

            # 轮询健康检查 + 每秒更新状态显示
            start_time = time.time()
            deadline = start_time + READY_TIMEOUT_S
            ready = False
            last_status_msg = ""
            while time.time() < deadline:
                if self._stop_polling.is_set():
                    return
                if self._proc.poll() is not None:
                    self._set_state("failed", f"api_v2.py 进程意外退出 (code={self._proc.returncode})")
                    return
                if self._ping():
                    ready = True
                    break
                elapsed = int(time.time() - start_time)
                msg = f"加载模型中... ({elapsed}s)"
                if msg != last_status_msg:
                    self._set_state("starting", msg)
                    last_status_msg = msg
                time.sleep(POLL_INTERVAL_S)
            if not ready:
                self._set_state("failed", f"等待 {READY_TIMEOUT_S}s 后 API 仍未响应")
                return

            # 切换权重（可选）
            if gpt_model:
                ok, msg = self._set_weights("set_gpt_weights", gpt_model)
                if not ok:
                    self._set_state("failed", f"加载 GPT 权重失败: {msg}")
                    return
            if sovits_model:
                ok, msg = self._set_weights("set_sovits_weights", sovits_model)
                if not ok:
                    self._set_state("failed", f"加载 SoVITS 权重失败: {msg}")
                    return

            self._set_state("running", "API 就绪，权重已加载")
        except Exception as e:
            logger.error(f"启动 GPT-SoVITS 异常: {e}", exc_info=True)
            self._set_state("failed", str(e))

    def _reader(self, proc):
        """收集子进程输出，发到 output_line + 也写到主程序日志，方便排错。"""
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if not line:
                    break
                line = line.rstrip()
                logger.info(f"[GPT-SoVITS] {line}")  # 升级到 INFO，确保用户看得见
                self.output_line.emit(line)
        except Exception as e:
            logger.warning(f"读取 GPT-SoVITS 输出异常: {e}")

    def _ping(self) -> bool:
        try:
            with urllib.request.urlopen(self.url + "/", timeout=1.5) as r:
                return True
        except urllib.error.HTTPError:
            # 4xx/5xx 也算存活：服务器有响应，只是 / 路径未注册
            return True
        except Exception:
            return False

    def _set_weights(self, endpoint: str, weights_path: str) -> tuple[bool, str]:
        url = f"{self.url}/{endpoint}?weights_path={urllib.parse.quote(weights_path)}"
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                body = r.read().decode("utf-8", errors="replace")
                if r.status == 200:
                    return True, body
                return False, f"HTTP {r.status}: {body}"
        except Exception as e:
            return False, str(e)
