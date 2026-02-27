"""
Kie.ai service for image generation and animation.

Kie.ai provides:
1. Image generation (Flux-2 Pro model) - flux-2/pro-text-to-image
2. Image-to-video animation (Flux-2 Pro model) - flux-2/pro-image-to-video

Uses async task polling pattern:
- POST /jobs/createTask to submit a generation task
- GET /jobs/recordInfo?taskId=... to poll for completion
- Parse resultJson.resultUrls to get the generated media
"""
import os
import logging
import asyncio
import uuid
from typing import Optional
import aiohttp
import json

# Configure logging
logger = logging.getLogger(__name__)

# Kie.ai API configuration
KIE_AI_BASE_URL = os.environ.get("KIE_AI_BASE_URL", "https://api.kie.ai/api/v1")
KIE_AI_API_KEY = os.environ.get("KIE_AI_API_KEY")
KIE_AI_CONCURRENCY = int(os.environ.get("KIE_AI_CONCURRENCY", "5"))
KIE_AI_TIMEOUT = int(os.environ.get("KIE_AI_TIMEOUT", "300"))  # 5 minutes
KIE_AI_POLL_INTERVAL = int(os.environ.get("KIE_AI_POLL_INTERVAL", "5"))  # 5 seconds
KIE_AI_MAX_POLLS = int(KIE_AI_TIMEOUT / KIE_AI_POLL_INTERVAL)  # Max polling attempts

class KieAiError(Exception):
    """Base exception for Kie.ai errors."""
    pass


class KieAiTimeoutError(KieAiError):
    """Timeout error for Kie.ai operations."""
    pass


async def _make_request(method: str, endpoint: str, data: dict = None, timeout: int = 30) -> dict:
    """
    Make an HTTP request to Kie.ai API.
    
    Args:
        method: HTTP method (GET, POST)
        endpoint: API endpoint (without base URL)
        data: Request body data
        timeout: Request timeout in seconds
        
    Returns:
        Response JSON
        
    Raises:
        KieAiError: If request fails
    """
    if not KIE_AI_API_KEY:
        raise KieAiError("KIE_AI_API_KEY environment variable is not set")
    
    url = f"{KIE_AI_BASE_URL}{endpoint}"
    headers = {
        "Authorization": f"Bearer {KIE_AI_API_KEY}",
        "Content-Type": "application/json"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method,
                url,
                json=data,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as response:
                response_data = await response.json()
                
                # Check for error status
                if response.status >= 400:
                    error_msg = response_data.get("message") or response_data.get("error") or f"HTTP {response.status}"
                    logger.error(f"Kie.ai API error: {error_msg}")
                    raise KieAiError(f"Kie.ai API error: {error_msg}")
                
                # Check for API-level error in response code
                code = response_data.get("code")
                if code and code != 200:
                    error_msg = response_data.get("message", f"API returned code {code}")
                    logger.error(f"Kie.ai API error: {error_msg}")
                    raise KieAiError(f"Kie.ai API error: {error_msg}")
                
                return response_data
    except asyncio.TimeoutError:
        raise KieAiTimeoutError(f"Kie.ai API request timed out after {timeout}s")
    except aiohttp.ClientError as e:
        raise KieAiError(f"Failed to connect to Kie.ai: {e}")


async def _poll_task_status(task_id: str, max_polls: int = None) -> dict:
    """
    Poll a Kie.ai task until completion.
    
    Args:
        task_id: The task ID to poll
        max_polls: Maximum number of polls before timeout
        
    Returns:
        Task result data
        
    Raises:
        KieAiTimeoutError: If task doesn't complete within timeout
        KieAiError: If task fails or polling fails
    """
    if max_polls is None:
        max_polls = KIE_AI_MAX_POLLS
    
    for poll_count in range(max_polls):
        try:
            response = await _make_request("GET", f"/jobs/recordInfo?taskId={task_id}")
            
            # Extract data from response
            data = response.get("data", {})
            status = data.get("state")
            
            logger.info(f"Task {task_id} status: {status} (poll {poll_count + 1}/{max_polls})")
            
            if status == "success":
                return data
            elif status == "failed" or status == "error":
                error_msg = data.get("failMsg", "Unknown error")
                raise KieAiError(f"Task {task_id} failed: {error_msg}")
            elif status in ("processing", "pending", "waiting", "queued"):
                # Task still processing, wait and retry
                await asyncio.sleep(KIE_AI_POLL_INTERVAL)
                continue
            else:
                raise KieAiError(f"Unknown task status: {status}")
        
        except KieAiError:
            raise
        except Exception as e:
            logger.error(f"Error polling task {task_id}: {e}")
            raise KieAiError(f"Error polling task: {e}")
    
    raise KieAiTimeoutError(f"Task {task_id} did not complete after {max_polls * KIE_AI_POLL_INTERVAL}s")


async def generate_image(
    prompt: str,
    aspect_ratio: str = "1:1",
    resolution: str = "1024"
) -> bytes:
    """
    Generate an image using Kie.ai Flux-2 Pro model.
    
    Args:
        prompt: Image generation prompt describing what to generate
        aspect_ratio: Aspect ratio (e.g., "1:1", "16:9", "9:16")
        resolution: Image resolution (e.g., "512", "1K", "1536")
        
    Returns:
        Image data as bytes
        
    Raises:
        KieAiError: If generation fails
    """
    try:
        logger.info(f"Generating image with prompt: {prompt[:100]}... using {aspect_ratio} aspect ratio")
        
        # Prepare request data for Kie.ai
        task_data = {
            "model": "flux-2/pro-text-to-image",
            "callBackUrl": None,  # No callback needed, we'll poll
            "input": {
                "prompt": prompt,
                "aspect_ratio": aspect_ratio,
                "resolution": resolution
            }
        }
        
        # Create generation task
        response = await _make_request("POST", "/jobs/createTask", task_data, timeout=30)
        
        # Extract task ID
        task_id = response.get("data", {}).get("taskId")
        
        if not task_id:
            raise KieAiError(f"No taskId returned from image generation API: {response}")
        
        logger.info(f"Image generation task submitted: {task_id}")
        
        # Poll for completion
        result = await _poll_task_status(task_id)
        
        # Extract image URL from result JSON
        result_json_str = result.get("resultJson")
        if not result_json_str:
            raise KieAiError(f"No resultJson in task result: {result}")
        
        # Parse the JSON string
        try:
            result_json = json.loads(result_json_str)
        except json.JSONDecodeError as e:
            raise KieAiError(f"Failed to parse resultJson: {e}")
        
        # Extract image URL
        result_urls = result_json.get("resultUrls", [])
        
        if not result_urls:
            raise KieAiError(f"No resultUrls in task result: {result_json}")
        
        image_url = result_urls[0]  # Get first image
        logger.info(f"Image generated successfully: {image_url}")
        
        # Download the image
        async with aiohttp.ClientSession() as session:
            async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=60)) as response:
                if response.status != 200:
                    raise KieAiError(f"Failed to download generated image: HTTP {response.status}")
                return await response.read()
    
    except KieAiError:
        raise
    except Exception as e:
        logger.error(f"Unexpected error during image generation: {e}")
        raise KieAiError(f"Image generation failed: {e}")


