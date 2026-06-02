"""
Reddit render channel.

Single-shot pipeline:
  1. Kokoro TTS the full script.
  2. Loop a registered background video (assets/backgrounds/<key>.mp4) to the
     narration duration and scale/crop to the requested aspect.
  3. Optionally transcribe the narration with Whisper and burn TikTok-style
     ASS captions (reusing the existing 'pop' style).
  4. Mix in optional background music under the narration.
  5. Upload the final mp4 to S3 and fire the webhook.

Mirrors the kenburns channel's patterns in ``app/services/render.py``:
in-memory ``self.jobs`` dict, ``asyncio.create_task`` dispatch, ``RenderJob``-
style state object, semaphore-limited TTS.
"""
import asyncio
import logging
import os
import shutil
import subprocess
from typing import Any, Dict, Optional

import aiohttp

from app.services.audio.text_to_speech import (
    generate_speech,
    generate_speech_chunked,
    generate_speech_xtts,
)
from app.services.backgrounds import resolve_background
from app.utils.captions import create_srt_from_word_timestamps, create_srt_from_text
from app.utils.media import download_media_file
from app.utils.storage import storage_manager


logger = logging.getLogger(__name__)


TEMP_BASE_DIR = os.environ.get("RENDER_TEMP_DIR", "/tmp/media-master")

# Aspect → (width, height) for the output video
ASPECT_DIMENSIONS = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
}

# Map public-facing caption style names to the existing utils/captions style names
CAPTION_STYLE_MAP = {
    "tiktok": "pop",
    "pop": "pop",
    "highlight": "highlight",
    "word_by_word": "word_by_word",
    "karaoke": "karaoke",
    "zoom_in": "zoom_in",
}

# Default styling overrides for the 'tiktok' caption look. Tuned for 1080x1920.
TIKTOK_CAPTION_PROPERTIES = {
    "font_family": "Arial",   # Impact / Arial Black require font install; Arial is safe everywhere
    "font_size": 96,           # large, readable at 1080x1920
    "bold": True,
    "line_color": "#FFFFFF",
    "outline_color": "#000000",
    "outline_width": 4,
    "shadow_offset": 2,
    "pop_chunk_size": 3,       # words per pop chunk
    "max_words_per_line": 5,   # spec asked for max 5
}


class RedditRenderJob:
    """In-memory state for one reddit render job."""

    def __init__(self, job_id: str, request_params: Dict[str, Any]):
        self.job_id = job_id
        self.params = request_params
        self.temp_dir = os.path.join(TEMP_BASE_DIR, "reddit", job_id)

        self.status: str = "pending"      # pending, processing, completed, failed
        self.stage: Optional[str] = None  # tts, loop_bg, captions, merge, upload
        self.progress_percent: int = 0
        self.error: Optional[str] = None

        self.audio_duration: Optional[float] = None
        self.final_video_url: Optional[str] = None
        self.final_file_size: Optional[int] = None

    def to_status_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "stage": self.stage,
            "progress_percent": self.progress_percent,
            "audio_duration_seconds": self.audio_duration,
            "final_video_url": self.final_video_url,
            "final_file_size": self.final_file_size,
            "error": self.error,
        }

    def cleanup(self):
        if os.path.exists(self.temp_dir):
            try:
                shutil.rmtree(self.temp_dir)
                logger.info(f"[reddit:{self.job_id}] cleaned up temp dir {self.temp_dir}")
            except Exception as e:
                logger.error(f"[reddit:{self.job_id}] cleanup failed: {e}")


