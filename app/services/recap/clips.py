"""
Step 3 — extract_clips.

Cut each segment's source window and re-encode to a uniform mezzanine
(identical codec/frame/rate everywhere) so the later concat is lossless-safe.
Optional PySceneDetect boundary snapping (±3s) when RECAP_SCENE_SNAP=1.

Multi-cut (RECAP_MULTI_CUT=1, default): each segment is broken into 1–6
sub-clips (2–5s each) instead of one continuous cut, using scene boundaries
when available, otherwise evenly distributed. Sub-clips are concatenated into
a single per-segment clip file that flows through TTS/reconcile/assembly
unchanged.

ctx in:  {payload, movie_path, movie_duration_sec, segments}
ctx out: + {segments[*].clip_path, scene_boundaries?}
"""
import asyncio
import logging
import os
import random
from typing import Any, Dict, List, Optional, Tuple

from app.services.recap.config import (
    RECAP_MAX_SUBCLIP_SEC,
    RECAP_MIN_SUBCLIP_SEC,
    RECAP_MULTI_CUT,
    RECAP_SCENE_SNAP,
    frame_size,
)
from app.services.recap.deliver import report_progress
from app.services.recap.utils import ffmpeg, media_duration, save_ctx, scratch_dir

logger = logging.getLogger(__name__)

SNAP_WINDOW_SEC = 3.0

# Estimated narration reading speed used to size sub-clips at extract time
# (before TTS actually runs). Matches script_gen's target density.
_WORDS_PER_MINUTE = 155.0

# Cut ~25% more footage per segment than the narration estimate calls for.
# Real TTS often runs longer than the WPM estimate; without headroom, the
# reconcile step ends up slowing or freezing the clip. With headroom the
# clip is almost always longer than the audio, so reconcile can just TRIM
# (seamless) instead of slowing/freezing (visible). Extra footage never
# leaks past source_end — the picker caps every sub-clip to the window.
FOOTAGE_HEADROOM = 1.25


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


def _estimate_narration_duration(narration: str) -> float:
    """Word-count-based estimate at ~155 WPM. Used before TTS actually runs."""
    words = len((narration or "").split())
    return (words / _WORDS_PER_MINUTE) * 60.0


