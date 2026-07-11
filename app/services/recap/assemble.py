"""
Step 5 — assemble.

Per segment: narration over the clip with the movie's own audio at
`original_audio_volume`, sidechain-ducked under speech. Concat everything
(uniform mezzanine → concat demuxer, lossless), music bed with fades ducked
under the program audio, captions burned in, final quality encode.

Captions REUSE the repo's ASS system (app.utils.captions): we build word-level
timestamps (proportional spread across each segment's measured TTS duration,
offset by the segment's position in the final timeline) and feed them to
create_srt_from_word_timestamps with the payload's caption_properties — the §6
property names (font_family, font_size, line_color, word_color, outline_color,
bold, position) are exactly what prepare_subtitle_styling expects.

ctx in:  {payload, segments (clip_path + tts_path), seo}
ctx out: + {final_path}
"""
import logging
import os
from typing import Any, Dict, List, Optional

from app.utils.captions import create_srt_from_word_timestamps
from app.services.recap.config import (
    RECAP_WATERMARK_OPACITY,
    RECAP_WATERMARK_SIZE,
    RECAP_WATERMARK_TEXT,
)
from app.services.recap.utils import (
    download_url,
    ffmpeg,
    media_duration,
    save_ctx,
    scratch_dir,
)

logger = logging.getLogger(__name__)

# Arial Bold ships in the repo's fonts/ dir and is installed under
# /usr/share/fonts/truetype/custom/ in the api container (see Dockerfile).
# Fall back to fontconfig (font=Arial) if the file happens not to be there.
_WATERMARK_FONT_FILE = "/usr/share/fonts/truetype/custom/ARIALBD.TTF"


def _escape_drawtext(text: str) -> str:
    """Escape text for FFmpeg drawtext (single-quoted form)."""
    return (
        text.replace("\\", r"\\").replace(":", r"\:").replace("'", r"\'")
    )


def _watermark_filter() -> str:
    """Return the drawtext filter for the configured watermark, or empty string."""
    if not RECAP_WATERMARK_TEXT:
        return ""
    text = _escape_drawtext(RECAP_WATERMARK_TEXT)
    if os.path.exists(_WATERMARK_FONT_FILE):
        font_part = f"fontfile='{_WATERMARK_FONT_FILE}'"
    else:
        font_part = "font=Arial"
    return (
        f"drawtext={font_part}:text='{text}'"
        f":fontsize={RECAP_WATERMARK_SIZE}"
        f":fontcolor=white@{RECAP_WATERMARK_OPACITY}"
        f":x=w-tw-20:y=h-th-20"
    )


NARRATION_GAIN = 1.5
ORIGINAL_VOLUME_CAP = 0.15


async def _mux_segment(seg: Dict[str, Any], original_volume: float, dest: str) -> None:
    """Narration is king. Boost narration 1.5x; original movie audio at
    min(original_volume, 0.15), ducked hard under the narration via
    sidechaincompress. If original_volume <= 0, drop the movie audio entirely.
    """
    original_volume = min(max(float(original_volume), 0.0), ORIGINAL_VOLUME_CAP)

    if original_volume <= 0.0:
        # Fully muted: narration only, still boosted.
        filter_complex = f"[1:a]volume={NARRATION_GAIN}[aout]"
    else:
        filter_complex = (
            f"[0:a]volume={original_volume}[bed];"
            f"[1:a]volume={NARRATION_GAIN}[narr];"
            "[bed][narr]sidechaincompress=threshold=0.02:ratio=10:attack=5:release=100[ducked];"
            "[ducked][narr]amix=inputs=2:duration=first:normalize=0[aout]"
        )

    await ffmpeg(
        [
            "-i", seg["clip_path"],
            "-i", seg["tts_path"],
            "-filter_complex", filter_complex,
            "-map", "0:v",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            dest,
        ]
    )


async def _build_captions(ctx: Dict[str, Any]) -> Optional[str]:
    """Word-timed ASS via the repo's caption system. Returns the .ass path."""
    settings = ctx["payload"]["settings"]
    style = settings.get("caption_style", "highlight")
    props = dict(settings.get("caption_properties") or {})
    props["style"] = style
    max_words = props.get("max_words_per_line", 6)

    word_timestamps: List[Dict[str, Any]] = []
    timeline = 0.0  # segments are back-to-back after concat
    for seg in ctx["segments"]:
        # Advance the timeline by the REAL post-reconciliation clip length so
        # it includes the tail pad (_reconcile targets tts_duration + 0.4s).
        # Word timings inside the segment still spread across the bare TTS
        # duration — that's where speech actually plays.
        real_dur = await media_duration(seg["clip_path"]) or float(seg["tts_duration_sec"])
        tts_duration = float(seg["tts_duration_sec"])
        words = seg["narration"].split()
        if words:
            weights = [max(len(w), 2) for w in words]
            total_weight = sum(weights)
            t = 0.0
            for w, weight in zip(words, weights):
                dur = tts_duration * weight / total_weight
                word_timestamps.append(
                    {"word": w, "start": timeline + t, "end": timeline + t + dur}
                )
                t += dur
        timeline += real_dur

    if not word_timestamps:
        return None

    ass_path = await create_srt_from_word_timestamps(
        word_timestamps, timeline, max_words, style, props
    )
    logger.info("captions: %d words -> %s (style=%s)", len(word_timestamps), ass_path, style)
    return ass_path


