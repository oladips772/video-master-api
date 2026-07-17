"""
Recap render chain orchestrator.

Runs the six steps sequentially inside one job_queue job (the queue's
in-process asyncio model replaces the Celery chain from the reference
implementation — same order, same ctx handoff):

    resolve_subtitles → generate_script → extract_clips
        → generate_tts → assemble → upload_and_callback

Manual-script mode (payload.script_mode == "manual" with payload.segments):
skip subtitles + LLM script and replace with prepare_manual, which downloads
the movie and adopts the provided segments directly:

    prepare_manual → extract_clips → generate_tts → assemble → upload_and_callback

Any step failure POSTs {status:"error", error:"<step>: <msg>"} to the Recap
Studio callback and cleans the scratch dir before re-raising so the job is
marked FAILED with the traceback.
"""
import asyncio
import logging
import os
from typing import Any, Dict

from app.models import JobStatus
from app.services.job_queue import job_queue
from app.services.recap.assemble import assemble
from app.services.recap.clips import extract_clips
from app.services.recap.deliver import report_error, report_progress, upload_and_callback
from app.services.recap.script_gen import generate_script
from app.services.recap.subtitles import resolve_subtitles
from app.services.recap.tts import generate_tts
from app.services.recap.utils import (
    download_source,
    media_duration,
    save_ctx,
    scratch_dir,
)

logger = logging.getLogger(__name__)

# Coarse step-level progress: (step_label, percent, message). Fired once at
# the START of each step, before it runs. Fine-grained per-segment pings
# happen inside clips.py (15→50%) and tts.py (50→82%); assemble.py fires an
# additional "final_encode" sub-phase ping (88%) before the heavy final pass.
STEP_PROGRESS: Dict[str, tuple] = {
    "resolve_subtitles": ("Preparing subtitles", 2, "Preparing subtitles"),
    "generate_script": ("Writing script", 8, "Writing script"),
    "prepare_manual": ("Preparing segments", 5, "Preparing manual segments"),
    "extract_clips": ("Extracting clips", 15, "Extracting clips"),
    "generate_tts": ("Generating narration", 50, "Generating narration"),
    "assemble": ("Assembling video", 82, "Muxing and concatenating segments"),
    "upload_and_callback": ("Uploading", 98, "Uploading finished video"),
}


async def prepare_manual(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Manual-mode substitute for resolve_subtitles + generate_script.

    Downloads the movie (required for clip extraction), infers duration, and
    adopts the caller-supplied segments and optional SEO block as the ctx the
    downstream extract_clips → tts → assemble pipeline expects.
    """
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
        raise RuntimeError("prepare_manual: could not determine movie duration")

    segments = [
        {
            "id": int(s.get("id") or (i + 1)),
            "narration": str(s["narration"]).strip(),
            "source_start": max(0.0, min(float(s["source_start"]), float(duration))),
            "source_end": max(0.0, min(float(s["source_end"]), float(duration))),
        }
        for i, s in enumerate(payload["segments"])
    ]
    # Enforce ordering + minimum window; drop degenerates.
    segments.sort(key=lambda s: (s["source_start"], s["source_end"]))
    segments = [s for s in segments if s["source_end"] > s["source_start"]]

    seo = dict(payload.get("seo") or {})
    seo["title"] = (seo.get("title") or payload.get("title") or "")[:95]
    seo["tags"] = (seo.get("tags") or [])[:20]

    logger.info(
        "[%s] manual mode: %d segments (%.0fs movie)",
        project_id, len(segments), float(duration),
    )
    ctx.update(
        movie_path=movie,
        movie_duration_sec=float(duration),
        segments=segments,
        seo=seo,
    )
    save_ctx(ctx)
    return ctx


STEPS = [
    ("resolve_subtitles", resolve_subtitles),
    ("generate_script", generate_script),
    ("extract_clips", extract_clips),
    ("generate_tts", generate_tts),
    ("assemble", assemble),
    ("upload_and_callback", upload_and_callback),
]

MANUAL_STEPS = [
    ("prepare_manual", prepare_manual),
    ("extract_clips", extract_clips),
    ("generate_tts", generate_tts),
    ("assemble", assemble),
    ("upload_and_callback", upload_and_callback),
]


def _is_manual_mode(payload: Dict[str, Any]) -> bool:
    return (
        payload.get("script_mode") == "manual"
        and isinstance(payload.get("segments"), list)
        and len(payload["segments"]) > 0
    )


async def process_recap_job(job_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Job entrypoint for job_queue.add_job."""
    project_id = payload.get("project_id")
    if not project_id:
        raise ValueError("payload.project_id is required")
    if not payload.get("callback_url"):
        raise ValueError("payload.callback_url is required")
    if not (payload.get("source") or {}).get("movie_s3_key") and not (
        payload.get("source") or {}
    ).get("movie_url"):
        raise ValueError("payload.source needs movie_s3_key or movie_url")

    # Stashed so downstream steps (clips.py/tts.py/assemble.py) can check
    # cancellation at finer-than-per-step granularity without changing their
    # uniform step(ctx) signature.
    ctx: Dict[str, Any] = {"payload": payload, "job_id": job_id}
    save_ctx(ctx)

    steps = MANUAL_STEPS if _is_manual_mode(payload) else STEPS
    logger.info(
        "job=%s project=%s mode=%s",
        job_id, project_id, "manual" if steps is MANUAL_STEPS else "auto",
    )

    for step_name, step in steps:
        # Cancellation checkpoint — cancel_job() sets status=CANCELLED
        # synchronously (and already fired the caller's notification) before
        # cancelling the underlying asyncio Task, so checking here lets us
        # stop cleanly between steps without starting more FFmpeg work.
        # Raising CancelledError (not Exception) skips the except block below
        # so we don't fire a second, redundant notification.
        job = job_queue.get_job(job_id)
        if job and job.status == JobStatus.CANCELLED:
            logger.info(
                "job=%s project=%s cancelled before step=%s", job_id, project_id, step_name
            )
            raise asyncio.CancelledError(f"cancelled before step={step_name}")

        logger.info("job=%s project=%s step=%s", job_id, project_id, step_name)
        step_label, percent, message = STEP_PROGRESS.get(
            step_name, (step_name, 0, step_name)
        )
        await report_progress(payload, step_name, step_label, percent, message)
        try:
            result = await step(ctx)
        except Exception as exc:
            logger.exception(
                "job=%s project=%s step=%s failed", job_id, project_id, step_name
            )
            await report_error(payload, step_name, exc)
            raise

        if step_name == "upload_and_callback":
            return result  # final step returns the delivery summary, not ctx
        ctx = result

    raise RuntimeError("recap chain ended without delivery")  # unreachable
