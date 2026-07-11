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
from app.services.recap.clips import _encode_clip, mezzanine_vf
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
    """Re-cut the segment's clip with a later end point.

    Routed through _encode_clip so reconciliation cuts pick up the same
    distortion filter (crop+scale+atempo) as the initial multi-cut, keeping
    every segment consistent under Content-ID fingerprinting.
    """
    settings = ctx["payload"]["settings"]
    clip = seg["clip_path"]
    tmp = clip + ".recut.mp4"
    await _encode_clip(
        ctx,
        seg["source_start"],
        new_end,
        tmp,
        mezzanine_vf(settings),
        label=f"seg {seg['id']:03d} recut",
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


async def _trim_leading_silence(audio_path: str) -> None:
    """Strip leading silence from a TTS output.

    Kokoro/XTTS both add a short lead-in silence (typically 100–400ms) to
    every generated file. Under the per-segment sidechain mux this reads as
    the original movie audio playing for that beat before narration kicks in
    — the "narration starts a little slow" symptom. silenceremove trims it
    in place; running it a second time on already-trimmed audio is a no-op.
    """
    tmp = audio_path + ".trim.mp3"
    await ffmpeg(
        [
            "-i", audio_path,
            "-af",
            "silenceremove=start_periods=1:start_duration=0.05:start_threshold=-40dB",
            tmp,
        ]
    )
    os.replace(tmp, audio_path)


async def generate_tts(ctx: Dict[str, Any]) -> Dict[str, Any]:
    payload = ctx["payload"]
    scratch = scratch_dir(payload["project_id"])
    recap = payload["recap"]
    voice_id = recap.get("voice_id") or "af_alloy"
    speed = float(recap.get("voice_speed") or 1.0)
    provider = _tts_provider(voice_id)
    logger.info("[%s] TTS provider=%s voice=%s", payload["project_id"], provider, voice_id)

    for seg in ctx["segments"]:
        # Trailing full-stops make Kokoro/XTTS tack on a small end-of-utterance
        # pause; strip them so the next segment starts cleanly. rstrip("." )
        # removes both a trailing period and any trailing whitespace it leaves.
        seg["narration"] = (seg.get("narration") or "").rstrip(". ").rstrip()

        audio_path = os.path.join(scratch, f"seg_{seg['id']:03d}.mp3")
        if not (os.path.exists(audio_path) and os.path.getsize(audio_path) > 0):
            await generate_speech_chunked(
                seg["narration"], voice_id, speed, audio_path, provider=provider
            )
            # Only trim freshly generated TTS — cached files were already trimmed.
            await _trim_leading_silence(audio_path)
        seg["tts_path"] = audio_path
        seg["tts_duration_sec"] = await media_duration(audio_path)
        if seg["tts_duration_sec"] is None:
            raise RuntimeError(f"generate_tts: could not read duration of {audio_path}")

    for idx in range(len(ctx["segments"])):
        await _reconcile(ctx, idx)

    save_ctx(ctx)
    return ctx
