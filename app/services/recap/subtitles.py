"""
Step 1 — resolve_subtitles.

Download the movie, obtain dialogue by the cheapest route: provided .srt →
embedded text-subtitle stream (prefers English subrip/ass) → Whisper
transcription (reuses the repo's transcription_service models). Cues are merged
into ~30–60s dialogue blocks to cut LLM token noise.

ctx in:  {payload}
ctx out: + {movie_path, movie_duration_sec, dialogue_blocks}
"""
import asyncio
import logging
import os
import re
from typing import Any, Dict, List, Optional

from app.services.recap.config import RECAP_WHISPER_MODEL
from app.services.recap.utils import (
    download_source,
    ffmpeg,
    ffprobe_json,
    media_duration,
    save_ctx,
    scratch_dir,
)

logger = logging.getLogger(__name__)

BLOCK_MIN_SEC = 30.0
BLOCK_MAX_SEC = 60.0

_SRT_TIME = re.compile(
    r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d{1,3})"
)


def _srt_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_srt(text: str) -> List[Dict[str, Any]]:
    """Minimal SRT parser -> [{start_sec, end_sec, text}]."""
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        match = None
        text_lines: List[str] = []
        for line in lines:
            m = _SRT_TIME.search(line)
            if m and match is None:
                match = m
            elif match is not None:
                text_lines.append(line)
        if match is None or not text_lines:
            continue
        g = match.groups()
        content = " ".join(text_lines)
        # Strip HTML-ish tags and ASS override blocks common in subs.
        content = re.sub(r"<[^>]+>|\{[^}]*\}", "", content).strip()
        if content:
            cues.append(
                {
                    "start_sec": _srt_seconds(*g[:4]),
                    "end_sec": _srt_seconds(*g[4:]),
                    "text": content,
                }
            )
    return cues


async def _find_embedded_subtitle_stream(movie: str) -> Optional[int]:
    """Subtitle-relative index of the best text-based stream, or None."""
    info = await ffprobe_json(movie)
    text_codecs = {"subrip", "ass", "ssa", "mov_text"}
    candidates = []
    sub_index = -1
    for stream in info.get("streams", []):
        if stream.get("codec_type") != "subtitle":
            continue
        sub_index += 1
        if stream.get("codec_name") not in text_codecs:
            continue  # bitmap subs (pgs/dvdsub) can't become text directly
        lang = (stream.get("tags") or {}).get("language", "").lower()
        candidates.append((0 if lang in ("eng", "en") else 1, sub_index))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][1]


async def _transcribe_movie(movie: str) -> List[Dict[str, Any]]:
    """Whisper fallback via the repo's transcription service (model cache +
    thread pool). Returns cue dicts directly — no SRT round-trip needed."""
    from app.services.media.transcription import transcription_service

    logger.info("transcribing %s with whisper %s", movie, RECAP_WHISPER_MODEL)
    model = transcription_service._get_model(RECAP_WHISPER_MODEL)
    result = await asyncio.get_event_loop().run_in_executor(
        transcription_service.executor,
        lambda: model.transcribe(movie, verbose=False),
    )
    return [
        {
            "start_sec": seg["start"],
            "end_sec": seg["end"],
            "text": seg["text"].strip(),
        }
        for seg in result.get("segments", [])
        if seg.get("text", "").strip()
    ]


def merge_cues_into_blocks(
    cues: List[Dict[str, Any]],
    min_sec: float = BLOCK_MIN_SEC,
    max_sec: float = BLOCK_MAX_SEC,
) -> List[Dict[str, Any]]:
    """Merge subtitle cues into ~30–60s dialogue blocks."""
    blocks: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for cue in cues:
        if current is None:
            current = dict(cue)
            continue
        if cue["end_sec"] - current["start_sec"] <= max_sec:
            current["end_sec"] = cue["end_sec"]
            current["text"] += " " + cue["text"]
        else:
            blocks.append(current)
            current = dict(cue)
    if current is not None:
        blocks.append(current)

    if len(blocks) >= 2 and blocks[-1]["end_sec"] - blocks[-1]["start_sec"] < min_sec / 2:
        tail = blocks.pop()
        blocks[-1]["end_sec"] = tail["end_sec"]
        blocks[-1]["text"] += " " + tail["text"]
    return blocks


async def resolve_subtitles(ctx: Dict[str, Any]) -> Dict[str, Any]:
    payload = ctx["payload"]
    project_id = payload["project_id"]
    scratch = scratch_dir(project_id)
    source = payload["source"]

    ext = os.path.splitext(source.get("movie_s3_key") or "movie.mkv")[1] or ".mkv"
    movie = os.path.join(scratch, f"movie{ext}")
    if not (os.path.exists(movie) and os.path.getsize(movie) > 0):
        await download_source(source, "movie_url", "movie_s3_key", movie)

    duration = source.get("duration_sec") or await media_duration(movie)
    if not duration:
        raise RuntimeError("resolve_subtitles: could not determine movie duration")

    cues: List[Dict[str, Any]] = []
    if source.get("srt_s3_key") or source.get("srt_url"):
        logger.info("[%s] using provided .srt", project_id)
        srt_path = os.path.join(scratch, "subs.srt")
        await download_source(source, "srt_url", "srt_s3_key", srt_path)
        with open(srt_path, encoding="utf-8", errors="replace") as f:
            cues = parse_srt(f.read())
    else:
        sub_stream = await _find_embedded_subtitle_stream(movie)
        if sub_stream is not None:
            logger.info("[%s] extracting embedded subtitle stream s:%d", project_id, sub_stream)
            srt_path = os.path.join(scratch, "subs.srt")
            await ffmpeg(["-i", movie, "-map", f"0:s:{sub_stream}", srt_path])
            with open(srt_path, encoding="utf-8", errors="replace") as f:
                cues = parse_srt(f.read())
        if not cues:
            logger.info("[%s] no usable subtitles — falling back to whisper", project_id)
            cues = await _transcribe_movie(movie)

    if not cues:
        raise RuntimeError("resolve_subtitles: no usable dialogue found")

    blocks = merge_cues_into_blocks(cues)
    logger.info(
        "[%s] %d cues -> %d dialogue blocks (%.0fs movie)",
        project_id, len(cues), len(blocks), duration,
    )

    ctx.update(movie_path=movie, movie_duration_sec=float(duration), dialogue_blocks=blocks)
    save_ctx(ctx)
    return ctx
