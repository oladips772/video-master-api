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
import asyncio
import logging
import os
import re
from typing import Any, Dict, Optional

from app.services.audio.text_to_speech import generate_speech_chunked
from app.services.job_queue import job_queue
from app.services.recap.deliver import report_progress
from app.services.recap.utils import ffmpeg, media_duration, save_ctx, scratch_dir

logger = logging.getLogger(__name__)

TAIL_PAD_SEC = 0.6  # was 0.4 — wider margin so retime/trim rounding can't clip narration
# Safety net for the rare segment where even 25% footage headroom in
# clips.py isn't enough — allow slow-mo up to 1.25x before the freeze
# fallback kicks in. Still imperceptible to viewers.
MAX_SLOW_FACTOR = 1.25
# 0.35s of slack tolerates rounding without letting per-segment overshoot
# accumulate as caption-vs-video drift (previously 1.5s → ~1s per segment
# could survive un-trimmed, adding tens of seconds of drift over a 40+
# segment recap).
TRIM_SLACK_SEC = 0.35  # was 0.3
SPEED_MAX = 1.25

# Kokoro voice ids look like af_alloy / am_echo / bf_emma; anything else is
# assumed to be an XTTS speaker or clone.
_KOKORO_VOICE = re.compile(r"^[a-z]{2}_[a-z0-9]+$")


def _tts_provider(voice_id: str) -> str:
    return "kokoro" if _KOKORO_VOICE.match(voice_id or "") else "xtts"


async def _retime_clip(seg: Dict[str, Any], factor: float, freeze_to: Optional[float]) -> None:
    """Slow video by `factor` (setpts), stretch audio to match (atempo), and
    optionally freeze the last frame out to cover the remainder."""
    clip = seg["clip_path"]
    tmp = clip + ".retimed.mp4"
    # Reset to a zero PTS baseline FIRST, then apply the speed factor — without
    # the reset, this clip's PTS state (post -c:v copy into mux_XXX.mp4) can
    # carry a residual offset into the video-only concat, which -fps_mode cfr
    # then "corrects" with a genuine duplicate-frame freeze.
    vf = f"setpts=PTS-STARTPTS,setpts={factor:.4f}*PTS"
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
    v = await media_duration(seg["clip_path"]) or 0.0
    a = float(seg["tts_duration_sec"]) + TAIL_PAD_SEC

    if v <= 0.0:
        logger.warning("seg %03d: zero-length clip, skipping reconcile", seg["id"])
        return

    if a > v:
        # Narration longer than the multi-cut clip: slow the clip (hard-capped),
        # then freeze the last frame to cover any remainder. We do NOT re-cut
        # into continuous source footage — that would destroy the multi-cut
        # variety and was the source of the "frozen/wrong-footage" drift bug.
        factor = min(a / v, MAX_SLOW_FACTOR, SPEED_MAX)
        freeze = a - v * factor if v * factor < a else None
        logger.info(
            "seg %03d: retime x%.3f%s (v=%.2fs a=%.2fs)",
            seg["id"], factor, f" + freeze {freeze:.2f}s" if freeze else "", v, a,
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

    -50dB (was -40dB) is less aggressive — soft word onsets can fall below
    -40dB and get wrongly stripped as silence, clipping the actual start of
    the narration. -50dB only catches genuine silence. adelay=40|40 pads 40ms
    of silence back before speech as a safety buffer against the trim being
    slightly too tight.
    """
    tmp = audio_path + ".trim.mp3"
    await ffmpeg(
        [
            "-i", audio_path,
            "-af",
            "silenceremove=start_periods=1:start_duration=0.05:start_threshold=-50dB,"
            "adelay=40|40",
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

    total_segments = len(ctx["segments"])
    for idx, seg in enumerate(ctx["segments"]):
        if job_queue.is_cancelled(ctx.get("job_id")):
            logger.info(
                "[%s] generate_tts: cancelled before seg %03d", payload["project_id"], seg["id"]
            )
            raise asyncio.CancelledError(f"cancelled before generate_tts seg {seg['id']}")

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

        await _reconcile(ctx, idx)

        # Segment-level progress only (never finer than per-segment).
        seg_percent = 50 + (32 * (idx + 1) / total_segments)
        await report_progress(
            payload,
            "generate_tts",
            "Generating narration",
            seg_percent,
            f"Generating narration ({idx + 1}/{total_segments})",
            current=idx + 1,
            total=total_segments,
        )

    save_ctx(ctx)
    return ctx