def _pick_subclips(
    source_start: float,
    source_end: float,
    target_duration: float,
    boundaries: List[float],
) -> List[Tuple[float, float]]:
    """Choose 1–6 (sub_start, sub_end) cuts within [source_start, source_end]
    whose lengths together approximate ``target_duration``.

    Sub-clip count tiers (spec §1.2):
      target ≤ 4s → 1; 4–8s → 3; 8–12s → 4; >12s → 6.

    Method A uses scene boundaries strictly inside the window when at least N
    are available (N distinct cuts → more visually distinct sub-clips).
    Method B (fallback) divides the window into N equal sections and places a
    sub-clip centered in each.
    """
    window = source_end - source_start
    if window <= 0 or target_duration <= 0:
        return [(source_start, source_end)]

    if target_duration <= 4.0:
        desired = 1
    elif target_duration <= 8.0:
        desired = 3
    elif target_duration <= 12.0:
        desired = 4
    else:
        desired = 6

    # Cap by what fits given the minimum sub-clip length.
    max_by_window = max(1, int(window // RECAP_MIN_SUBCLIP_SEC))
    n = max(1, min(desired, max_by_window))
    if n == 1:
        return [(source_start, min(source_start + target_duration, source_end))]

    sub_dur = max(RECAP_MIN_SUBCLIP_SEC, min(RECAP_MAX_SUBCLIP_SEC, target_duration / n))

    # --- Method A: scene boundaries inside the window.
    in_window = sorted(b for b in boundaries if source_start < b < source_end)
    if len(in_window) >= n:
        step = max(1, len(in_window) // n)
        picks = in_window[::step][:n]
        subclips = [
            (p, min(p + sub_dur, source_end))
            for p in picks
            if source_end - p >= RECAP_MIN_SUBCLIP_SEC
        ]
        if len(subclips) >= 1:
            return subclips

    # --- Method B: evenly distributed sections.
    section_size = window / n
    subclips: List[Tuple[float, float]] = []
    for i in range(n):
        section_start = source_start + i * section_size
        section_end = source_start + (i + 1) * section_size
        margin = max(0.0, (section_size - sub_dur) / 2.0)
        sub_start = section_start + margin
        sub_end = min(sub_start + sub_dur, section_end)
        if sub_end - sub_start >= RECAP_MIN_SUBCLIP_SEC:
            subclips.append((sub_start, sub_end))
    return subclips or [(source_start, min(source_start + target_duration, source_end))]


# --- Old distortion recipe (crop + zoom + speed jitter). Superseded below by
# a crop+shake-only version with no speed change. Kept for reference.
#
# def build_distortion_filter() -> Optional[Dict[str, str]]:
#     """Return a per-sub-clip distortion recipe (Content-ID break) or None.
#
#     Random ranges match the spec: center-crop 4–6%, zoom 4–7%, subtle
#     horizontal pan wiggle, and video-speed jitter 0.98–1.02x. Composed as
#     plain crop/scale/setpts so it chains cleanly BEFORE the mezzanine
#     scale/crop, keeping every sub-clip at the same final resolution.
#
#     Returned dict keys:
#       vf      — filter chain to prepend to the mezzanine chain
#       af      — atempo compensation so audio stays in sync
#       summary — short label for logging ("crop=95% zoom=106% speed=1.01x")
#
#     Skipped entirely (returns None) when RECAP_DISTORTION_ENABLED != "1".
#     """
#     if os.getenv("RECAP_DISTORTION_ENABLED", "1") != "1":
#         return None
#
#     crop_pct = random.uniform(0.94, 0.96)
#     # Zoom: base 1.03 + range 0.01 → subtle static 3–4% zoom-in.
#     zoom_pct = random.uniform(1.03, 1.04)
#     # Pan amplitude capped at 0.008 so the sin() drift stays sub-pixel-per-sec
#     # on typical resolutions — reads as a still frame, not a shake.
#     pan_amp = random.uniform(0.002, 0.008)
#     # ±1% speed jitter — enough for Content-ID variance, imperceptible to viewers.
#     speed = random.uniform(0.99, 1.01)
#
#     # Crop tuple: W, H, X, Y. X wobbles with a slow sin() → subtle pan.
#     x_expr = f"(iw-iw*{crop_pct:.4f})/2 + iw*{pan_amp:.4f}*sin(t*2)"
#     y_expr = f"(ih-ih*{crop_pct:.4f})/2"
#     vf = (
#         f"crop=iw*{crop_pct:.4f}:ih*{crop_pct:.4f}:'{x_expr}':{y_expr},"
#         f"scale=iw*{zoom_pct:.4f}:ih*{zoom_pct:.4f},"
#         f"setpts=PTS*{speed:.4f}"
#     )
#     # atempo=1/speed compensates the video PTS change so AV stays in sync.
#     af = f"atempo={1.0 / speed:.4f}"
#     summary = (
#         f"crop={int(round(crop_pct * 100))}% "
#         f"zoom={int(round(zoom_pct * 100))}% "
#         f"speed={speed:.2f}x"
#     )
#     return {"vf": vf, "af": af, "summary": summary}

# def build_distortion_filter() -> Optional[Dict[str, str]]:
#     """Return a subtle per-sub-clip distortion recipe: crop + handheld shake."""
#     if os.getenv("RECAP_DISTORTION_ENABLED", "1") != "1":
#         return None

#     import random

#     out_w = 1280
#     out_h = 720

#     # Crop only 2% to leave room for the shake.
#     crop_pct = 0.98
#     crop_w = int(out_w * crop_pct)
#     crop_h = int(out_h * crop_pct)

#     # Very subtle handheld shake.
#     shake_px = random.uniform(2.0, 4.0)

#     # vf = (
#     #     f"crop={crop_w}:{crop_h}:"
#     #     f"(iw-{crop_w})/2+random(0)*{shake_px:.1f}:"
#     #     f"(ih-{crop_h})/2+random(0)*{shake_px:.1f},"
#     #     f"scale={out_w}:{out_h}:flags=lanczos"
#     # )

#     vf = (
#     f"crop="
#     f"trunc(iw*0.98/2)*2:"
#     f"trunc(ih*0.98/2)*2:"
#     f"(iw-trunc(iw*0.98/2)*2)/2+random(0)*{shake:.1f}:"
#     f"(ih-trunc(ih*0.98/2)*2)/2+random(0)*{shake:.1f},"
#     "scale=1280:720:flags=lanczos"
# )

#     return {
#         "vf": vf,
#         "summary": f"crop=98% shake={shake_px:.1f}px",
#     }

def build_distortion_filter() -> Optional[Dict[str, str]]:
    """Return a subtle per-sub-clip distortion recipe: crop + handheld shake."""
    if os.getenv("RECAP_DISTORTION_ENABLED", "1") != "1":
        return None

    import random

    crop_pct = 0.98
    shake_px = random.uniform(1, 2)
    # shake_px = random.uniform(0.3, 0.8)

    vf = (
        f"crop="
        f"trunc(iw*{crop_pct}/2)*2:"
        f"trunc(ih*{crop_pct}/2)*2:"
        f"(iw-trunc(iw*{crop_pct}/2)*2)/2+(random(0)-0.5)*{shake_px:.1f}:"
        f"(ih-trunc(ih*{crop_pct}/2)*2)/2+(random(0)-0.5)*{shake_px:.1f},"
        "scale=1280:720:flags=lanczos"
    )

    return {
        "vf": vf,
        "summary": f"crop={crop_pct*100:.0f}% shake={shake_px:.1f}px",
    }

# working encode before chatgpt gave me
# async def _encode_clip(
#     ctx: Dict[str, Any],
#     start: float,
#     end: float,
#     dest: str,
#     vf: str,
#     label: Optional[str] = None,
# ) -> None:
#     """Cut [start, end] from the source and re-encode to mezzanine specs,
#     optionally prepending a random distortion filter for Content-ID variety.

#     Seek is INPUT-side (-ss before -i) for speed, paired with a DURATION-based
#     cut (-t, not -to) so the read length can't be misread as an absolute
#     position in the original timeline. setpts=PTS-STARTPTS / asetpts=PTS-STARTPTS
#     are appended as the LAST filter on each stream so every sub-clip's encoded
#     frames start at PTS 0 regardless of how far into the movie they were cut
#     from — without this reset, frames cut from e.g. the 45-minute mark can keep
#     PTS values around 2700s, which is what inflated concatenated durations to
#     8x actual length.
#     """
#     settings = ctx["payload"]["settings"]
#     distortion = build_distortion_filter()
#     duration = end - start

#     if distortion:
#         video_filters = f"{distortion['vf']},{vf},setpts=PTS-STARTPTS"
#         # Distortion recipes without a speed change (af=None) skip atempo —
#         # asetpts alone still resets audio PTS to 0.
#         audio_filters = (
#             f"{distortion['af']},asetpts=PTS-STARTPTS"
#             if distortion.get("af")
#             else "asetpts=PTS-STARTPTS"
#         )
#     else:
#         video_filters = f"{vf},setpts=PTS-STARTPTS"
#         audio_filters = "asetpts=PTS-STARTPTS"

#     args = [
#         "-ss", f"{start:.3f}",
#         "-i", ctx["movie_path"],
#         "-t", f"{duration:.3f}",
#         "-vf", video_filters,
#         "-af", audio_filters,
#         "-r", str(settings["fps"]),
#         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
#         "-c:a", "aac", "-ar", "44100", "-ac", "2",
#         "-movflags", "+faststart",
#         dest,
#     ]

#     await ffmpeg(args)

#     if distortion and label:
#         logger.info(
#             "%s: %.1f-%.1fs | distortion applied: %s",
#             label, start, end, distortion["summary"],
#         )


async def _encode_clip(
    ctx: Dict[str, Any],
    start: float,
    end: float,
    dest: str,
    vf: str,
    label: Optional[str] = None,
) -> None:
    """Cut [start, end] from the source and re-encode to mezzanine specs.

    Optionally applies a subtle crop + handheld shake to each clip for
    Content-ID variation. The original movie audio is discarded because
    narration and background music are added during the final encode.
    """

    settings = ctx["payload"]["settings"]
    distortion = build_distortion_filter()
    duration = end - start

    if distortion:
        video_filters = f"{distortion['vf']},{vf},setpts=PTS-STARTPTS"
    else:
        video_filters = f"{vf},setpts=PTS-STARTPTS"

    args = [
        "-ss", f"{start:.3f}",
        "-i", ctx["movie_path"],
        "-t", f"{duration:.3f}",
        "-vf", video_filters,
        "-an",  # Drop the original movie audio
        "-r", str(settings["fps"]),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-movflags", "+faststart",
        dest,
    ]

    await ffmpeg(args)

    if distortion and label:
        logger.info(
            "%s: %.1f-%.1fs | distortion applied: %s",
            label,
            start,
            end,
            distortion["summary"],
        )

async def _extract_and_concat(
    ctx: Dict[str, Any],
    seg: Dict[str, Any],
    subclips: List[Tuple[float, float]],
    vf: str,
) -> None:
    """Cut each sub-clip to a temp file and concat them into seg['clip_path'].

    Single sub-clip → skip the concat and emit the final path directly.

    The intra-segment concat re-encodes (not -c copy): sub-clips are small
    (a handful of 2-5s cuts) so the cost is negligible, and re-encoding here
    is extra insurance against any residual timestamp inconsistency between
    sub-clips instead of trusting stream-copy to staple them cleanly.
    """
    project_id = ctx["payload"]["project_id"]
    scratch = scratch_dir(project_id)
    expected_dur = sum(e - s for s, e in subclips)

    if len(subclips) == 1:
        s, e = subclips[0]
        label = f"seg {seg['id']:03d} clip 1"
        await _encode_clip(ctx, s, e, seg["clip_path"], vf, label=label)
    else:
        sub_paths: List[str] = []
        try:
            for i, (s, e) in enumerate(subclips):
                sub_path = os.path.join(
                    scratch, f"seg_{seg['id']:03d}_sub_{i:02d}.mp4"
                )
                label = f"seg {seg['id']:03d} clip {i + 1}"
                await _encode_clip(ctx, s, e, sub_path, vf, label=label)
                sub_paths.append(sub_path)

            concat_list = os.path.join(
                scratch, f"seg_{seg['id']:03d}_concat.txt"
            )
            with open(concat_list, "w") as f:
                for p in sub_paths:
                    f.write(f"file '{p}'\n")

            await ffmpeg(
                [
                    "-f", "concat", "-safe", "0", "-fflags", "+genpts",
                    "-i", concat_list,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-c:a", "aac", "-ar", "44100", "-ac", "2",
                    "-movflags", "+faststart",
                    seg["clip_path"],
                ]
            )
        finally:
            # Sub-clip temps are only useful for building the final; drop them.
            for p in sub_paths:
                try:
                    os.remove(p)
                except OSError:
                    pass
            list_path = os.path.join(scratch, f"seg_{seg['id']:03d}_concat.txt")
            try:
                os.remove(list_path)
            except OSError:
                pass

    # Diagnostic: catch corrupted timestamps at the source, not 20 minutes
    # later at the final concat. Warn-only — this never blocks the render.
    actual_dur = await media_duration(seg["clip_path"]) or 0.0
    if actual_dur > expected_dur * 1.5 + 2.0:
        logger.warning(
            "[%s] seg %03d: clip duration %.2fs far exceeds expected %.2fs — "
            "possible timestamp corruption at extraction",
            project_id, seg["id"], actual_dur, expected_dur,
        )


async def extract_clips(ctx: Dict[str, Any]) -> Dict[str, Any]:
    payload = ctx["payload"]
    project_id = payload["project_id"]
    scratch = scratch_dir(project_id)
    settings = payload["settings"]
    duration = float(ctx["movie_duration_sec"])

    boundaries: List[float] = ctx.get("scene_boundaries") or []
    # Scene detection helps both boundary-snapping and multi-cut. Run once,
    # cache in ctx; multi-cut works without it (falls back to even distribution).
    want_detection = RECAP_SCENE_SNAP or RECAP_MULTI_CUT
    if want_detection and not boundaries:
        try:
            boundaries = await asyncio.get_event_loop().run_in_executor(
                None, _detect_scenes_sync, ctx["movie_path"]
            )
            ctx["scene_boundaries"] = boundaries
            save_ctx(ctx)  # cache — detection is expensive
            logger.info("[%s] scene detection: found %d scenes", project_id, len(boundaries))
        except ImportError:
            logger.info(
                "[%s] scenedetect not installed — multi-cut will use even distribution",
                project_id,
            )
        except Exception as exc:
            logger.warning("[%s] scene detection failed: %s — continuing without", project_id, exc)

    if RECAP_SCENE_SNAP and boundaries:
        for seg in ctx["segments"]:
            seg["source_start"] = _snap(seg["source_start"], boundaries, duration)
            seg["source_end"] = _snap(seg["source_end"], boundaries, duration)
            if seg["source_end"] - seg["source_start"] < 2.0:
                seg["source_end"] = min(seg["source_start"] + 5.0, duration)

    vf = mezzanine_vf(settings)
    total_segments = len(ctx["segments"])
    for idx, seg in enumerate(ctx["segments"]):
        clip = os.path.join(scratch, f"seg_{seg['id']:03d}.mp4")
        seg["clip_path"] = clip
        if not (os.path.exists(clip) and os.path.getsize(clip) > 0):
            if RECAP_MULTI_CUT:
                narr_dur = _estimate_narration_duration(seg["narration"])
                # Aim for narr_dur * headroom of footage, but never past source_end
                # — the picker itself caps every sub-clip to the window, and the
                # tier logic decides sub-clip count from this padded target.
                window = seg["source_end"] - seg["source_start"]
                target_footage = min(narr_dur * FOOTAGE_HEADROOM, window)
                subclips = _pick_subclips(
                    seg["source_start"], seg["source_end"], target_footage, boundaries
                )
            else:
                subclips = [(seg["source_start"], seg["source_end"])]

            if len(subclips) > 1:
                picks_str = ", ".join(f"{s:.1f}-{e:.1f}" for s, e in subclips)
                total_footage = sum(e - s for s, e in subclips)
                logger.info(
                    "[%s] seg %03d: source %.1f-%.1fs (%.1fs), narr ~%.1fs, footage %.1fs → %d sub-clips [%s]",
                    project_id, seg["id"],
                    seg["source_start"], seg["source_end"],
                    seg["source_end"] - seg["source_start"],
                    _estimate_narration_duration(seg["narration"]),
                    total_footage,
                    len(subclips), picks_str,
                )
            else:
                s, e = subclips[0]
                logger.info(
                    "[%s] seg %03d: source %.1f-%.1fs → 1 clip [%.1f-%.1f]",
                    project_id, seg["id"],
                    seg["source_start"], seg["source_end"], s, e,
                )

            await _extract_and_concat(ctx, seg, subclips, vf)

        # Fires whether the segment was freshly extracted or resumed from
        # disk, so progress never stalls on a resumed job. Segment-level
        # granularity only — never per sub-clip.
        seg_percent = 15 + (35 * (idx + 1) / total_segments)
        await report_progress(
            payload,
            "extract_clips",
            "Extracting clips",
            seg_percent,
            f"Extracting clips ({idx + 1}/{total_segments})",
            current=idx + 1,
            total=total_segments,
        )

    save_ctx(ctx)
    return ctx
