"""
Service for generating images via OpenRouter (Gemini 2.5 Flash Image Preview).
"""
import os
import base64
import logging
import httpx

logger = logging.getLogger(__name__)

DIMENSION_MAP = {
    "16:9": {"width": 1344, "height": 768},
    "9:16": {"width": 768,  "height": 1344},
    "1:1":  {"width": 1024, "height": 1024},
    "4:3":  {"width": 1024, "height": 768},
}


class OpenRouterImageService:
    def __init__(self):
        self.api_key = os.getenv("OPENROUTER_API_KEY")
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY environment variable is not set")
        self.base_url = "https://openrouter.ai/api/v1"

    async def generate_image(self, prompt: str, aspect_ratio: str = "16:9") -> bytes:
        """
        Generate an image via OpenRouter using Gemini 2.5 Flash Image Preview.

        Args:
            prompt:       Text prompt for image generation.
            aspect_ratio: One of "16:9", "9:16", "1:1", "4:3".

        Returns:
            Raw image bytes.
        """
        dims = DIMENSION_MAP.get(aspect_ratio, DIMENSION_MAP["16:9"])

        logger.info(
            f"Requesting OpenRouter image: ratio={aspect_ratio} "
            f"{dims['width']}x{dims['height']}, prompt={prompt[:80]}..."
        )

        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{self.base_url}/images/generations",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "google/gemini-2.5-flash-preview-05-20:free",
                    "prompt": prompt,
                    "width": dims["width"],
                    "height": dims["height"],
                    "n": 1,
                    "response_format": "b64_json",
                },
            )
            response.raise_for_status()
            data = response.json()

        image_b64 = data["data"][0]["b64_json"]
        image_bytes = base64.b64decode(image_b64)

        logger.info(f"OpenRouter image decoded: {len(image_bytes)} bytes")
        return image_bytes


# Lazy singleton
_openrouter_image_service: "OpenRouterImageService | None" = None


def get_openrouter_image_service() -> OpenRouterImageService:
    global _openrouter_image_service
    if _openrouter_image_service is None:
        _openrouter_image_service = OpenRouterImageService()
    return _openrouter_image_service
