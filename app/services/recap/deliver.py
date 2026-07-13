"""
Step 6 — upload_and_callback.

Upload the final MP4 to S3 (recaps/{project_id}.mp4) via the repo's
storage_manager, then POST the result to the Recap Studio callback URL.
Also hosts the shared error callback used when any step fails.

ctx in:  {payload, final_path, segments, seo}
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional

import aiohttp

from app.services.recap.config import PIPELINE_SHARED_SECRET
from app.services.recap.utils import cleanup_scratch
from app.utils.storage import storage_manager

logger = logging.getLogger(__name__)


def _segments_for_callback(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "segment_number": s["id"],
            "narration_text": s["narration"],
            "source_start_sec": s["source_start"],
            "source_end_sec": s["source_end"],
            "tts_duration_sec": s.get("tts_duration_sec"),
        }
        for s in segments
    ]


async def _post_callback(callback_url: str, body: Dict[str, Any]) -> None:
    headers = {}
    if PIPELINE_SHARED_SECRET:
        headers["X-Pipeline-Secret"] = PIPELINE_SHARED_SECRET
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(callback_url, json=body, headers=headers) as resp:
            if resp.status >= 300:
                text = await resp.text()
                raise RuntimeError(f"callback returned {resp.status}: {text[:300]}")


async def report_progress(
    payload: Dict[str, Any],
    step: str,
    step_label: str,
    percent: float,
    message: str,
    current: Optional[int] = None,
    total: Optional[int] = None,
) -> None:
    """Best-effort progress ping to Recap Studio's progress_url.

    NEVER raises — a failed or missing progress_url must not interrupt the
    render. Short 3s timeout so an unreachable progress endpoint can't stall
    the pipeline. Uses the same X-Pipeline-Secret header as the final
    callback for consistency.
    """
    progress_url = payload.get("progress_url")
    if not progress_url:
        return  # older Recap Studio version without progress support — skip silently

    body = {
        "step": step,
        "step_label": step_label,
        "percent": max(0.0, min(100.0, percent)),
        "message": message,
        "current": current,
        "total": total,
    }

    headers = {"Content-Type": "application/json"}
    if PIPELINE_SHARED_SECRET:
        headers["X-Pipeline-Secret"] = PIPELINE_SHARED_SECRET

    try:
        timeout = aiohttp.ClientTimeout(total=3)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(progress_url, json=body, headers=headers) as resp:
                if resp.status >= 400:
                    logger.debug(
                        "progress ping non-200 (%s) for step=%s — ignoring",
                        resp.status, step,
                    )
    except Exception as e:
        # Never let a progress ping failure affect the render.
        logger.debug("progress ping failed for step=%s: %s", step, e)


async def upload_and_callback(ctx: Dict[str, Any]) -> Dict[str, Any]:
    payload = ctx["payload"]
    project_id = payload["project_id"]

    success = False
    try:
        s3_key = f"recaps/{project_id}.mp4"
        url = await asyncio.get_event_loop().run_in_executor(
            None, storage_manager.upload_file, ctx["final_path"], s3_key
        )
        seo = ctx.get("seo") or {}
        body = {
            "status": "done",
            "final_video_url": url,
            "generated_title": seo.get("title"),
            "generated_description": seo.get("description"),
            "generated_tags": seo.get("tags"),
            "segments": _segments_for_callback(ctx["segments"]),
        }
        await _post_callback(payload["callback_url"], body)
        logger.info("[%s] delivered: %s", project_id, url.split("?")[0])
        success = True
        return {"project_id": project_id, "final_video_url": url, "s3_key": s3_key}
    finally:
        if success:
            cleanup_scratch(project_id)
        else:
            logger.warning(
                "[%s] delivery failed — preserving scratch dir for diagnosis",
                project_id,
            )


async def report_error(payload: Dict[str, Any], step_name: str, error: Any) -> None:
    """Tell Recap Studio the render failed. Never raises. Scratch is preserved
    for diagnosis — do NOT clean up here; upload_and_callback owns cleanup and
    only runs it on success.
    """
    project_id = payload.get("project_id", "?")
    try:
        await _post_callback(
            payload["callback_url"],
            {"status": "error", "error": f"{step_name}: {error}"},
        )
        logger.info("[%s] error callback sent (%s)", project_id, step_name)
    except Exception:
        logger.exception("[%s] error callback itself failed", project_id)
    logger.warning(
        "[%s] render failed at step=%s — scratch preserved at %s/%s",
        project_id, step_name, "RECAP_SCRATCH_ROOT", project_id,
    )