async def animate_image(
    image_url: str,
    prompt: str = "",
    duration_seconds: float = 5.0
) -> bytes:
    """
    Animate a static image using Kie.ai image-to-video model.
    
    Args:
        image_url: URL of the image to animate
        prompt: Optional animation prompt describing the motion
        duration_seconds: Duration of the output video in seconds
        
    Returns:
        Video data as bytes
        
    Raises:
        KieAiError: If animation fails
    """
    try:
        logger.info(f"Animating image from {image_url} with prompt: {prompt[:100] if prompt else '(none)'}...")
        
        # Prepare request data for image-to-video task
        task_data = {
            "model": "flux-2/pro-image-to-video",  # Image-to-video model
            "callBackUrl": None,  # No callback needed, we'll poll
            "input": {
                "image_url": image_url,
                "prompt": prompt or "smooth camera movement",  # Default prompt if none provided
                "duration": duration_seconds
            }
        }
        
        # Create animation task
        response = await _make_request("POST", "/jobs/createTask", task_data, timeout=60)
        task_id = response.get("data", {}).get("taskId")
        
        if not task_id:
            raise KieAiError(f"No taskId returned from animation API: {response}")
        
        logger.info(f"Animation task submitted: {task_id}")
        
        # Poll for completion (longer timeout for video generation)
        result = await _poll_task_status(task_id, max_polls=int(600 / KIE_AI_POLL_INTERVAL))  # 10 min timeout
        
        # Extract video URL from result JSON
        result_json_str = result.get("resultJson")
        if not result_json_str:
            raise KieAiError(f"No resultJson in task result: {result}")
        
        # Parse the JSON string
        try:
            result_json = json.loads(result_json_str)
        except json.JSONDecodeError as e:
            raise KieAiError(f"Failed to parse resultJson: {e}")
        
        # Extract video URL
        result_urls = result_json.get("resultUrls", [])
        
        if not result_urls:
            raise KieAiError(f"No resultUrls in task result: {result_json}")
        
        video_url = result_urls[0]  # Get first video
        logger.info(f"Image animated successfully: {video_url}")
        
        # Download the video
        async with aiohttp.ClientSession() as session:
            async with session.get(video_url, timeout=aiohttp.ClientTimeout(total=300)) as response:
                if response.status != 200:
                    raise KieAiError(f"Failed to download animated video: HTTP {response.status}")
                return await response.read()
    
    except KieAiError:
        raise
    except Exception as e:
        logger.error(f"Unexpected error during image animation: {e}")
        raise KieAiError(f"Image animation failed: {e}")


# Singleton instance for convenience
class KieAiService:
    """High-level service for Kie.ai operations."""
    
    async def generate_image(
        self,
        prompt: str,
        aspect_ratio: str = "1:1",
        resolution: str = "1024"
    ) -> bytes:
        """Generate an image. See generate_image() for details."""
        return await generate_image(prompt, aspect_ratio, resolution)
    
    async def animate_image(
        self,
        image_url: str,
        prompt: str = "",
        duration_seconds: float = 5.0
    ) -> bytes:
        """Animate an image. See animate_image() for details."""
        return await animate_image(image_url, prompt, duration_seconds)


# Create singleton instance
kie_ai_service = KieAiService()
