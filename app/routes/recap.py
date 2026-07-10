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
    for field in ("project_id", "callback_url", "source", "recap", "settings"):
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

    # Manual mode: require at least 10 segments
    if payload.get("script_mode") == "manual":
        segs = payload.get("segments", [])
        if not isinstance(segs, list) or len(segs) < 10:
            raise HTTPException(
                status_code=400,
                detail=f"Manual mode requires at least 10 segments, got {len(segs) if isinstance(segs, list) else 0}",
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
