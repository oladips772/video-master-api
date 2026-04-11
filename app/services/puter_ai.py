"""
Service for generating images via Puter AI (FLUX.1-schnell-Free).
"""
import os
import logging
import httpx
from putergenai import PuterClient

logger = logging.getLogger(__name__)


class PuterImageService:
    def __init__(self):
        self.username = os.getenv("PUTER_USERNAME")
        self.password = os.getenv("PUTER_PASSWORD")
        if not self.username or not self.password:
            raise ValueError("PUTER_USERNAME and PUTER_PASSWORD environment variables are required")

    async def generate_image(self, prompt: str, aspect_ratio: str = "16:9") -> bytes:
        """
        Generate an image via Puter AI using FLUX.1-schnell-Free.

        Args:
            prompt:       Text prompt for image generation.
            aspect_ratio: Aspect ratio hint (not enforced by Puter — included for API parity).

        Returns:
            Raw image bytes.
        """
        logger.info(f"Requesting Puter AI image: ratio={aspect_ratio}, prompt={prompt[:80]}...")

        async with PuterClient() as client:
            await client.login(self.username, self.password)
            image_url = await client.ai_txt2img(
                prompt,
                model="black-forest-labs/FLUX.1-schnell-Free"
            )

        logger.info(f"Puter AI image URL: {image_url}")

        async with httpx.AsyncClient(timeout=60) as http:
            response = await http.get(image_url, follow_redirects=True)
            response.raise_for_status()
            image_bytes = response.content

        logger.info(f"Puter AI image downloaded: {len(image_bytes)} bytes")
        return image_bytes


# Lazy singleton
_puter_image_service: "PuterImageService | None" = None


def get_puter_image_service() -> PuterImageService:
    global _puter_image_service
    if _puter_image_service is None:
        _puter_image_service = PuterImageService()
    return _puter_image_service
