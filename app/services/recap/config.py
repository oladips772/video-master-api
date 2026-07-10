"""
Configuration for the recap render chain (env-driven, matching the repo's
pattern of reading os.environ directly in each service).
"""
import os

RECAP_SCRATCH_ROOT = os.environ.get("RECAP_SCRATCH_ROOT", "/tmp/recap")

# Gemini (free tier) for script generation.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# Whisper fallback transcription model for movies without subtitles.
RECAP_WHISPER_MODEL = os.environ.get("RECAP_WHISPER_MODEL", "medium")

# Snap segment boundaries to detected scene cuts (needs scenedetect installed).
RECAP_SCENE_SNAP = os.environ.get("RECAP_SCENE_SNAP", "0") == "1"

# Multi-cut clip extraction: break each segment into 1–6 sub-clips for visual
# variety instead of one continuous cut. Falls back to a single continuous cut
# when set to 0. Scene-detection boundaries are used as sub-clip starts when
# scenedetect is installed; otherwise sub-clips are evenly distributed.
RECAP_MULTI_CUT = os.environ.get("RECAP_MULTI_CUT", "1") == "1"
RECAP_MIN_SUBCLIP_SEC = float(os.environ.get("RECAP_MIN_SUBCLIP_SEC", "2"))
RECAP_MAX_SUBCLIP_SEC = float(os.environ.get("RECAP_MAX_SUBCLIP_SEC", "5"))

# Optional bottom-right channel watermark on the final render. Empty → skip.
RECAP_WATERMARK_TEXT = os.environ.get("RECAP_WATERMARK_TEXT", "")
RECAP_WATERMARK_OPACITY = float(os.environ.get("RECAP_WATERMARK_OPACITY", "0.3"))
RECAP_WATERMARK_SIZE = int(os.environ.get("RECAP_WATERMARK_SIZE", "24"))

# `nice` level for FFmpeg subprocesses so renders don't starve the API.
RECAP_FFMPEG_NICE = int(os.environ.get("RECAP_FFMPEG_NICE", "10"))

# Sent as X-Pipeline-Secret on status callbacks to Recap Studio when set
# (must match Recap Studio's PIPELINE_SHARED_SECRET).
PIPELINE_SHARED_SECRET = os.environ.get("PIPELINE_SHARED_SECRET", "")

RESOLUTIONS = {
    "1K": (1920, 1080),
    "2K": (2560, 1440),
    "4K": (3840, 2160),
}


def frame_size(resolution: str, aspect_ratio: str) -> tuple:
    """Exact output frame size for a resolution tier + aspect ratio."""
    w, h = RESOLUTIONS.get(resolution, RESOLUTIONS["1K"])
    if aspect_ratio == "9:16":
        return h * 9 // 16, h
    if aspect_ratio == "1:1":
        return h, h
    return w, h
