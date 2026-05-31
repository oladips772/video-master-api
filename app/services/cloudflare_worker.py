"""
Service for generating images via our Cloudflare Worker (Stable Diffusion XL).

The Worker returns raw PNG bytes directly in the response body — no JSON,
no base64, no temporary URL to download.
"""
import os
import logging
import aiohttp

logger = logging.getLogger(__name__)

CLOUDFLARE_WORKER_URL = "https://my-image.oladipupoakorede497.workers.dev/"

# Map our aspect ratios to the Worker's "ratio" values.
RATIO_MAP = {
    "16:9": "16:9",
    "9:16": "9:16",
    "1:1":  "1:1",
    "4:5":  "4:5",
    "4:3":  "4:3",
    "3:4":  "3:4",
}


class CloudflareWorkerService:
    """Generate images using our Cloudflare Worker (Stable Diffusion XL)."""

    def __init__(self):
        self.api_key = os.getenv("CLOUDFLARE_WORKER_API_KEY")
        if not self.api_key:
            raise ValueError("CLOUDFLARE_WORKER_API_KEY environment variable is not set")

    async def generate_image(
        self,
        prompt: str,
        aspect_ratio: str = "16:9",
    ) -> bytes:
        """
        Generate an image from a text prompt via the Cloudflare Worker.

        Args:
            prompt:       Text prompt for image generation.
            aspect_ratio: One of "16:9", "9:16", "1:1", "4:5", "4:3", "3:4".
                          Defaults to "16:9" for unknown values.

        Returns:
            Raw image bytes (PNG).
        """
        ratio = RATIO_MAP.get(aspect_ratio, "16:9")

        payload = {
            "prompt": prompt,
            "ratio": ratio,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        logger.info(
            f"Requesting Cloudflare Worker image: ratio={ratio}, prompt={prompt[:80]}..."
        )

        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                CLOUDFLARE_WORKER_URL,
                headers=headers,
                json=payload,
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise RuntimeError(
                        f"Cloudflare Worker error {response.status}: {error_text[:300]}"
                    )
                image_bytes = await response.read()

        logger.info(f"Cloudflare Worker image received: {len(image_bytes)} bytes")
        return image_bytes


# Lazy singleton — instantiated on first use so startup doesn't fail when key is absent
_cloudflare_worker_service: "CloudflareWorkerService | None" = None


def get_cloudflare_worker_service() -> CloudflareWorkerService:
    global _cloudflare_worker_service
    if _cloudflare_worker_service is None:
        _cloudflare_worker_service = CloudflareWorkerService()
    return _cloudflare_worker_service
