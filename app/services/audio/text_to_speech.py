"""
Service for converting text to speech using external Kokoro TTS API.
"""
import os
import re
import uuid
import asyncio
import logging
import json
import subprocess
import aiohttp
from typing import List
from app.utils.storage import storage_manager

# Configure logging
logger = logging.getLogger(__name__)

# Kokoro API settings
KOKORO_API_URL = os.environ.get("KOKORO_API_URL", "http://kokoro-tts:8880/v1/audio/speech")
KOKORO_TIMEOUT = int(os.environ.get("KOKORO_TIMEOUT", "30"))  # seconds

async def generate_speech(
    text: str,
    voice: str = "af_alloy",
    speed: float = 1.0,
) -> bytes:
    """
    Generate speech from text using the external Kokoro TTS API.

    Args:
        text: Text to convert to speech
        voice: Name of the Kokoro voice to use
        speed: Speech speed multiplier (0.5–2.0). 1.0 = normal, 0.9 = slightly slower.

    Returns:
        Audio data bytes
    """
    try:
        logger.info(f"Calling Kokoro TTS service with voice: {voice}, speed: {speed}")

        # Prepare request data - MUST use "input" for text
        data = {
            "input": text,  # API expects "input", not "text"
            "voice": voice,
            "speed": speed,
        }
        
        # Log exact request details for debugging
        logger.info(f"Kokoro API URL: {KOKORO_API_URL}")
        logger.info(f"Sending request to Kokoro API with data: {json.dumps(data)}")
        
        # Call the Kokoro API
        async with aiohttp.ClientSession() as session:
            async with session.post(
                KOKORO_API_URL,
                json=data,
                timeout=aiohttp.ClientTimeout(total=KOKORO_TIMEOUT)
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Kokoro API error {response.status}: {error_text}")
                    # Also log headers for debugging
                    logger.error(f"Request headers: {response.request_info.headers}")
                    logger.error(f"Response headers: {response.headers}")
                    raise ValueError(f"Kokoro API returned {response.status}: {error_text}")
                
                # Read the audio data
                audio_data = await response.read()
                logger.info(f"Received {len(audio_data)} bytes of audio data from Kokoro API")
                return audio_data
                
    except aiohttp.ClientError as e:
        logger.error(f"Error connecting to Kokoro API: {e}")
        raise ValueError(f"Failed to connect to Kokoro API: {e}")
    except Exception as e:
        logger.error(f"Unexpected error calling Kokoro API: {e}")
        raise ValueError(f"Failed to generate speech with Kokoro: {e}")


def _split_into_sentences(text: str) -> List[str]:
    """Split on sentence-ending punctuation followed by whitespace, and paragraph breaks."""
    parts = re.split(r'(?<=[.!?])\s+|\n\n+', text.strip())
    return [p.strip() for p in parts if p.strip()]


def _chunk_text(text: str, max_chars: int) -> List[str]:
    """Group sentences into chunks under max_chars. Each chunk contains at least one sentence."""
    sentences = _split_into_sentences(text)
    if not sentences:
        return []
    chunks: List[str] = []
    current = ""
    for sent in sentences:
        if not current:
            current = sent
        elif len(current) + 1 + len(sent) <= max_chars:
            current = current + " " + sent
        else:
            chunks.append(current)
            current = sent
    if current:
        chunks.append(current)
    return chunks


async def generate_speech_chunked(
    text: str,
    voice: str = "af_alloy",
    speed: float = 1.0,
    output_path: str = "",
    max_chars: int = 400,
) -> str:
    """Split text into sentence-aware chunks, TTS each, concatenate to output_path with FFmpeg."""
    if not output_path:
        raise ValueError("output_path is required for generate_speech_chunked")

    chunks = _chunk_text(text, max_chars)
    if not chunks:
        raise ValueError("No text content to synthesize")

    logger.info(
        f"Chunked TTS: {len(text)} chars → {len(chunks)} chunks (max_chars={max_chars})"
    )

    work_dir = os.path.dirname(output_path) or "."
    os.makedirs(work_dir, exist_ok=True)
    run_id = uuid.uuid4().hex

    chunk_paths: List[str] = []
    list_path = os.path.join(work_dir, f"_tts_concat_{run_id}.txt")

    try:
        for i, chunk in enumerate(chunks):
            audio_data = await generate_speech(chunk, voice, speed)
            if not audio_data or len(audio_data) < 100:
                raise RuntimeError(
                    f"Kokoro returned empty audio for chunk {i + 1}/{len(chunks)} "
                    f"({len(audio_data) if audio_data else 0} bytes)"
                )
            chunk_path = os.path.join(work_dir, f"_tts_chunk_{run_id}_{i}.mp3")
            with open(chunk_path, "wb") as f:
                f.write(audio_data)
            chunk_paths.append(chunk_path)

        with open(list_path, "w") as f:
            for p in chunk_paths:
                f.write(f"file '{p}'\n")

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", list_path,
            "-c", "copy",
            output_path,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg concat failed: {r.stderr[-500:]}")

        logger.info(f"Chunked TTS concatenated → {output_path}")
        return output_path

    finally:
        for p in chunk_paths + [list_path]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError as e:
                    logger.warning(f"Failed to remove chunk temp {p}: {e}")


async def process_text_to_speech(params: dict) -> dict:
    """
    Process text to speech conversion as a job using Kokoro TTS API.
    
    Args:
        params: Job parameters
            - text: Text to convert to speech
            - voice: Name of the Kokoro voice to use
            
    Returns:
        Dictionary with the result
            - audio_url: URL of the generated audio file
            - tts_engine: The TTS engine used (kokoro)
            - voice: The voice used for synthesis
    """
    text = params.get("text")
    voice = params.get("voice", "af_alloy")
    
    if not text:
        raise ValueError("Text parameter is required")
    
    # Track created files for cleanup
    created_files = []
    audio_url = None
    
    try:
        # Generate a unique output filename
        mp3_filename = f"{uuid.uuid4()}.mp3"
        mp3_output_path = f"temp/output/{mp3_filename}"
        
        # Make sure output directory exists
        os.makedirs(os.path.dirname(mp3_output_path), exist_ok=True)
        
        # Log the parameters being sent to the API
        logger.info(f"Sending to Kokoro TTS API - Text: '{text}', Voice: '{voice}'")
        
        # Get the audio data from Kokoro API
        audio_data = await generate_speech(text, voice)
        
        # Save the audio data to file
        with open(mp3_output_path, 'wb') as f:
            f.write(audio_data)
        
        created_files.append(mp3_output_path)
        logger.info(f"Saved audio data to {mp3_output_path}")
        
        # Upload to S3
        s3_key = f"audio/{mp3_filename}"
        audio_url  = storage_manager.upload_file(mp3_output_path, s3_key)
    
        
        logger.info(f"Audio file uploaded to S3 with URL: {audio_url}")

        #remove signed url from s3
        audio_url = audio_url.split("?")[0]
        

        # Return the result
        return {
            "audio_url": audio_url,
            "audio_path": s3_key,
            "tts_engine": "kokoro",
            "voice": voice
        }
        
    except Exception as e:
        logger.error(f"Error processing text to speech: {e}")
        raise
    finally:
        # Only clean up temporary files if we've successfully uploaded to S3
        if audio_url:
            logger.info("Cleaning up temporary files")
            for path in created_files:
                if os.path.exists(path):
                    try:
                        os.remove(path)
                        logger.info(f"Removed temporary file: {path}")
                    except Exception as e:
                        logger.warning(f"Failed to remove temporary file {path}: {e}")
        else:
            logger.warning("Keeping temporary files for debugging as upload failed") 