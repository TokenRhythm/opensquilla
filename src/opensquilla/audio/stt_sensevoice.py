"""Local SenseVoice speech-to-text through the sherpa-onnx Python API.

The browser microphone records ``audio/webm`` (Opus). Sherpa consumes raw
16-bit PCM at a fixed rate, so the payload is decoded to WAV with ffmpeg when
available and otherwise passed through when the browser or tool already
produced a WAV. Model files follow the AgentRMC layout: a directory holding
``model.int8.onnx`` (or ``model.onnx``) plus ``tokens.txt``.

Nothing here reaches the network: the point of this provider is free local
testing, so a missing dependency or model fails with an actionable message
instead of silently falling back to a metered cloud provider.
"""

from __future__ import annotations

import asyncio
import io
import math
import re
import shutil
import subprocess
import wave
from array import array as _array
from collections import OrderedDict
from functools import partial
from pathlib import Path

from opensquilla.audio.stt_types import SttAudio, SttProviderError, SttResult

_TARGET_SAMPLE_RATE = 16000
_FEATURE_DIM = 80
_MAX_THREADS = 8
# Silence guard: digital silence feeds the model a DC-free flat signal, and
# SenseVoice answers with one short hallucination token ("嗯", "我", ...). The
# frontend already gates on live levels; this catches silence that slipped
# through the gate (very quiet rooms). The rule is narrow on purpose: drop
# short output ONLY when the signal carried no voiced frames at all. A real
# word spoken quietly keeps its voiced energy and is preserved.
_MAX_SHORT_OUTPUT_CHARS = 2
_VOICED_RMS_THRESHOLD = 0.0025
_VOICED_PEAK_THRESHOLD = 0.008
# SenseVoice bakes its language token into the recognizer, so per-request
# language hints need one cached recognizer per language. LRU-capped because
# each graph holds the model resident.
_LANG_CACHE_LIMIT = 3
_LANG_TAGS = {
    "zh": "zh",
    "en": "en",
    "ja": "ja",
    "ko": "ko",
    "yue": "yue",
    "auto": "auto",
}
# CJK output often arrives punctuation-only in shape ("，。" plus hanzi), and
# \w alone would drop it; the keep-test adds the script ranges explicitly.
_WORD_RE = re.compile(r"[\w぀-ヿ㐀-䶿一-鿿가-힯]")