class RedditRenderService:
    def __init__(self):
        self.jobs: Dict[str, RedditRenderJob] = {}
        # Kokoro is CPU-bound — match the kenburns service's gating
        self._tts_semaphore = asyncio.Semaphore(int(os.environ.get("KOKORO_CONCURRENCY", "1")))
        logger.info("RedditRenderService initialised")

    # ------------------------------------------------------------------ public

    async def start_render(self, job_id: str, request_params: Dict[str, Any]) -> str:
        job = RedditRenderJob(job_id, request_params)
        self.jobs[job_id] = job
        asyncio.create_task(self._process_job(job))
        return job_id

    async def get_job_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        job = self.jobs.get(job_id)
        if not job:
            return None
        return job.to_status_dict()

    # ----------------------------------------------------------- orchestration

    async def _process_job(self, job: RedditRenderJob):
        try:
            job.status = "processing"
            os.makedirs(job.temp_dir, exist_ok=True)

            # Resolve and validate background file (raises if missing)
            bg_key = job.params["background"]
            bg_path = resolve_background(bg_key)
            if not os.path.exists(bg_path):
                raise FileNotFoundError(
                    f"Background '{bg_key}' is registered but file is missing on disk: {bg_path}. "
                    f"Drop the video into assets/backgrounds/ and restart."
                )

            # 1. TTS
            job.stage = "tts"
            job.progress_percent = 5
            narration_path = await self._generate_tts(job)

            duration = self._get_audio_duration(narration_path)
            job.audio_duration = duration
            logger.info(f"[reddit:{job.job_id}] narration duration: {duration:.2f}s")

            # 2. Loop background
            job.stage = "loop_bg"
            job.progress_percent = 25
            looped_path = await self._loop_background(job, bg_path, duration)

            # 3. Captions (optional)
            ass_path: Optional[str] = None
            if job.params.get("captions", True):
                job.stage = "captions"
                job.progress_percent = 50
                ass_path = await self._generate_captions(job, narration_path, duration)

            # 4. Merge
            job.stage = "merge"
            job.progress_percent = 70
            final_local_path = await self._merge_final(
                job,
                looped_path=looped_path,
                narration_path=narration_path,
                ass_path=ass_path,
            )

            # 5. Upload
            job.stage = "upload"
            job.progress_percent = 90
            await self._upload_and_finalise(job, final_local_path)

            job.status = "completed"
            job.progress_percent = 100
            job.stage = None
            logger.info(f"[reddit:{job.job_id}] completed: {job.final_video_url}")

        except Exception as e:
            logger.exception(f"[reddit:{job.job_id}] failed: {e}")
            job.status = "failed"
            job.error = str(e)

        finally:
            await self._fire_webhook(job)
            if os.environ.get("RENDER_CLEANUP", "true").lower() == "true":
                job.cleanup()

    # ----------------------------------------------------------------- step 1

    async def _generate_tts(self, job: RedditRenderJob) -> str:
        text = job.params["script"]
        provider = (job.params.get("voice_provider") or "xtts").lower()
        if provider == "xtts":
            voice = job.params.get("speaker") or "Claribel Daws"
        else:
            voice = job.params.get("voice_id", "af_heart")
        speed = float(job.params.get("voice_speed", 1.0))
        max_attempts = int(os.environ.get("KOKORO_MAX_RETRIES", "3"))

        logger.info(
            f"[reddit:{job.job_id}] TTS provider={provider} voice={voice} "
            f"speed={speed} chars={len(text)}"
        )

        path = os.path.join(job.temp_dir, "narration.mp3")
        use_chunked = len(text) > 400

        last_err: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                async with self._tts_semaphore:
                    if use_chunked:
                        await generate_speech_chunked(
                            text, voice, speed, path, provider=provider
                        )
                        size = os.path.getsize(path) if os.path.exists(path) else 0
                        if size < 100:
                            raise RuntimeError(
                                f"Chunked TTS produced empty/invalid audio ({size} bytes)"
                            )
                    else:
                        if provider == "xtts":
                            audio_data = await generate_speech_xtts(text, voice, speed)
                        else:
                            audio_data = await generate_speech(text, voice, speed)
                        if not audio_data or len(audio_data) < 100:
                            raise RuntimeError(
                                f"{provider} returned empty/invalid audio "
                                f"({len(audio_data) if audio_data else 0} bytes)"
                            )
                        with open(path, "wb") as f:
                            f.write(audio_data)
                return path

            except Exception as e:
                last_err = e
                if attempt < max_attempts:
                    wait = 2 ** attempt
                    logger.warning(
                        f"[reddit:{job.job_id}] TTS attempt {attempt}/{max_attempts} "
                        f"failed: {e}. retrying in {wait}s"
                    )
                    await asyncio.sleep(wait)

        raise RuntimeError(f"TTS failed after {max_attempts} attempts: {last_err}")

    # ----------------------------------------------------------------- step 2

    async def _loop_background(
        self, job: RedditRenderJob, bg_path: str, duration: float
    ) -> str:
        aspect = job.params.get("aspect_ratio", "9:16")
        width, height = ASPECT_DIMENSIONS[aspect]
        output_path = os.path.join(job.temp_dir, "looped_bg.mp4")

        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1"
        )

        cmd = [
            "ffmpeg", "-y",
            "-stream_loop", "-1",
            "-i", bg_path,
            "-t", f"{duration:.3f}",
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-an",
            output_path,
        ]
        logger.info(f"[reddit:{job.job_id}] looping background → {output_path}")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg loop failed: {r.stderr[-500:]}")
        return output_path

    # ----------------------------------------------------------------- step 3

    async def _generate_captions(
        self, job: RedditRenderJob, narration_path: str, duration: float
    ) -> Optional[str]:
        """Whisper-transcribe the narration and render an ASS caption file."""
        try:
            # transcription_service.transcribe() deletes its input in a finally
            # block, so feed it a copy. (Same pattern as render.py.)
            audio_copy = os.path.join(job.temp_dir, "narration_for_captions.mp3")
            shutil.copy2(narration_path, audio_copy)

            from app.services.media.transcription import transcription_service

            requested_style = (job.params.get("caption_style") or "tiktok").lower()
            internal_style = CAPTION_STYLE_MAP.get(requested_style, "pop")

            logger.info(
                f"[reddit:{job.job_id}] transcribing for captions "
                f"(requested={requested_style} → internal={internal_style})"
            )

            trans = await transcription_service.transcribe(
                file_path=audio_copy,
                include_text=True,
                include_srt=False,
                word_timestamps=True,
            )

            word_timestamps = trans.get("words") or []
            # Merge tiktok defaults under the existing 'pop' style. Users can
            # override later via a request field if we ever need to expose it.
            caption_properties = dict(TIKTOK_CAPTION_PROPERTIES)
            max_words = int(caption_properties.get("max_words_per_line", 5))

            if word_timestamps:
                ass_path = await create_srt_from_word_timestamps(
                    word_timestamps=word_timestamps,
                    duration=duration,
                    max_words_per_line=max_words,
                    style=internal_style,
                    caption_properties=caption_properties,
                )
            else:
                text = trans.get("text", "")
                if not text:
                    logger.warning(
                        f"[reddit:{job.job_id}] transcription returned no text; skipping captions"
                    )
                    return None
                ass_path = await create_srt_from_text(text, duration, max_words, internal_style)

            logger.info(f"[reddit:{job.job_id}] caption file: {ass_path}")
            return ass_path

        except Exception as e:
            # Caption generation is non-fatal — fall through to no-caption merge
            logger.warning(f"[reddit:{job.job_id}] caption generation failed: {e}")
            return None

    # ----------------------------------------------------------------- step 4

    async def _merge_final(
        self,
        job: RedditRenderJob,
        looped_path: str,
        narration_path: str,
        ass_path: Optional[str],
    ) -> str:
        bg_music_url = job.params.get("background_music_url")
        bg_music_volume = float(job.params.get("background_music_volume", 0.08))

        # Optionally download background music
        bg_music_local: Optional[str] = None
        if bg_music_url:
            try:
                logger.info(f"[reddit:{job.job_id}] downloading bg music: {bg_music_url}")
                bg_music_local, _ = await download_media_file(str(bg_music_url), job.temp_dir)
            except Exception as e:
                logger.warning(
                    f"[reddit:{job.job_id}] bg music download failed: {e}. continuing without it."
                )
                bg_music_local = None

        output_path = os.path.join(job.temp_dir, "final.mp4")

        # Build the filter graph. Two axes: with/without bg music, with/without captions.
        inputs = ["-i", looped_path, "-i", narration_path]
        if bg_music_local:
            inputs += ["-i", bg_music_local]

        if bg_music_local:
            audio_filter = (
                "[1:a]volume=1.0[narr];"
                f"[2:a]volume={bg_music_volume}[music];"
                "[narr][music]amix=inputs=2:duration=first:dropout_transition=0[aout]"
            )
        else:
            audio_filter = "[1:a]volume=1.0[narr];[narr]aformat=sample_rates=44100[aout]"

        cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", audio_filter]

        if ass_path and os.path.exists(ass_path):
            # Burn captions: re-encode video with the ass filter.
            cmd += ["-vf", f"ass={ass_path}"]
            cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "20"]
        else:
            # No captions → copy video stream
            cmd += ["-c:v", "copy"]

        cmd += [
            "-map", "0:v",
            "-map", "[aout]",
            "-c:a", "aac",
            "-shortest",
            output_path,
        ]

        logger.info(
            f"[reddit:{job.job_id}] merging → {output_path} "
            f"(captions={bool(ass_path)}, bg_music={bool(bg_music_local)})"
        )
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg merge failed: {r.stderr[-500:]}")
        return output_path

    # ----------------------------------------------------------------- step 5

    async def _upload_and_finalise(self, job: RedditRenderJob, final_local_path: str):
        object_name = f"renders/reddit/{job.job_id}/final.mp4"
        try:
            raw_url = storage_manager.upload_file(final_local_path, object_name)
        except Exception as e:
            raise RuntimeError(f"S3 upload failed: {e}")

        # Strip presigned query string — matches the kenburns convention
        clean_url = raw_url.split("?")[0] if raw_url and "?" in raw_url else raw_url
        job.final_video_url = clean_url

        try:
            job.final_file_size = os.path.getsize(final_local_path)
        except OSError:
            job.final_file_size = None

    # ---------------------------------------------------------------- helpers

    def _get_audio_duration(self, audio_path: str) -> float:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                audio_path,
            ],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            raise RuntimeError(f"ffprobe error: {r.stderr}")
        return float(r.stdout.strip())

    async def _fire_webhook(self, job: RedditRenderJob):
        webhook_url = job.params.get("webhook_url")
        if not webhook_url:
            return
        try:
            payload = {
                "job_id": job.job_id,
                "channel": "reddit",
                "status": job.status,
                "final_video_url": job.final_video_url,
                "audio_duration_seconds": job.audio_duration,
                "error": job.error,
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    str(webhook_url),
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    logger.info(f"[reddit:{job.job_id}] webhook → {response.status}")
        except Exception as e:
            logger.error(f"[reddit:{job.job_id}] webhook error: {e}")


# Singleton — matches render_service pattern
reddit_render_service = RedditRenderService()
