"""
Routes for movie recap rendering (consumes the Recap Studio webhook payload).

Endpoints:
- POST /recap/create - Accept a Recap Studio payload and start the render chain
- GET /recap/{job_id}/status - Check job status
"""
import logging
import uuid
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, status

from app.models import JobStatusResponse, JobType
from app.services.job_queue import job_queue
from app.services.recap.service import process_recap_job

# Configure logging
logger = logging.getLogger(__name__)

# Create router
router = APIRouter(prefix="/recap", tags=["recap"])


@router.post("/create", status_code=status.HTTP_202_ACCEPTED)
async def create_recap(payload: Dict[str, Any]):
    """
    Start a recap render job.

    Accepts exactly the Recap Studio dispatch payload (§6 of the spec):
    project_id, callback_url, title, source{movie_s3_key|movie_url, srt_*,
    duration_sec}, recap{target_length_min, narration_style, voice_id,
    voice_speed}, settings{resolution, fps, aspect_ratio, background_music*,
    original_audio_volume, captions_*}.

    The chain runs asynchronously; the result (or error) is POSTed to
    payload.callback_url. This endpoint only validates and enqueues.
    """
    manual_mode = payload.get("script_mode") == "manual"
    # In manual mode the LLM-driven `recap` block is unused, so it's optional.
    required_top = ("project_id", "callback_url", "source", "settings")
    if not manual_mode:
        required_top = required_top + ("recap",)
    for field in required_top:
        if not payload.get(field):
            raise HTTPException(
                status_code=400,
                detail=f"Missing required payload field: {field}",
            )
    source = payload["source"]
    if not source.get("movie_s3_key") and not source.get("movie_url"):
        raise HTTPException(
            status_code=400,
            detail="payload.source needs movie_s3_key or movie_url",
        )

    if manual_mode:
        segments = payload.get("segments")
        if not isinstance(segments, list) or len(segments) < 10:
            raise HTTPException(
                status_code=400,
                detail="script_mode=manual requires 'segments' to be a list with at least 10 items",
            )
        for i, seg in enumerate(segments, start=1):
            if not isinstance(seg, dict):
                raise HTTPException(
                    status_code=400, detail=f"segment {i}: expected object"
                )
            narration = seg.get("narration")
            if not isinstance(narration, str) or not narration.strip():
                raise HTTPException(
                    status_code=400,
                    detail=f"segment {i}: 'narration' must be a non-empty string",
                )
            try:
                start = float(seg["source_start"])
                end = float(seg["source_end"])
            except (KeyError, TypeError, ValueError):
                raise HTTPException(
                    status_code=400,
                    detail=f"segment {i}: 'source_start' and 'source_end' must be numbers",
                )
            if start < 0 or end <= start:
                raise HTTPException(
                    status_code=400,
                    detail=f"segment {i}: require source_start >= 0 and source_end > source_start",
                )

    job_id = str(uuid.uuid4())
    try:
        await job_queue.add_job(job_id, JobType.RECAP, process_recap_job, payload)
    except ValueError as e:
        # Queue full
        raise HTTPException(status_code=429, detail=str(e))

    logger.info("Created recap job %s for project %s", job_id, payload["project_id"])
    return {
        "job_id": job_id,
        "project_id": payload["project_id"],
        "status_url": f"/recap/{job_id}/status",
    }


@router.get("/{job_id}/status", response_model=JobStatusResponse)
async def get_recap_status(job_id: str):
    """Get the status of a recap render job."""
    job = job_queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return JobStatusResponse(
        job_id=job.id,
        status=job.status,
        result=job.result,
        error=job.error,
    )


@router.post("/{job_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_recap(job_id: str):
    """Cancel an in-progress recap render job.

    Returns 202 immediately — cancellation is cooperative (the render loop
    stops between chain steps rather than being killed mid-FFmpeg-call), so
    it may take a moment to actually stop.

    404 covers both "never existed" and "already finished/gone" (including
    jobs from before a container restart, since in-memory job state doesn't
    survive one) — Recap Studio should treat either as "already gone, just
    reset locally" rather than surfacing an error to the user.
    """
    cancelled = await job_queue.cancel_job(job_id)
    if not cancelled:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found or already finished")
    return {"job_id": job_id, "status": "cancelled"}
