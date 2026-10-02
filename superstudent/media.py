"""Lectures and other media -> timestamped transcripts, with screen snapshots pinned to the timeline.

Order of preference for a transcript:
  1. Captions already on Canvas (fast, often human-corrected)
  2. Local Whisper transcription (faster-whisper everywhere, or mlx-whisper on Apple Silicon)
YouTube links use YouTube's own captions.

While a video is on disk for transcription, a frame is saved whenever the picture changes
(slide change, new whiteboard step), so the AI can look at what was on screen at 00:32:10.
"""

from __future__ import annotations

import html
import importlib.util
import os
import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .extract import ocr_image_file, tesseract_available
from .util import md_link

DEFAULT_MODELS = {"faster-whisper": "small.en", "mlx": "mlx-community/whisper-large-v3-turbo"}


class TranscriberUnavailable(RuntimeError):
    """The speech model couldn't be loaded (usually no internet on first use). Try again next sync."""


@dataclass
class Segment:
    start: float
    end: float
    text: str


def fmt_ts(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


_TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?")


def _seconds(match: "re.Match[str]") -> float:
    h, m, s, ms = match.groups()
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + (int((ms or "0").ljust(3, "0")) / 1000.0)


def parse_captions(text: str) -> List[Segment]:
    """Parse WebVTT or SRT captions into segments, collapsing rolling duplicates."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    segments: List[Segment] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:
            continue
        left, _, right = lines[idx].partition("-->")
        m1, m2 = _TS.search(left), _TS.search(right)
        if not m1:
            continue
        body = " ".join(lines[idx + 1:])
        body = re.sub(r"<[^>]+>", "", body)
        body = html.unescape(body).replace("&nbsp;", " ").strip()
        body = re.sub(r"\s+", " ", body)
        if body:
            segments.append(Segment(_seconds(m1), _seconds(m2) if m2 else _seconds(m1), body))
    merged: List[Segment] = []
    for seg in segments:
        if merged:
            prev = merged[-1]
            if seg.text == prev.text or prev.text.endswith(seg.text):
                prev.end = max(prev.end, seg.end)
                continue
            if seg.text.startswith(prev.text):
                merged[-1] = Segment(prev.start, seg.end, seg.text)
                continue
        merged.append(seg)
    return merged


def transcript_markdown(segments: Sequence[Segment], frames: Sequence[Tuple[float, str, str]] = (),
                        block_seconds: int = 60) -> str:
    """Group speech into ~1 minute blocks headed '## [HH:MM:SS]' and pin screen snapshots
    (time, relative image path, OCR text) into the block where they appeared."""
    buckets: Dict[int, Dict[str, list]] = {}
    for seg in segments:
        b = buckets.setdefault(int(seg.start // block_seconds), {"t": [], "text": [], "frames": []})
        b["t"].append(seg.start)
        b["text"].append(seg.text.strip())
    for t, rel_path, ocr in frames:
        b = buckets.setdefault(int(t // block_seconds), {"t": [], "text": [], "frames": []})
        b["t"].append(t)
        b["frames"].append((t, rel_path, ocr))
    parts: List[str] = []
    for key in sorted(buckets):
        b = buckets[key]
        lines = [f"## [{fmt_ts(min(b['t']))}]", ""]
        for t, rel_path, ocr in sorted(b["frames"]):
            lines.append(md_link(f"Screen at {fmt_ts(t)}", rel_path))
            if ocr:
                flat = re.sub(r"\s+", " ", ocr)[:600]
                lines.append(f"On screen (OCR): {flat}")
            lines.append("")
        if b["text"]:
            lines.append(" ".join(b["text"]))
        parts.append("\n".join(lines).strip())
    return "\n\n".join(parts)


# ---------------------------------------------------------------- Whisper

def detect_backend(preference: str = "auto") -> Optional[str]:
    has = lambda mod: importlib.util.find_spec(mod) is not None  # noqa: E731
    apple = platform.system() == "Darwin" and platform.machine() == "arm64"
    if preference == "mlx":
        return "mlx" if has("mlx_whisper") else None
    if preference == "faster-whisper":
        return "faster-whisper" if has("faster_whisper") else None
    if apple and has("mlx_whisper"):
        return "mlx"
    if has("faster_whisper"):
        return "faster-whisper"
    return "mlx" if has("mlx_whisper") else None


class Transcriber:
    def __init__(self, preference: str = "auto", model: str = "", log: Optional[Callable[[str], None]] = None):
        self.backend = detect_backend(preference)
        self.model_name = model or (DEFAULT_MODELS.get(self.backend or "", ""))
        self.log = log or (lambda msg: None)
        self._model = None

    @property
    def available(self) -> bool:
        return self.backend is not None

    def describe(self) -> str:
        return f"{self.backend} ({self.model_name})" if self.backend else "not installed"

    def transcribe(self, path: Path, prompt: str = "") -> List[Segment]:
        if self.backend == "faster-whisper":
            from faster_whisper import WhisperModel

            if self._model is None:
                self.log(f"  loading Whisper model {self.model_name} (first time downloads it)…")
                threads = max(2, (os.cpu_count() or 4) // 2)
                try:
                    self._model = WhisperModel(self.model_name, device="auto", compute_type="int8", cpu_threads=threads,
                                               download_root=_model_folder(self.model_name))
                except Exception as exc:
                    raise TranscriberUnavailable(
                        f"couldn't load the speech model '{self.model_name}' ({exc.__class__.__name__}); it downloads "
                        "from huggingface.co the first time, so check the internet connection") from exc
            audio = load_audio(path)
            if not len(audio):
                return []
            segments, _info = self._model.transcribe(
                audio, vad_filter=True, beam_size=5, initial_prompt=(prompt or None),
                condition_on_previous_text=False,
            )
            return [Segment(s.start, s.end, s.text.strip()) for s in segments if s.text and s.text.strip()]
        if self.backend == "mlx":
            import mlx_whisper

            audio = load_audio(path)
            try:
                result = mlx_whisper.transcribe(audio, path_or_hf_repo=self.model_name, initial_prompt=(prompt or None),
                                                condition_on_previous_text=False)
            except Exception as exc:
                if self._model is None:  # never worked yet: the model download, or something about this recording?
                    if not self._mlx_works():
                        raise TranscriberUnavailable(
                            f"couldn't load the speech model '{self.model_name}' ({exc.__class__.__name__})") from exc
                    self._model = True   # the model is fine: only this recording failed (don't hold up the rest)
                raise
            self._model = True
            return [Segment(float(s["start"]), float(s["end"]), s["text"].strip())
                    for s in result.get("segments", []) if (s.get("text") or "").strip()]
        raise RuntimeError("No transcription backend installed")

    def _mlx_works(self) -> bool:
        """Transcribe one second of silence: tells a broken model download apart from one bad recording."""
        try:
            import mlx_whisper
            import numpy as np

            mlx_whisper.transcribe(np.zeros(16000, dtype=np.float32), path_or_hf_repo=self.model_name)
            return True
        except Exception:
            return False


def _model_folder(name: str) -> Optional[str]:
    """Where a speech model is downloaded: Super Student's own folder, so uninstalling removes it too. A model
    already downloaded to the shared Hugging Face cache by an earlier version is used where it is."""
    if os.path.isdir(name):
        return None
    shared = Path(os.environ.get("HF_HUB_CACHE") or Path.home() / ".cache" / "huggingface" / "hub")
    tail = name.split("/")[-1].lower()
    if shared.is_dir() and any(p.name.lower().endswith("faster-whisper-" + tail) for p in shared.glob("models--*")):
        return None
    from .config import APP_DIR

    return str(APP_DIR / "models")


def load_audio(path: Path):
    """16 kHz mono float32 samples, decoded with the FFmpeg libraries bundled in PyAV.

    (faster-whisper's own decoder passes an argument newer PyAV releases reject, so we decode here
    and hand Whisper the samples directly.)"""
    import av
    import numpy as np

    chunks = []
    with av.open(str(path)) as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            return np.zeros(0, dtype=np.float32)
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for packet in container.demux(stream):
            try:
                frames = packet.decode()
            except Exception:  # a corrupt packet shouldn't lose the whole lecture
                continue
            for frame in frames:
                frame.pts = None
                for out in resampler.resample(frame):
                    chunks.append(out.to_ndarray().reshape(-1))
        try:
            for out in resampler.resample(None):
                chunks.append(out.to_ndarray().reshape(-1))
        except Exception:
            pass
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def media_duration(path: Path) -> Optional[float]:
    try:
        import av

        with av.open(str(path)) as container:
            if container.duration:
                return float(container.duration) / 1_000_000
    except Exception:
        return None
    return None


# ---------------------------------------------------------------- screen snapshots

def extract_keyframes(video: Path, assets: Path, *, threshold: float = 12.0, min_gap: float = 6.0,
                      max_frames: int = 200, ocr: bool = True) -> List[Tuple[float, str, str]]:
    """Save a JPEG each time the picture changes noticeably. Returns (seconds, relative path, OCR text)."""
    try:
        import av
        import numpy as np
        from PIL import Image  # noqa: F401  (frame.to_image needs Pillow)
    except ImportError:
        return []
    try:
        container = av.open(str(video))
    except Exception:
        return []
    kept: List[Tuple[float, str, str]] = []
    try:
        streams = [s for s in container.streams if s.type == "video"]
        if not streams:
            return []
        stream = streams[0]
        duration = float(container.duration or 0) / 1_000_000

        def scan(keyframes_only: bool) -> List[Tuple[float, object]]:
            stream.codec_context.skip_frame = "NONKEY" if keyframes_only else "DEFAULT"
            found: List[Tuple[float, object]] = []
            last_thumb = None
            last_kept = -1e9
            last_checked = -1e9
            for frame in container.decode(stream):
                if frame.time is None:
                    continue
                t = float(frame.time)
                if not keyframes_only and t - last_checked < 2.0:
                    continue
                last_checked = t
                img = frame.to_image()
                thumb = np.asarray(img.convert("L").resize((64, 36)), dtype=np.float32)
                if thumb.std() < 4.0:          # black or blank screen
                    continue
                if last_thumb is None or (np.abs(thumb - last_thumb).mean() > threshold and t - last_kept >= min_gap):
                    found.append((t, img))
                    last_thumb, last_kept = thumb, t
                    if len(found) >= max_frames:
                        break
            return found

        frames = scan(keyframes_only=True)
        if duration > 600 and len(frames) < max(3, duration / 600):
            container.seek(0)
            frames = scan(keyframes_only=False)
        if not frames:
            return []
        assets.mkdir(parents=True, exist_ok=True)
        do_ocr = ocr and tesseract_available()
        for t, img in frames:
            name = f"screen-{fmt_ts(t).replace(':', '-')}.jpg"
            target = assets / name
            img = img.convert("RGB")
            img.thumbnail((1280, 1280))
            img.save(target, "JPEG", quality=80)
            text = ocr_image_file(target, timeout=30) if do_ocr else ""
            kept.append((t, f"{assets.name}/{name}", text))
    except Exception:
        return kept
    finally:
        container.close()
    return kept


# ---------------------------------------------------------------- YouTube

def youtube_transcript(video_id: str) -> List[Segment]:
    from youtube_transcript_api import YouTubeTranscriptApi

    langs = ["en", "en-US", "en-GB", "en-CA"]
    api = YouTubeTranscriptApi()
    if hasattr(api, "fetch"):
        try:
            fetched = api.fetch(video_id, languages=langs)
        except Exception:
            listing = api.list(video_id)
            fetched = next(iter(listing)).fetch()
        return [Segment(float(s.start), float(s.start) + float(s.duration), s.text) for s in fetched if s.text.strip()]
    data = YouTubeTranscriptApi.get_transcript(video_id, languages=langs)  # type: ignore[attr-defined]
    return [Segment(float(d["start"]), float(d["start"]) + float(d.get("duration", 0)), d["text"]) for d in data]


def youtube_title(video_id: str) -> Tuple[str, str]:
    import requests

    try:
        resp = requests.get("https://www.youtube.com/oembed",
                            params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"},
                            timeout=15)
        if resp.ok:
            data = resp.json()
            return data.get("title") or "", data.get("author_name") or ""
    except Exception:
        pass
    return "", ""


def pick_media_source(sources: Iterable[dict]) -> Optional[dict]:
    """From Canvas media_sources, pick the smallest file that still has the audio."""
    usable = [s for s in sources or [] if s.get("url")]
    if not usable:
        return None
    audio = [s for s in usable if str(s.get("content_type", "")).startswith("audio/")]
    pool = audio or [s for s in usable if "mp4" in str(s.get("content_type", "")) or s.get("fileExt") == "mp4"] or usable

    def weight(s: dict) -> float:
        for key in ("size", "bitrate"):
            try:
                return float(s.get(key))
            except (TypeError, ValueError):
                continue
        return 1e12

    return sorted(pool, key=weight)[0]
