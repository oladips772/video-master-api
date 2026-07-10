"""
Step 3 — extract_clips.

Cut each segment's source window and re-encode to a uniform mezzanine
(identical codec/frame/rate everywhere) so the later concat is lossless-safe.
Optional PySceneDetect boundary snapping (±3s) when RECAP_SCENE_SNAP=1.

ctx in:  {payload, movie_path, movie_duration_sec, segments}
ctx out: + {segments[*].clip_path, scene_boundaries?}
"""
import asyncio
import logging
import os
from typing import Any, Dict, List

from app.services.recap.config import RECAP_SCENE_SNAP, frame_size
from app.services.recap.utils import ffmpeg, save_ctx, scratch_dir

logger = logging.getLogger(__name__)

SNAP_WINDOW_SEC = 3.0


def mezzanine_vf(settings: Dict[str, Any]) -> str:
    """Scale-to-fill + crop to the exact frame — no black bars.

    Sacrifices small edges on ultrawide/portrait sources instead of letterboxing,
    which is standard practice for movie-recap channels. Shared with the re-cut
    in step 4 so mezzanine and re-cut match pixel-for-pixel.
    """
    width, height = frame_size(settings["resolution"], settings["aspect_ratio"])
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,format=yuv420p"
    )


def _detect_scenes_sync(movie: str) -> List[float]:
    from scenedetect import ContentDetector, detect

    logger.info("scene detection pass on %s (this can take a while)", movie)
    scene_list = detect(movie, ContentDetector())
    return [s[0].get_seconds() for s in scene_list]


def _snap(t: float, boundaries: List[float], duration: float) -> float:
    best = min(boundaries, key=lambda b: abs(b - t), default=None)
    if best is not None and abs(best - t) <= SNAP_WINDOW_SEC:
        return min(max(best, 0.0), duration)
    return t


async def extract_clips(ctx: Dict[str, Any]) -> Dict[str, Any]:
    payload = ctx["payload"]
    project_id = payload["project_id"]
    scratch = scratch_dir(project_id)
    settings = payload["settings"]
    duration = float(ctx["movie_duration_sec"])

    boundaries: List[float] = ctx.get("scene_boundaries") or []
    if RECAP_SCENE_SNAP and not boundaries:
        try:
            boundaries = await asyncio.get_event_loop().run_in_executor(
                None, _detect_scenes_sync, ctx["movie_path"]
            )
            ctx["scene_boundaries"] = boundaries
            save_ctx(ctx)  # cache — detection is expensive
        except ImportError:
            logger.warning("RECAP_SCENE_SNAP=1 but scenedetect is not installed — skipping")

    if boundaries:
        for seg in ctx["segments"]:
            seg["source_start"] = _snap(seg["source_start"], boundaries, duration)
            seg["source_end"] = _snap(seg["source_end"], boundaries, duration)
            if seg["source_end"] - seg["source_start"] < 2.0:
                seg["source_end"] = min(seg["source_start"] + 5.0, duration)

    vf = mezzanine_vf(settings)
    for seg in ctx["segments"]:
        clip = os.path.join(scratch, f"seg_{seg['id']:03d}.mp4")
        seg["clip_path"] = clip
        if os.path.exists(clip) and os.path.getsize(clip) > 0:
            continue  # resumable
        await ffmpeg(
            [
                "-ss", f"{seg['source_start']:.3f}",
                "-to", f"{seg['source_end']:.3f}",
                "-i", ctx["movie_path"],
                "-vf", vf,
                "-r", str(settings["fps"]),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-ar", "44100", "-ac", "2",
                "-movflags", "+faststart",
                clip,
            ]
        )
        logger.info(
            "[%s] cut seg_%03d (%.1fs-%.1fs)",
            project_id, seg["id"], seg["source_start"], seg["source_end"],
        )

    save_ctx(ctx)
    return ctx
