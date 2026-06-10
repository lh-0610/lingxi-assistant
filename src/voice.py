"""语音识别 (STT) + 文字转语音 (TTS)。

- STT: faster-whisper 本地（GPU 优先，CPU fallback）。首次使用懒加载模型。
- TTS: GPT-SoVITS 本地 HTTP API（端口 9880），生成 WAV 后用 QMediaPlayer 播放。

线程模型：
- 录音：`sounddevice.InputStream` 后台线程持续 push chunk 到 list
- 识别：单独 worker 线程跑，结束发 Qt signal 回 UI
- TTS：单独 worker 线程发 HTTP 请求，完成后通过 Signal 切回主线程播放
"""
from __future__ import annotations

import os
import tempfile
import threading

import numpy as np
import sounddevice as sd

from PySide6.QtCore import QObject, Signal

from .paths import logger


# 默认配置
DEFAULT_WHISPER_MODEL = "small"        # tiny / base / small / medium / large-v3
DEFAULT_WHISPER_LANG = "zh"

SAMPLE_RATE = 16000  # whisper 要求 16kHz mono float32


# ═══════════════════════════════════════════════════════
# 录音
# ═══════════════════════════════════════════════════════

class Recorder:
    """非阻塞录音：start() 开始抓取，stop() 返回拼好的单声道 float32 numpy 数组。"""

    def __init__(self):
        self._stream: sd.InputStream | None = None
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()

    @property
    def is_recording(self) -> bool:
        return self._stream is not None

    def start(self) -> bool:
        if self._stream is not None:
            return False
        self._chunks = []

        def callback(indata, frames, time_info, status):
            if status:
                logger.warning(f"录音回调状态: {status}")
            with self._lock:
                self._chunks.append(indata.copy())

        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                callback=callback,
            )
            self._stream.start()
            logger.info("开始录音")
            return True
        except Exception as e:
            logger.error(f"录音启动失败: {e}")
            self._stream = None
            return False

    def stop(self) -> np.ndarray | None:
        if self._stream is None:
            return None
        try:
            self._stream.stop()
            self._stream.close()
        except Exception as e:
            logger.warning(f"关闭录音流出错: {e}")
        finally:
            self._stream = None

        with self._lock:
            if not self._chunks:
                return None
            audio = np.concatenate(self._chunks).flatten()
        logger.info(f"结束录音，{len(audio)/SAMPLE_RATE:.1f}s")
        return audio


# ═══════════════════════════════════════════════════════
# STT
# ═══════════════════════════════════════════════════════

class STT(QObject):
    """faster-whisper 包装。模型懒加载。识别在 worker 线程跑，结果通过信号回 UI。"""

    transcribed = Signal(str)         # 识别成功 → 文本
    failed = Signal(str)              # 识别失败 → 错误信息

    def __init__(self, model_size: str = DEFAULT_WHISPER_MODEL, language: str = DEFAULT_WHISPER_LANG):
        super().__init__()
        self.model_size = model_size
        self.language = language
        self._model = None
        self._load_lock = threading.Lock()

    def _ensure_model(self):
        if self._model is not None:
            return
        from faster_whisper import WhisperModel

        # 探测 CUDA 是否真的可用（不仅看显卡，还要看 cublas/cudnn DLL 能否加载）
        gpu_ok = False
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:
                gpu_ok = True
        except Exception as e:
            logger.warning(f"ctranslate2 CUDA 探测失败: {e}")

        if gpu_ok:
            try:
                self._model = WhisperModel(self.model_size, device="cuda", compute_type="float16")
                # 做一次极小的 dry-run，确认 cublas/cudnn DLL 真能加载（init 不报错不等于 transcribe 不报错）
                import numpy as _np
                _silence = _np.zeros(1600, dtype=_np.float32)
                list(self._model.transcribe(_silence, language=self.language, vad_filter=False)[0])
                logger.info(f"Whisper {self.model_size} 已加载 (GPU/fp16)")
                return
            except Exception as e:
                logger.warning(f"GPU 加载/试运行失败，降级到 CPU: {e}")
                self._model = None  # 清掉 GPU 实例，下面重新建 CPU

        self._model = WhisperModel(self.model_size, device="cpu", compute_type="int8")
        logger.info(f"Whisper {self.model_size} 已加载 (CPU/int8)")

    def transcribe_async(self, audio: np.ndarray):
        """非阻塞调用：在后台线程识别，完成后发 transcribed/failed 信号。"""
        def _work():
            try:
                with self._load_lock:
                    self._ensure_model()
                segments, _info = self._model.transcribe(
                    audio,
                    language=self.language,
                    beam_size=5,
                    vad_filter=True,  # 过滤静音段（避免幻觉）
                )
                text = "".join(s.text for s in segments).strip()
                if text:
                    self.transcribed.emit(text)
                else:
                    self.failed.emit("没识别到内容")
            except Exception as e:
                logger.error(f"STT 异常: {e}", exc_info=True)
                self.failed.emit(str(e))

        threading.Thread(target=_work, daemon=True).start()


# ═══════════════════════════════════════════════════════
# GPT-SoVITS TTS（HTTP 调本地推理服务）
# ═══════════════════════════════════════════════════════