async def _fetch_music(ctx: Dict[str, Any]) -> Optional[str]:
    """Download the background music to the scratch dir ONCE and return the
    local path. Looping a remote URL with -stream_loop -1 during the final
    encode buffers the network input unboundedly and gets FFmpeg OOM-killed
    (signal -9) on this box — loop a local file instead.

    Returns None (render proceeds without music) when no music is configured
    or the download fails."""
    settings = ctx["payload"]["settings"]
    music_url = settings.get("background_music")
    if not music_url:
        return None

    project_id = ctx["payload"]["project_id"]
    local = os.path.join(scratch_dir(project_id), "background_music.mp3")
    if os.path.exists(local) and os.path.getsize(local) > 0:
        return local
    try:
        await download_url(music_url, local)
        return local
    except Exception:
        logger.warning(
            "[%s] background music download failed — rendering without music",
            project_id,
            exc_info=True,
        )
        return None


async def _final_encode(
    ctx: Dict[str, Any], base: str, ass_path: Optional[str], dest: str
) -> None:
    """Music bed + caption burn + final quality encode in one pass."""
    settings = ctx["payload"]["settings"]
    music_path = await _fetch_music(ctx)
    music_volume = settings.get("background_music_volume", 0.07)
    total = await media_duration(base) or 0.0

    filters: List[str] = []
    if music_path:
        # -thread_queue_size bounds the looped input's queue; the local file
        # makes the loop cheap (no network buffering).
        args = [
            "-thread_queue_size", "512",
            "-stream_loop", "-1",
            "-i", music_path,
            "-i", base,
        ]
        base_a, music_a, video_in = "1:a", "0:a", "1:v"
        fade_out_start = max(0.0, total - 3.0)
        filters.append(
            f"[{music_a}]volume={music_volume},afade=t=in:d=2,"
            f"afade=t=out:st={fade_out_start:.2f}:d=3[music]"
        )
        filters.append(
            f"[music][{base_a}]sidechaincompress=threshold=0.03:ratio=6:attack=10:release=400[mducked]"
        )
        filters.append(f"[mducked][{base_a}]amix=inputs=2:duration=first:normalize=0[aout]")
        audio_map = "[aout]"
    else:
        args = ["-i", base]
        video_in, audio_map = "0:v", "0:a"

    video_filters: List[str] = []
    if ass_path:
        escaped = ass_path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        video_filters.append(f"subtitles='{escaped}'")
    watermark = _watermark_filter()
    if watermark:
        video_filters.append(watermark)
        logger.info("applying watermark: %s", RECAP_WATERMARK_TEXT)

    if video_filters:
        filters.append(f"[{video_in}]{','.join(video_filters)}[vout]")
        video_map = "[vout]"
    else:
        video_map = video_in

    if filters:
        args += ["-filter_complex", ";".join(filters)]
    args += ["-map", video_map, "-map", audio_map]

    await ffmpeg(
        [
            *args,
            "-c:v", "libx264", "-preset", "medium", "-crf", "19",
            "-c:a", "aac", "-b:a", "192k",
            # Resource bounds: shared VPS with limited RAM — cap encoder
            # threads and the muxing queue instead of letting them balloon.
            "-threads", "2",
            "-max_muxing_queue_size", "1024",
            "-movflags", "+faststart",
            "-shortest",
            dest,
        ]
    )


async def assemble(ctx: Dict[str, Any]) -> Dict[str, Any]:
    payload = ctx["payload"]
    project_id = payload["project_id"]
    scratch = scratch_dir(project_id)
    settings = payload["settings"]

    # 1) Per-segment narration mux.
    muxed: List[str] = []
    for seg in ctx["segments"]:
        dest = os.path.join(scratch, f"mux_{seg['id']:03d}.mp4")
        if not (os.path.exists(dest) and os.path.getsize(dest) > 0):
            await _mux_segment(seg, settings.get("original_audio_volume", 0.12), dest)
        muxed.append(dest)
    logger.info("[%s] muxed %d segments", project_id, len(muxed))

    # 2) Concat (lossless — uniform mezzanine).
    concat_list = os.path.join(scratch, "concat.txt")
    with open(concat_list, "w") as f:
        for p in muxed:
            f.write(f"file '{p}'\n")
    concatenated = os.path.join(scratch, "recap_concat.mp4")
    await ffmpeg(["-f", "concat", "-safe", "0", "-i", concat_list, "-c", "copy", concatenated])

    # 3) Captions (repo ASS system).
    ass_path = None
    if settings.get("captions_enabled"):
        ass_path = await _build_captions(ctx)

    # 4) Music + captions + final encode.
    final = os.path.join(scratch, "recap_final.mp4")
    await _final_encode(ctx, concatenated, ass_path, final)
    logger.info(
        "[%s] final render: %s (%.1fs)",
        project_id, final, await media_duration(final) or -1,
    )

    # The caption system writes its .ass into the repo temp dir — clean it.
    if ass_path and os.path.exists(ass_path):
        try:
            os.remove(ass_path)
        except OSError:
            pass

    ctx["final_path"] = final
    save_ctx(ctx)
    return ctx
