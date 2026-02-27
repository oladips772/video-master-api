"""
Routes for multi-scene video rendering.

Endpoints:
- POST /v1/render - Start a new multi-scene render job
- GET /v1/render/{job_id}/status - Check job status and progress
- POST /v1/render/{job_id}/retry - Retry failed scenes
"""
import logging
import uuid
from fastapi import APIRouter, HTTPException, status

from app.models import (
    RenderRequest,
    RenderResponse,
    RenderJobStatus,
    RenderRetryRequest,
    SceneProgress,
    JobStatus
)
from app.services.render import render_service

# Configure logging
logger = logging.getLogger(__name__)

# Create router
router = APIRouter(prefix="/v1/render", tags=["render"])


@router.post("", response_model=RenderResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_render_job(request: RenderRequest):
    """
    Start a new multi-scene video render job.
    
    This endpoint accepts a project with multiple scenes and starts an asynchronous
    rendering pipeline that:
    1. Generates images from prompts using Kie.ai (Flux-2 Pro)
    2. Generates voiceovers using Kokoro TTS
    3. Creates videos (Ken Burns or animated)
    4. Assembles scenes with audio and subtitles
    5. Concatenates scenes with transitions
    6. Adds background music
    7. Uploads final video to S3
    
    Args:
        request: RenderRequest containing:
            - project_name: Name of the project
            - channel: 'kenburns' or 'animated'
            - settings: Global video settings (resolution, FPS, background music, etc.)
            - scenes: List of scenes with prompts, narration, voice, effects
            - webhook_url: Optional webhook to notify on completion
    
    Returns:
        RenderResponse with job_id and status URL
        
    Examples:
        ```
        POST /v1/render
        {
            "project_name": "My Story",
            "channel": "kenburns",
            "webhook_url": "https://example.com/webhook",
            "settings": {
                "aspect_ratio": "9:16",
                "resolution": "1K",
                "fps": 30,
                "background_music": "https://example.com/music.mp3",
                "background_music_volume": 0.12,
                "subtitle_enabled": true,
                "subtitle_style": "bold_center",
                "transition_type": "crossfade",
                "transition_duration_ms": 500
            },
            "scenes": [
                {
                    "scene_number": 1,
                    "image_prompt": "A beautiful sunset over mountains...",
                    "narration_text": "Once upon a time...",
                    "voice_id": "af_heart",
                    "pan_direction": "right",
                    "ken_burns_keypoints": [
                        {"x": 0.5, "y": 0.4, "zoom": 1.0},
                        {"x": 0.3, "y": 0.5, "zoom": 1.2}
                    ]
                }
            ]
        }
        ```
    """
    try:
        # Validate input
        if not request.scenes:
            raise HTTPException(
                status_code=400,
                detail="At least one scene is required"
            )
        
        if request.channel not in ["kenburns", "animated"]:
            raise HTTPException(
                status_code=400,
                detail="Channel must be 'kenburns' or 'animated'"
            )
        
        if len(request.scenes) > 100:
            raise HTTPException(
                status_code=400,
                detail="Maximum 100 scenes per render job"
            )
        
        # Validate scenes have required fields
        for i, scene in enumerate(request.scenes, 1):
            if not scene.image_prompt:
                raise HTTPException(
                    status_code=400,
                    detail=f"Scene {i} missing required field: image_prompt"
                )
            if not scene.narration_text:
                raise HTTPException(
                    status_code=400,
                    detail=f"Scene {i} missing required field: narration_text"
                )
        
        # Generate job ID
        job_id = str(uuid.uuid4())
        
        # Convert request to dict for service
        render_params = {
            "project_name": request.project_name,
            "channel": request.channel,
            "webhook_url": request.webhook_url,
            "settings": request.settings.dict(exclude_none=True),
            "scenes": [scene.dict(exclude_none=True) for scene in request.scenes]
        }
        
        # Start rendering
        await render_service.start_render(job_id, render_params)
        
        logger.info(f"Started render job {job_id} with {len(request.scenes)} scenes")
        
        return RenderResponse(
            job_id=job_id,
            status="pending",
            total_scenes=len(request.scenes),
            monitor_url=f"/v1/render/{job_id}/status"
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating render job: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create render job: {str(e)}"
        )


@router.get("/{job_id}/status", response_model=RenderJobStatus)
async def get_render_job_status(job_id: str):
    """
    Get the current status and progress of a render job.
    
    Returns detailed progress information including:
    - Overall job status and progress percentage
    - Per-scene status and progress
    - Final video URL when completed
    - Error details if failed
    
    Args:
        job_id: The render job ID
        
    Returns:
        RenderJobStatus with detailed progress information
        
    Raises:
        HTTPException: If job not found
    """
    status_info = await render_service.get_job_status(job_id)
    
    if not status_info:
        raise HTTPException(
            status_code=404,
            detail=f"Render job {job_id} not found"
        )
    
    # Convert to response model
    return RenderJobStatus(
        job_id=status_info["job_id"],
        status=status_info["status"],
        total_scenes=status_info["total_scenes"],
        completed_scenes=status_info["completed_scenes"],
        failed_scenes=status_info["failed_scenes"],
        progress_percent=status_info["progress_percent"],
        scenes=[SceneProgress(**s) for s in status_info["scenes"]],
        final_video_url=status_info.get("final_video_url"),
        final_file_size=status_info.get("final_file_size"),
        error=status_info.get("error"),
        created_at="",  # TODO: Add timestamps to job tracking
        updated_at=""
    )


@router.post("/{job_id}/retry", status_code=status.HTTP_200_OK)
async def retry_render_job(job_id: str, request: RenderRetryRequest = None):
    """
    Retry failed scenes in a render job.
    
    Can retry all failed scenes or specific scene numbers.
    Scenes are reset to 'pending' state and reprocessed.
    
    Args:
        job_id: The render job ID
        request: Optional request with list of specific scene numbers to retry.
                If not provided, all failed scenes are retried.
    
    Returns:
        Dictionary with retry status
        
    Raises:
        HTTPException: If job not found or has no failed scenes
        
    Examples:
        ```
        # Retry all failed scenes
        POST /v1/render/abc123/retry
        
        # Retry specific scenes
        POST /v1/render/abc123/retry
        {
            "failed_scene_numbers": [2, 5, 8]
        }
        ```
    """
    try:
        scene_numbers = request.failed_scene_numbers if request else None
        
        success = await render_service.retry_scenes(job_id, scene_numbers)
        
        if not success:
            raise HTTPException(
                status_code=404,
                detail=f"Render job {job_id} not found or has no scenes to retry"
            )
        
        logger.info(f"Retry started for job {job_id}")
        
        return {
            "job_id": job_id,
            "status": "retry_started",
            "message": "Failed scenes have been queued for retry"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrying render job: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retry render job: {str(e)}"
        )
