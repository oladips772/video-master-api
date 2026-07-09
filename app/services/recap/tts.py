"""
Step 4 — generate_tts.

Synthesizes narration per segment through the repo's existing TTS layer
(generate_speech_chunked: Kokoro for standard voice ids, XTTS for everything
else, e.g. cloned voices), then reconciles each clip's duration with its
narration:

  a = narration duration + 0.4s tail pad, v = clip duration
  - a > v: extend source_end (re-cut) up to +8s if in-bounds and not crossing
    the next segment; else slow the clip with setpts up to 1.18x; if still
    short, freeze the last frame (tpad=stop_mode=clone).
  - v > a + 1.5s: trim the clip end.
  Hard rule: playback speed stays within 0.85-1.25x.

ctx in:  {payload, movie_path, movie_duration_sec, segments (clip_path)}
ctx out: + {segments[*].{tts_path, tts_duration_sec}}, clips re-timed in place
"""
import logging
import os
import re
from typing import Any, Dict, Optional

from app.services.audio.text_to_speech import generate_speech_chunked
from app.services.recap.clips import mezzanine_vf
from app.services.recap.utils import ffmpeg, media_duration, save_ctx, scratch_dir

logger = logging.getLogger(__name__)

TAIL_PAD_SEC = 0.4
MAX_EXTEND_SEC = 8.0
MAX_SLOW_FACTOR = 1.18
TRIM_SLACK_SEC = 1.5
SPEED_MAX = 1.25

# Kokoro voice ids look like af_alloy / am_echo / bf_emma; anything else is
# assumed to be an XTTS speaker or clone.
_KOKORO_VOICE = re.compile(r"^[a-z]{2}_[a-z0-9]+$")


def _tts_provider(voice_id: str) -> str:
    return "kokoro" if _KOKORO_VOICE.match(voice_id or "") else "xtts"


async def _recut_clip(ctx: Dict[str, Any], seg: Dict[str, Any], new_end: float) -> None:
    """Re-cut the segment's clip from the movie with a later end point."""
    settings = ctx["payload"]["settings"]
    clip = seg["clip_path"]
    tmp = clip + ".recut.mp4"
    await ffmpeg(
        [
            "-ss", f"{seg['source_start']:.3f}",
            "-to", f"{new_end:.3f}",
            "-i", ctx["movie_path"],
            "-vf", mezzanine_vf(settings),
            "-r", str(settings["fps"]),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            tmp,
        ]
    )
    os.replace(tmp, clip)
    seg["source_end"] = new_end


async def _retime_clip(seg: Dict[str, Any], factor: float, freeze_to: Optional[float]) -> None:
    """Slow video by `factor` (setpts), stretch audio to match (atempo), and
    optionally freeze the last frame out to cover the remainder."""
    clip = seg["clip_path"]
    tmp = clip + ".retimed.mp4"
    vf = f"setpts={factor:.4f}*PTS"
    af = f"atempo={1 / factor:.4f}"
    if freeze_to is not None:
        vf += f",tpad=stop_mode=clone:stop_duration={freeze_to:.3f}"
        af += f",apad=pad_dur={freeze_to:.3f}"
    await ffmpeg(
        [
            "-i", clip,
            "-vf", vf,
            "-af", af,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            tmp,
        ]
    )
    os.replace(tmp, clip)


async def _trim_clip(seg: Dict[str, Any], new_duration: float) -> None:
    clip = seg["clip_path"]
    tmp = clip + ".trimmed.mp4"
    await ffmpeg(
        [
            "-i", clip,
            "-t", f"{new_duration:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            tmp,
        ]
    )
    os.replace(tmp, clip)


async def _reconcile(ctx: Dict[str, Any], idx: int) -> None:
    segments = ctx["segments"]
    seg = segments[idx]
    movie_duration = float(ctx["movie_duration_sec"])

    v = await media_duration(seg["clip_path"]) or 0.0
    a = float(seg["tts_duration_sec"]) + TAIL_PAD_SEC

    if a > v:
        # 1) Extend the source window if there's room.
        next_start = (
            segments[idx + 1]["source_start"] if idx + 1 < len(segments) else movie_duration
        )
        max_end = min(seg["source_end"] + MAX_EXTEND_SEC, movie_duration, next_start)
        needed_end = seg["source_start"] + a
        if max_end > seg["source_end"] + 0.25:
            new_end = min(needed_end, max_end)
            logger.info(
                "seg %03d: extending clip %.2fs -> %.2fs", seg["id"], seg["source_end"], new_end
            )
            await _recut_clip(ctx, seg, new_end)
            v = await media_duration(seg["clip_path"]) or v

        if a > v:
            # 2) Slow the clip (hard-capped), 3) freeze the tail if still short.
            factor = min(a / v, MAX_SLOW_FACTOR, SPEED_MAX)
            freeze = a - v * factor if v * factor < a else None
            logger.info(
                "seg %03d: retime x%.3f%s",
                seg["id"], factor, f" + freeze {freeze:.2f}s" if freeze else "",
            )
            await _retime_clip(seg, factor, freeze)

    elif v > a + TRIM_SLACK_SEC:
        logger.info("seg %03d: trimming clip %.2fs -> %.2fs", seg["id"], v, a)
        await _trim_clip(seg, a)


async def generate_tts(ctx: Dict[str, Any]) -> Dict[str, Any]:
    payload = ctx["payload"]
    scratch = scratch_dir(payload["project_id"])
    recap = payload["recap"]
    voice_id = recap.get("voice_id") or "af_alloy"
    speed = float(recap.get("voice_speed") or 1.0)
    provider = _tts_provider(voice_id)
    logger.info("[%s] TTS provider=%s voice=%s", payload["project_id"], provider, voice_id)

    for seg in ctx["segments"]:
        audio_path = os.path.join(scratch, f"seg_{seg['id']:03d}.mp3")
        if not (os.path.exists(audio_path) and os.path.getsize(audio_path) > 0):
            await generate_speech_chunked(
                seg["narration"], voice_id, speed, audio_path, provider=provider
            )
        seg["tts_path"] = audio_path
        seg["tts_duration_sec"] = await media_duration(audio_path)
        if seg["tts_duration_sec"] is None:
            raise RuntimeError(f"generate_tts: could not read duration of {audio_path}")

    for idx in range(len(ctx["segments"])):
        await _reconcile(ctx, idx)

    save_ctx(ctx)
    return ctx
