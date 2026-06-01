"""
Routes for the Reddit render channel.

Endpoints:
- POST /v1/render/reddit              — start a reddit render job
- GET  /v1/render/reddit/{job_id}/status — poll job status
"""
import logging
import uuid

from fastapi import APIRouter, HTTPException, status

from app.models import (
    RedditJobStatus,
    RedditRenderRequest,
    RedditRenderResponse,
)
from app.services.backgrounds import list_background_keys
from app.services.reddit_render import reddit_render_service


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/render/reddit", tags=["render"])


@router.post("", response_model=RedditRenderResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_reddit_render_job(request: RedditRenderRequest):
    """
    Start a reddit-channel render job.

    Pipeline: Kokoro TTS → loop background video → (optional) burn captions
    → (optional) mix background music → upload to S3 → fire webhook.
    """
    if request.background not in list_background_keys():
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown background '{request.background}'. "
                f"Available: {list_background_keys()}"
            ),
        )

    job_id = str(uuid.uuid4())
    params = request.dict(exclude_none=True)

    try:
        await reddit_render_service.start_render(job_id, params)
    except Exception as e:
        logger.error(f"Error starting reddit render job: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to start reddit render job: {e}",
        )

    logger.info(f"Started reddit render job {job_id} (background={request.background})")

    return RedditRenderResponse(
        job_id=job_id,
        status="pending",
        monitor_url=f"/v1/render/reddit/{job_id}/status",
    )


@router.get("/{job_id}/status", response_model=RedditJobStatus)
async def get_reddit_render_job_status(job_id: str):
    """Poll status of a reddit render job."""
    info = await reddit_render_service.get_job_status(job_id)
    if not info:
        raise HTTPException(status_code=404, detail=f"Reddit render job {job_id} not found")
    return RedditJobStatus(**info)
