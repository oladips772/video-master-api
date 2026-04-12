"""
Service for generating images via OpenRouter (Gemini 2.0 Flash).

OpenRouter does not have an /images/generations endpoint.
Image generation is done via /chat/completions with a Gemini model
that supports image output — the image is returned as a base64
inline_data part inside the assistant message content.
"""
import os
import base64
import logging
import aiohttp

logger = logging.getLogger(__name__)


class OpenRouterImageService:
    def __init__(self):
        self.api_key = os.getenv("OPENROUTER_API_KEY")
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY environment variable is not set")
        self.base_url = "https://openrouter.ai/api/v1"

    async def generate_image(self, prompt: str, aspect_ratio: str = "16:9") -> bytes:
        """
        Generate an image via OpenRouter using Gemini 2.0 Flash Exp (free).

        The model returns the image as a base64-encoded inline_data part
        inside the chat completions response.

        Args:
            prompt:       Text prompt for image generation.
            aspect_ratio: Aspect ratio hint appended to the prompt.

        Returns:
            Raw image bytes.
        """
        logger.info(f"Requesting OpenRouter image: ratio={aspect_ratio}, prompt={prompt[:80]}...")

        # Gemini image generation is triggered via chat completions.
        # Appending the aspect ratio to the prompt is the only way to hint dimensions.
        full_prompt = f"{prompt}. Aspect ratio: {aspect_ratio}."

        payload = {
            "model": "google/gemini-2.0-flash-exp:free",
            "messages": [
                {
                    "role": "user",
                    "content": full_prompt,
                }
            ],
            "modalities": ["image", "text"],
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise RuntimeError(
                        f"OpenRouter API error {response.status}: {error_text[:300]}"
                    )
                data = await response.json()

        # Extract base64 image from the response content parts
        content = data["choices"][0]["message"].get("content", "")

        # Content may be a list of parts (text + image) or a plain string
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    image_url = part["image_url"]["url"]
                    if image_url.startswith("data:"):
                        # data:<mime>;base64,<data>
                        b64_data = image_url.split(",", 1)[1]
                        image_bytes = base64.b64decode(b64_data)
                        logger.info(f"OpenRouter image decoded: {len(image_bytes)} bytes")
                        return image_bytes
                elif isinstance(part, dict) and part.get("type") == "inline_data":
                    image_bytes = base64.b64decode(part["inline_data"]["data"])
                    logger.info(f"OpenRouter image decoded: {len(image_bytes)} bytes")
                    return image_bytes

        raise RuntimeError(
            f"OpenRouter response contained no image part. "
            f"Response: {str(data)[:400]}"
        )


# Lazy singleton
_openrouter_image_service: "OpenRouterImageService | None" = None


def get_openrouter_image_service() -> OpenRouterImageService:
    global _openrouter_image_service
    if _openrouter_image_service is None:
        _openrouter_image_service = OpenRouterImageService()
    return _openrouter_image_service
