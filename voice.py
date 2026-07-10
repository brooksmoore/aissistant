"""Local voice-note transcription (faster-whisper). Runs entirely on this Mac —
free, private, no audio ever leaves the machine. Model downloads once (~145MB)."""
import logging

log = logging.getLogger("penny.voice")

_model = None


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        log.info("loading whisper model (first time downloads ~145MB)…")
        _model = WhisperModel("base.en", device="cpu", compute_type="int8")
    return _model


def transcribe(path: str) -> str:
    """Blocking — call via asyncio.to_thread. Returns the spoken text."""
    segments, _info = _get_model().transcribe(path, vad_filter=True, beam_size=5)
    return " ".join(s.text.strip() for s in segments).strip()


def warmup():
    """Optionally call at startup so the first voice note isn't slow."""
    try:
        _get_model()
        log.info("whisper model ready")
    except Exception:
        log.exception("whisper warmup failed — voice notes will error until fixed")