class GPTSoVITSTTS(QObject):
    """通过本地 GPT-SoVITS API 合成音频，sounddevice 直接播放。

    选 sounddevice 而非 QMediaPlayer：QMediaPlayer 首次实例化要触发 Qt FFmpeg
    后端初始化，Windows 上要 25s+，期间 UI 卡死。sounddevice 是 PortAudio 包装，
    几毫秒就能开播，且我们已经在用它做麦克风录音，无需新增依赖。
    """

    started = Signal()
    finished = Signal()
    failed = Signal(str)

    def __init__(self,
                 url: str,
                 ref_audio: str,
                 prompt_text: str,
                 prompt_lang: str = "zh",
                 text_lang: str = "zh",
                 media_type: str = "wav",
                 text_split_method: str = "cut5"):
        super().__init__()
        self.url = url.rstrip("/")
        self.ref_audio = ref_audio
        self.prompt_text = prompt_text
        self.prompt_lang = prompt_lang
        self.text_lang = text_lang
        self.media_type = media_type
        self.text_split_method = text_split_method

        self._stop_requested = False

    # GPT-SoVITS V2 输出采样率
    STREAM_SAMPLE_RATE = 32000

    def speak(self, text: str):
        text = (text or "").strip()
        if not text:
            return
        if not self.ref_audio or not self.prompt_text:
            logger.error("GPT-SoVITS 未配置 ref_audio / prompt_text，请在 config.json 填好")
            self.failed.emit("GPT-SoVITS 缺少参考音频配置")
            return
        logger.info(f"GPT-SoVITS TTS 开始（流式）: text_len={len(text)}, url={self.url}")
        self._stop_requested = False

        def _bg():
            try:
                import requests
                params = {
                    "text": text,
                    "text_lang": self.text_lang,
                    "ref_audio_path": self.ref_audio,
                    "prompt_text": self.prompt_text,
                    "prompt_lang": self.prompt_lang,
                    "text_split_method": self.text_split_method,
                    "media_type": "raw",       # raw PCM int16 mono，sounddevice 直接吞
                    "streaming_mode": "true",  # 流式，逐句合成逐句返回
                }
                resp = requests.get(f"{self.url}/tts", params=params, timeout=300, stream=True)
                if resp.status_code != 200:
                    body = resp.text[:300] if hasattr(resp, "text") else ""
                    logger.error(f"GPT-SoVITS API 错误 {resp.status_code}: {body}")
                    self.failed.emit(f"HTTP {resp.status_code}")
                    return

                # 边收边喂给 sounddevice OutputStream
                self._stream_pcm(resp)
            except Exception as e:
                logger.error(f"GPT-SoVITS 调用失败: {e}", exc_info=True)
                self.failed.emit(str(e))

        threading.Thread(target=_bg, daemon=True).start()

    def _stream_pcm(self, resp):
        """逐 chunk 把 raw PCM 喂给 sounddevice，实现"边合成边播放"。"""
        sd.stop()  # 万一上一次还在响
        stream = sd.OutputStream(
            samplerate=self.STREAM_SAMPLE_RATE,
            channels=1,
            dtype="int16",
        )
        stream.start()
        first_chunk = True
        total_bytes = 0
        leftover = b""   # int16 需偶数字节；chunk 边界落单的尾字节缓冲到下一块，否则 frombuffer 崩
        try:
            for chunk in resp.iter_content(chunk_size=4096):
                if self._stop_requested:
                    break
                if not chunk:
                    continue
                if first_chunk:
                    self.started.emit()
                    logger.info("GPT-SoVITS 开始流式播放")
                    first_chunk = False
                data = leftover + chunk
                if len(data) % 2:
                    leftover = data[-1:]
                    data = data[:-1]
                else:
                    leftover = b""
                if not data:
                    continue
                samples = np.frombuffer(data, dtype=np.int16)
                stream.write(samples)
                total_bytes += len(chunk)
            if not self._stop_requested:
                logger.info(f"GPT-SoVITS 流式播放结束（{total_bytes} 字节）")
                self.finished.emit()
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
            try:
                resp.close()
            except Exception:
                pass

    def stop(self):
        """中断当前播放（如果有）。"""
        self._stop_requested = True
        try:
            sd.stop()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════
# 工厂
# ═══════════════════════════════════════════════════════

def make_tts():
    """根据 config.json 创建 GPT-SoVITS TTS 实例。"""
    from .config import (
        GPT_SOVITS_URL, GPT_SOVITS_REF_AUDIO,
        GPT_SOVITS_PROMPT_TEXT, GPT_SOVITS_PROMPT_LANG,
        GPT_SOVITS_TEXT_LANG, GPT_SOVITS_MEDIA_TYPE,
        GPT_SOVITS_TEXT_SPLIT_METHOD,
    )
    logger.info(f"使用 TTS: GPT-SoVITS ({GPT_SOVITS_URL})")
    return GPTSoVITSTTS(
        url=GPT_SOVITS_URL,
        ref_audio=GPT_SOVITS_REF_AUDIO,
        prompt_text=GPT_SOVITS_PROMPT_TEXT,
        prompt_lang=GPT_SOVITS_PROMPT_LANG,
        text_lang=GPT_SOVITS_TEXT_LANG,
        media_type=GPT_SOVITS_MEDIA_TYPE,
        text_split_method=GPT_SOVITS_TEXT_SPLIT_METHOD,
    )