class SenseVoiceSttProvider:
    """Offline transcription with a lazily loaded sherpa-onnx recognizer."""

    provider_id = "sensevoice"
    accepts_request_model = False

    def __init__(
        self,
        *,
        model_dir: str = "",
        language: str = "auto",
        use_itn: bool = True,
        num_threads: int = 4,
    ) -> None:
        self._model_dir = Path(str(model_dir or "").strip() or ".").expanduser()
        self._language = str(language or "auto").strip().lower() or "auto"
        self._use_itn = bool(use_itn)
        self._num_threads = max(1, min(_MAX_THREADS, int(num_threads or 4)))
        self._recognizers: OrderedDict[str, object] = OrderedDict()
        self._load_lock = asyncio.Lock()

    # -- model / dependency discovery -------------------------------------

    def _model_files(self) -> tuple[Path, Path]:
        model = next(
            (
                self._model_dir / name
                for name in ("model.int8.onnx", "model.onnx")
                if (self._model_dir / name).is_file()
            ),
            None,
        )
        tokens = self._model_dir / "tokens.txt"
        if model is None or not tokens.is_file():
            raise SttProviderError(
                "local sensevoice model not found: expected model.int8.onnx"
                f" (or model.onnx) and tokens.txt in {self._model_dir}; set"
                " [audio.stt.sensevoice].model_dir"
            )
        return model, tokens

    @staticmethod
    def _import_sherpa():
        try:
            import sherpa_onnx  # type: ignore[import-untyped]
        except ImportError as exc:
            raise SttProviderError(
                "local speech-to-text requires the sherpa-onnx package"
                " (pip install 'opensquilla[voice]' or pip install sherpa-onnx)"
            ) from exc
        return sherpa_onnx

    def _resolve_language(self, requested: str | None) -> str:
        for candidate in (requested, self._language):
            tag = _LANG_TAGS.get(str(candidate or "").strip().lower())
            if tag:
                return tag
        return "auto"

    async def _get_recognizer(self, language: str | None = None):
        lang_tag = self._resolve_language(language)
        cached = self._recognizers.get(lang_tag)
        if cached is not None:
            self._recognizers.move_to_end(lang_tag)
            return cached
        async with self._load_lock:
            cached = self._recognizers.get(lang_tag)
            if cached is not None:
                self._recognizers.move_to_end(lang_tag)
                return cached
            model, tokens = self._model_files()
            sherpa_onnx = self._import_sherpa()
            # Recognizer construction loads a ~230MB ONNX graph; keep it off
            # the event loop so a first recording cannot stall gateway WS.
            recognizer = await asyncio.to_thread(
                partial(
                    sherpa_onnx.OfflineRecognizer.from_sense_voice,
                    model=str(model),
                    tokens=str(tokens),
                    num_threads=self._num_threads,
                    sample_rate=_TARGET_SAMPLE_RATE,
                    feature_dim=_FEATURE_DIM,
                    language=lang_tag,
                    use_itn=self._use_itn,
                )
            )
            self._recognizers[lang_tag] = recognizer
            while len(self._recognizers) > _LANG_CACHE_LIMIT:
                self._recognizers.popitem(last=False)
            return recognizer

    # -- audio decoding ----------------------------------------------------

    @staticmethod
    def _decode_wav(data: bytes) -> tuple[int, list[float]]:
        with wave.open(io.BytesIO(data), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                raise SttProviderError(
                    "local sensevoice expects mono 16-bit PCM"
                    " (the browser webm could not be decoded without ffmpeg)"
                )
            rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
        samples = _array("h", frames)
        return rate, [value / 32768.0 for value in samples]

    def _decode_with_ffmpeg(self, data: bytes) -> tuple[int, list[float]]:
        ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
        if not ffmpeg:
            raise SttProviderError(
                "cannot decode audio/webm locally: ffmpeg not found on PATH."
                " Install ffmpeg (winget install Gyan.FFmpeg) or record in a"
                " browser that emits wav/opus the provider understands"
            )
        proc = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-i", "pipe:0",
                "-f", "wav", "-acodec", "pcm_s16le",
                "-ac", "1", "-ar", str(_TARGET_SAMPLE_RATE),
                "pipe:1",
            ],
            input=data,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout:
            detail = proc.stderr.decode("utf-8", "replace").strip()[:400]
            raise SttProviderError(f"ffmpeg failed to decode recording: {detail}")
        return self._decode_wav(proc.stdout)

    def _to_pcm(self, audio: SttAudio) -> tuple[int, list[float]]:
        mime = (audio.mime_type or "").split(";", 1)[0].strip().lower()
        data = audio.data
        if mime.startswith("audio/wav") or mime.startswith("audio/x-wav") or data[:4] == b"RIFF":
            rate, samples = self._decode_wav(data)
        else:
            rate, samples = self._decode_with_ffmpeg(data)
        return rate, samples

    @staticmethod
    def _resample(rate: int, samples: list[float]) -> list[float]:
        if rate == _TARGET_SAMPLE_RATE or not samples:
            return samples
        ratio = _TARGET_SAMPLE_RATE / rate
        out_len = max(1, int(len(samples) * ratio))
        step = 1.0 / ratio
        out: list[float] = []
        for i in range(out_len):
            pos = i * step
            left = min(int(pos), len(samples) - 1)
            right = min(left + 1, len(samples) - 1)
            frac = pos - left
            out.append(samples[left] * (1 - frac) + samples[right] * frac)
        return out

    # -- provider contract --------------------------------------------------

    @staticmethod
    def _voiced(samples: list[float]) -> bool:
        if not samples:
            return False
        # Voiced speech has a syllabic envelope (2–8 Hz modulations) that hum
        # (steady sine) and digital silence (flat zero) lack. Frame RMS alone
        # cannot tell a 30Hz rumble from quiet speech, so count frames whose
        # energy *rises* relative to the smoothed history; hallucinations come
        # from signals with no such variation.
        window = 320  # 20ms at 16kHz
        hop = 160
        step = max(1, window // 4)
        frames_with_energy = 0
        smoothed = 0.0
        rising = 0
        for start in range(0, len(samples) - window, hop):
            frame = samples[start : start + window : step]
            sum_sq = 0.0
            for value in frame:
                sum_sq += value * value
            level = math.sqrt(sum_sq / len(frame))
            if level > _VOICED_RMS_THRESHOLD:
                frames_with_energy += 1
                if smoothed and level > smoothed * 1.6:
                    rising += 1
            smoothed = level if not smoothed else smoothed * 0.7 + level * 0.3
        return frames_with_energy > 2 and rising > 0

    @staticmethod
    def _clean_text(text: str) -> str:
        # Strip SenseVoice special tags (<|zh|>, <|en|>, <|HIS|>, ...) and
        # punctuation-only fragments left by silence: bare tags and lone
        # punctuation are model artifacts, not speech, and must not reach the
        # composer. CJK output frequently looks punctuation-only in shape, so
        # hanzi/kana/hangul count as word characters for the keep-test.
        cleaned = re.sub(r"<\|[^|>]*\|>", " ", text)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned or not _WORD_RE.search(cleaned):
            return ""
        return cleaned

    async def transcribe(
        self,
        audio: SttAudio,
        *,
        model_id: str | None = None,
        language: str | None = None,
    ) -> SttResult:
        recognizer = await self._get_recognizer(language)
        rate, samples = await asyncio.to_thread(self._to_pcm, audio)
        if not samples:
            return SttResult(
                text="", provider=self.provider_id, model="sensevoice",
            )
        if rate != _TARGET_SAMPLE_RATE:
            samples = self._resample(rate, samples)

        def _run() -> tuple[str, str | None]:
            stream = recognizer.create_stream()
            stream.accept_waveform(_TARGET_SAMPLE_RATE, samples)
            recognizer.decode_stream(stream)
            text = str(getattr(stream.result, "text", "") or "").strip()
            raw_lang = str(getattr(stream.result, "lang", "") or "")
            lang: str | None = (
                raw_lang.strip("<|").split("|>")[0] if raw_lang else None
            )
            return text, (lang if lang and lang != "auto" else None)

        text, detected = await asyncio.to_thread(_run)
        cleaned = self._clean_text(text)
        if (
            cleaned
            and len(cleaned) <= _MAX_SHORT_OUTPUT_CHARS
            and not self._voiced(samples)
        ):
            # Short hallucination on an unvoiced signal: silence noise.
            cleaned = ""
        return SttResult(
            text=cleaned,
            provider=self.provider_id,
            model="sensevoice",
            language_code=detected,
        )
