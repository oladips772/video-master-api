"""
Service for generating images via Together AI (FLUX.1-schnell).
"""
import os
import logging
import aiohttp

logger = logging.getLogger(__name__)

TOGETHER_BASE_URL = "https://api.together.xyz/v1"
TOGETHER_MODEL    = "black-forest-labs/FLUX.1-schnell"

# Explicit pixel dimensions per aspect ratio (must be integers)
DIMENSION_MAP = {
    "16:9": {"width": 1344, "height": 768},
    "9:16": {"width": 768,  "height": 1344},
    "1:1":  {"width": 1024, "height": 1024},
    "4:3":  {"width": 1024, "height": 768},
}


class TogetherAIService:
    """Generate images using Together AI's FLUX.1-schnell model."""

    def __init__(self):
        self.api_key = os.getenv("TOGETHER_API_KEY")
        if not self.api_key:
            raise ValueError("TOGETHER_API_KEY environment variable is not set")

    async def generate_image(
        self,
        prompt: str,
        aspect_ratio: str = "16:9",
    ) -> bytes:
        """
        Generate an image from a text prompt via Together AI.

        Args:
            prompt:       Text prompt for image generation.
            aspect_ratio: One of "16:9", "9:16", "1:1", "4:3".
                          Defaults to "16:9" for unknown values.

        Returns:
            Raw image bytes.
        """
        dims = DIMENSION_MAP.get(aspect_ratio, DIMENSION_MAP["16:9"])

        payload = {
            "model":           TOGETHER_MODEL,
            "prompt":          prompt,
            "width":           dims["width"],   # must be int
            "height":          dims["height"],  # must be int
            "steps":           4,               # max for Flux Schnell
            "n":               1,
            "response_format": "url",
        }

        logger.info(
            f"Requesting Together AI image: ratio={aspect_ratio} "
            f"{dims['width']}x{dims['height']}, prompt={prompt[:80]}..."
        )

        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Step 1: generate image and get temporary URL
            async with session.post(
                f"{TOGETHER_BASE_URL}/images/generations",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise RuntimeError(
                        f"Together AI API error {response.status}: {error_text[:300]}"
                    )
                data = await response.json()

            image_url = data["data"][0]["url"]
            logger.info(f"Together AI image URL: {image_url}")

            # Step 2: download immediately — URL is temporary
            async with session.get(image_url, allow_redirects=True) as img_response:
                if img_response.status != 200:
                    error_text = await img_response.text()
                    raise RuntimeError(
                        f"Failed to download Together AI image ({img_response.status}): "
                        f"{error_text[:200]}"
                    )
                image_bytes = await img_response.read()

        logger.info(f"Together AI image downloaded: {len(image_bytes)} bytes")
        return image_bytes


# Lazy singleton — instantiated on first use so startup doesn't fail when key is absent
_together_ai_service: "TogetherAIService | None" = None


def get_together_ai_service() -> TogetherAIService:
    global _together_ai_service
    if _together_ai_service is None:
        _together_ai_service = TogetherAIService()
    return _together_ai_service
