"""
Background video registry for the Reddit render channel.

Maps short keys (used in API requests) to absolute file paths on disk.
Files are expected to live under ``assets/backgrounds/`` at the project root.
See ``assets/backgrounds/README.md`` for what to put there.
"""
import os
from typing import Dict, List

# Project root = two parents up from this file (.../app/services/backgrounds.py)
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BACKGROUNDS_DIR = os.path.join(_PROJECT_ROOT, "assets", "backgrounds")


BACKGROUNDS: Dict[str, str] = {
    "minecraft":      os.path.join(_BACKGROUNDS_DIR, "minecraft.mp4"),
    "subway_surfers": os.path.join(_BACKGROUNDS_DIR, "subway_surfers.mp4"),
    "gta":            os.path.join(_BACKGROUNDS_DIR, "gta.mp4"),
    "satisfying":     os.path.join(_BACKGROUNDS_DIR, "satisfying.mp4"),
    "messi":          os.path.join(_BACKGROUNDS_DIR, "messi.mp4"),
    "luxury":         os.path.join(_BACKGROUNDS_DIR, "luxury.mp4"),
    "transformers":   os.path.join(_BACKGROUNDS_DIR, "transformers.mp4"),
    "kingdoms":       os.path.join(_BACKGROUNDS_DIR, "kingdoms.mp4"),
}


def list_background_keys() -> List[str]:
    """Return all registered background keys."""
    return sorted(BACKGROUNDS.keys())


def resolve_background(key: str) -> str:
    """
    Resolve a background key to an absolute file path.

    Raises:
        KeyError: key is not registered in BACKGROUNDS.
    """
    if key not in BACKGROUNDS:
        raise KeyError(
            f"Unknown background '{key}'. Available: {list_background_keys()}"
        )
    return BACKGROUNDS[key]
