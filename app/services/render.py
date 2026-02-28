"""
Service for multi-scene video rendering with Ken Burns and animated channels.

Coordinates:
1. Image generation via Kie.ai
2. Voice generation via Kokoro TTS
3. Video assembly per-scene
4. Multi-scene concatenation
5. Background music mixing
6. Progress tracking and retry logic
"""
import os
import asyncio
import logging
import uuid
import tempfile
import shutil
import subprocess
from typing import Dict, Any, Optional, List
from pathlib import Path
import json

from app.services.kie_ai import kie_ai_service, KieAiError
from app.services.audio.text_to_speech import generate_speech
from app.services.video.concatenate import concatenate_videos
from app.services.video.add_audio import add_audio_service
from app.utils.storage import storage_manager
from app.utils.media import download_media_file
from app.utils.image_to_video.image_processing import process_image
from app.utils.image_to_video.video_generation import create_video_with_effects
from app.utils.captions import create_srt_from_word_timestamps, create_srt_from_text
import aiohttp

# Configure logging
logger = logging.getLogger(__name__)

# Configuration
BATCH_SIZE = int(os.environ.get("KIE_AI_CONCURRENCY", "5"))  # Process scenes in batches
TEMP_BASE_DIR = os.environ.get("RENDER_TEMP_DIR", "/tmp/media-master")


class RenderJob:
    """Tracks state of a multi-scene render job."""
    
    def __init__(self, job_id: str, render_params: Dict[str, Any]):
        self.job_id = job_id
        self.render_params = render_params
        self.temp_dir = os.path.join(TEMP_BASE_DIR, job_id)
        self.status = "pending"  # pending, processing, completed, failed
        self.error = None
        self.scenes: Dict[int, Dict[str, Any]] = {}  # Track per-scene state
        self.completed_scenes = 0
        self.failed_scenes = 0
        self.final_video_url = None
        self.final_file_size = None
        
        # Initialize scene tracking
        for scene in render_params.get("scenes", []):
            scene_num = scene.get("scene_number")
            self.scenes[scene_num] = {
                "status": "pending",
                "progress_percent": 0,
                "error": None,
                "video_path": None,
                "video_url": None
            }
        
        # Create temp directory
        os.makedirs(self.temp_dir, exist_ok=True)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert job to dict for storage."""
        return {
            "job_id": self.job_id,
            "render_params": self.render_params,
            "status": self.status,
            "error": self.error,
            "scenes": self.scenes,
            "completed_scenes": self.completed_scenes,
            "failed_scenes": self.failed_scenes,
            "final_video_url": self.final_video_url,
            "final_file_size": self.final_file_size
        }
    
    def cleanup(self):
        """Clean up temporary files."""
        if os.path.exists(self.temp_dir):
            try:
                shutil.rmtree(self.temp_dir)
                logger.info(f"Cleaned up temp directory: {self.temp_dir}")
            except Exception as e:
                logger.error(f"Error cleaning up temp directory: {e}")


class RenderService:
    """Service for multi-scene video rendering."""

    def __init__(self):
        self.jobs: Dict[str, RenderJob] = {}
        # Limit concurrent TTS calls — Kokoro is CPU-bound and can't handle many at once
        self._tts_semaphore = asyncio.Semaphore(int(os.environ.get("KOKORO_CONCURRENCY", "1")))
        logger.info(f"Initialized RenderService with batch size {BATCH_SIZE}")
    
    async def start_render(self, job_id: str, render_params: Dict[str, Any]) -> str:
        """
        Start a multi-scene render job.
        
        Args:
            job_id: Unique job ID
            render_params: Render request parameters
            
        Returns:
            Job ID
        """
        job = RenderJob(job_id, render_params)
        self.jobs[job_id] = job
        
        # Start processing in background
        asyncio.create_task(self._process_render_job(job))
        
        return job_id
    
    async def get_job_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get the current status of a render job."""
        job = self.jobs.get(job_id)
        if not job:
            return None
        
        return {
            "job_id": job.job_id,
            "status": job.status,
            "total_scenes": len(job.scenes),
            "completed_scenes": job.completed_scenes,
            "failed_scenes": job.failed_scenes,
            "progress_percent": self._calculate_progress(job),
            "scenes": [
                {
                    "scene_number": num,
                    "status": info["status"],
                    "progress_percent": info["progress_percent"],
                    "error": info["error"],
                    "video_url": info["video_url"]
                }
                for num, info in sorted(job.scenes.items())
            ],
            "final_video_url": job.final_video_url,
            "final_file_size": job.final_file_size,
            "error": job.error
        }
    
    async def retry_scenes(self, job_id: str, scene_numbers: Optional[List[int]] = None) -> bool:
        """
        Retry failed scenes or specific scenes.
        
        Args:
            job_id: Job ID
            scene_numbers: Optional list of scene numbers to retry. If None, retries all failed.
            
        Returns:
            True if retry started, False if job not found or no scenes to retry
        """
        job = self.jobs.get(job_id)
        if not job:
            return False
        
        # Determine which scenes to retry
        if scene_numbers:
            scenes_to_retry = scene_numbers
        else:
            scenes_to_retry = [
                num for num, info in job.scenes.items()
                if info["status"] == "failed"
            ]
        
        if not scenes_to_retry:
            logger.info(f"No scenes to retry for job {job_id}")
            return False
        
        # Reset status for scenes being retried
        for scene_num in scenes_to_retry:
            if scene_num in job.scenes:
                job.scenes[scene_num]["status"] = "pending"
                job.scenes[scene_num]["error"] = None
                job.failed_scenes -= 1
        
        # Restart processing
        asyncio.create_task(self._process_render_job(job))
        return True
    
    async def _process_render_job(self, job: RenderJob):
        """Main processing loop for a render job."""
        try:
            job.status = "processing"
            channel = job.render_params.get("channel", "kenburns")
            
            logger.info(f"Starting render job {job.job_id} with channel {channel}")
            
            # Process scenes in parallel batches
            await self._process_scenes_batched(job, channel)
            
            # After all scenes are done, assemble final video
            if job.failed_scenes == 0:
                await self._assemble_final_video(job)
                job.status = "completed"
                logger.info(f"Render job {job.job_id} completed successfully")
            else:
                job.status = "failed"
                job.error = f"{job.failed_scenes} scenes failed to render"
                logger.error(f"Render job {job.job_id} failed: {job.error}")
            
            # Fire webhook if provided
            await self._fire_webhook(job)
        
        except Exception as e:
            logger.error(f"Error processing render job {job.job_id}: {e}")
            job.status = "failed"
            job.error = str(e)
            await self._fire_webhook(job)
        
        finally:
            # Cleanup temp files (optional - can be disabled for debugging)
            if os.environ.get("RENDER_CLEANUP", "true").lower() == "true":
                job.cleanup()
    
    async def _process_scenes_batched(self, job: RenderJob, channel: str):
        """Process scenes in parallel batches."""
        scene_list = job.render_params.get("scenes", [])

        # Only process scenes that are still pending — skip already-completed or
        # in-progress ones so that retries don't double-count completed_scenes.
        scenes_to_process = [
            scene for scene in scene_list
            if job.scenes.get(scene.get("scene_number"), {}).get("status") == "pending"
        ]
        total = len(scenes_to_process)

        logger.info(f"Scenes to process: {[s.get('scene_number') for s in scenes_to_process]}")

        for batch_start in range(0, total, BATCH_SIZE):
            batch = scenes_to_process[batch_start:batch_start + BATCH_SIZE]

            logger.info(
                f"Processing batch {batch_start // BATCH_SIZE + 1}: "
                f"scenes {[s.get('scene_number') for s in batch]}"
            )

            await asyncio.gather(*[
                self._process_scene(job, scene, channel)
                for scene in batch
            ])
    
    async def _process_scene(self, job: RenderJob, scene: Dict[str, Any], channel: str):
        """Process a single scene."""
        scene_num = scene.get("scene_number")
        
        try:
            job.scenes[scene_num]["status"] = "processing"
            logger.info(f"Processing scene {scene_num}")
            
            # Step 1: Generate image from prompt
            job.scenes[scene_num]["progress_percent"] = 10
            image_data = await self._generate_image_for_scene(job, scene)
            
            # Step 2: Generate voiceover
            job.scenes[scene_num]["progress_percent"] = 25
            audio_path = await self._generate_voiceover_for_scene(job, scene)
            
            # Step 3: Get audio duration
            audio_duration = self._get_audio_duration(audio_path)
            
            # Step 4: Generate video based on channel
            job.scenes[scene_num]["progress_percent"] = 50
            if channel == "animated":
                video_path = await self._generate_animated_video(job, scene, image_data, audio_duration)
            else:  # kenburns
                video_path = await self._generate_kenburns_video(job, scene, image_data, audio_duration)
            
            # Step 5: Assemble scene (video + audio + subtitles)
            job.scenes[scene_num]["progress_percent"] = 75
            assembled_path = await self._assemble_scene(job, scene_num, video_path, audio_path)
            
            # Step 6: Upload to S3
            job.scenes[scene_num]["progress_percent"] = 90
            video_url = await self._upload_scene_to_s3(job, scene_num, assembled_path)
            
            job.scenes[scene_num]["status"] = "completed"
            job.scenes[scene_num]["progress_percent"] = 100
            job.scenes[scene_num]["video_path"] = assembled_path
            job.scenes[scene_num]["video_url"] = video_url
            job.completed_scenes += 1
            
            logger.info(f"Scene {scene_num} completed successfully")
        
        except Exception as e:
            logger.error(f"Error processing scene {scene_num}: {e}")
            job.scenes[scene_num]["status"] = "failed"
            job.scenes[scene_num]["error"] = str(e)
            job.failed_scenes += 1
    
    async def _generate_image_for_scene(self, job: RenderJob, scene: Dict[str, Any]) -> bytes:
        """Generate image using the provider specified in settings (default: Together AI)."""
        settings = job.render_params.get("settings", {})
        aspect_ratio = settings.get("aspect_ratio", "16:9")
        image_provider = settings.get("image_provider", "together")
        prompt = scene.get("image_prompt")
        scene_num = scene.get("scene_number")

        logger.info(
            f"Generating image for scene {scene_num} via {image_provider}: {prompt[:100]}..."
        )

        try:
            if image_provider == "kie":
                resolution = settings.get("resolution", "1080")
                image_data = await kie_ai_service.generate_image(prompt, aspect_ratio, resolution)
            else:
                # Default: Together AI
                from app.services.together_ai import get_together_ai_service
                image_data = await get_together_ai_service().generate_image(prompt, aspect_ratio)

            # Save to temp file
            temp_image_path = os.path.join(job.temp_dir, f"scene_{scene_num}_image.jpg")
            with open(temp_image_path, "wb") as f:
                f.write(image_data)

            logger.info(f"Image saved to {temp_image_path}")
            return image_data

        except KieAiError as e:
            raise RuntimeError(f"Failed to generate image for scene {scene_num}: {e}")
        except Exception as e:
            raise RuntimeError(f"Failed to generate image for scene {scene_num}: {e}")
    
    async def _generate_voiceover_for_scene(self, job: RenderJob, scene: Dict[str, Any]) -> str:
        """Generate voiceover using Kokoro TTS with retry logic."""
        text = scene.get("narration_text")
        voice_id = scene.get("voice_id", "af_heart")
        scene_num = scene.get("scene_number")
        max_attempts = int(os.environ.get("KOKORO_MAX_RETRIES", "3"))

        # Scene-level speed overrides the global settings speed
        settings = job.render_params.get("settings", {})
        speed = scene.get("voice_speed") or settings.get("voice_speed", 1.0)
        speed = float(speed)

        logger.info(f"Generating voiceover for scene {scene_num} with voice {voice_id}, speed {speed}")

        last_error = None
        for attempt in range(1, max_attempts + 1):
            try:
                async with self._tts_semaphore:
                    audio_data = await generate_speech(text, voice_id, speed)

                if not audio_data or len(audio_data) < 100:
                    raise RuntimeError(
                        f"Kokoro TTS returned empty or invalid audio data ({len(audio_data) if audio_data else 0} bytes)"
                    )

                temp_audio_path = os.path.join(job.temp_dir, f"scene_{scene_num}_audio.mp3")
                with open(temp_audio_path, "wb") as f:
                    f.write(audio_data)

                logger.info(f"Voiceover saved to {temp_audio_path}")
                return temp_audio_path

            except Exception as e:
                last_error = e
                if attempt < max_attempts:
                    wait = 2 ** attempt  # 2s, 4s, 8s
                    logger.warning(
                        f"TTS attempt {attempt}/{max_attempts} failed for scene {scene_num}: {e}. "
                        f"Retrying in {wait}s..."
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error(f"TTS failed after {max_attempts} attempts for scene {scene_num}: {e}")

        raise RuntimeError(f"Failed to generate voiceover for scene {scene_num}: {last_error}")
    
    def _get_audio_duration(self, audio_path: str) -> float:
        """Get audio duration using ffprobe."""
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1:nokey=1",
                    audio_path
                ],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0:
                duration = float(result.stdout.strip())
                logger.info(f"Audio duration: {duration:.2f}s")
                return duration
            else:
                raise RuntimeError(f"ffprobe error: {result.stderr}")
        
        except Exception as e:
            logger.error(f"Error getting audio duration: {e}")
            raise RuntimeError(f"Failed to get audio duration: {e}")
    
    async def _generate_kenburns_video(self, job: RenderJob, scene: Dict[str, Any], image_data: bytes, duration: float) -> str:
        """Generate Ken Burns video from image."""
        scene_num = scene.get("scene_number")
        settings = job.render_params.get("settings", {})
        
        logger.info(f"Generating Ken Burns video for scene {scene_num} with duration {duration:.2f}s")
        
        # Save image temporarily
        temp_image_path = os.path.join(job.temp_dir, f"scene_{scene_num}_image.jpg")
        with open(temp_image_path, "wb") as f:
            f.write(image_data)
        
        try:
            # Get image dimensions for video generation
            image_result = await process_image(temp_image_path)

            # Generate Ken Burns video as a local file (no S3 upload)
            video_path = await create_video_with_effects(
                image_path=image_result["image_path"],
                video_length=duration,
                frame_rate=settings.get("fps", 30),
                zoom_speed=10.0,
                output_dims=image_result["output_dims"],
                scale_dims=image_result["scale_dims"],
                effect_type="ken_burns",
                pan_direction=scene.get("pan_direction", "right"),
                ken_burns_keypoints=scene.get("ken_burns_keypoints")
            )

            logger.info(f"Ken Burns video generated: {video_path}")
            return video_path

        except Exception as e:
            logger.error(f"Error generating Ken Burns video: {e}")
            raise RuntimeError(f"Failed to generate Ken Burns video: {e}")
    
    async def _generate_animated_video(self, job: RenderJob, scene: Dict[str, Any], image_data: bytes, duration: float) -> str:
        """Generate animated video using Kie.ai image-to-video."""
        scene_num = scene.get("scene_number")
        settings = job.render_params.get("settings", {})
        
        logger.info(f"Generating animated video for scene {scene_num} with duration {duration:.2f}s")
        
        # Save image temporarily and upload to S3 to get a public URL
        temp_image_path = os.path.join(job.temp_dir, f"scene_{scene_num}_image.jpg")
        with open(temp_image_path, "wb") as f:
            f.write(image_data)
        
        try:
            # Upload image to S3 to get public URL
            image_s3_path = f"temp/render/{job.job_id}/scene_{scene_num}_image.jpg"
            image_url = storage_manager.upload_file(temp_image_path, image_s3_path)
            logger.info(f"Image uploaded to S3: {image_url}")
            
            # Get animation prompt from scene, default to image prompt
            animation_prompt = scene.get("animation_prompt") or scene.get("image_prompt")
            
            # Call Kie.ai image-to-video
            video_data = await kie_ai_service.animate_image(image_url, animation_prompt, duration)
            
            # Save video to temp file
            video_path = os.path.join(job.temp_dir, f"scene_{scene_num}_video.mp4")
            with open(video_path, "wb") as f:
                f.write(video_data)
            
            logger.info(f"Animated video saved: {video_path}")
            return video_path
        
        except Exception as e:
            logger.error(f"Error generating animated video: {e}")
            raise RuntimeError(f"Failed to generate animated video: {e}")
    
    async def _generate_scene_captions(
        self,
        job: RenderJob,
        scene_num: int,
        audio_path: str,
        caption_style: str,
        caption_properties: Optional[Dict],
    ) -> Optional[str]:
        """
        Transcribe scene audio with Whisper and generate an ASS caption file.

        The transcription service deletes its input in a finally block, so we
        pass it a copy of the audio file to protect the original.

        Returns:
            Absolute path to the generated .ass file, or None on failure.
        """
        try:
            from app.services.media.transcription import transcription_service

            # Protect original audio — transcription service deletes the file it receives
            audio_copy = os.path.join(job.temp_dir, f"scene_{scene_num}_caption_audio.mp3")
            shutil.copy2(audio_path, audio_copy)

            logger.info(f"Transcribing scene {scene_num} audio for captions (style={caption_style})")
            trans_result = await transcription_service.transcribe(
                file_path=audio_copy,
                include_text=True,
                include_srt=False,       # we generate our own ASS file
                word_timestamps=True,
            )

            word_timestamps = trans_result.get("words", [])
            duration = self._get_audio_duration(audio_path)
            max_words = int((caption_properties or {}).get("max_words_per_line", 6))

            if word_timestamps:
                ass_path = await create_srt_from_word_timestamps(
                    word_timestamps=word_timestamps,
                    duration=duration,
                    max_words_per_line=max_words,
                    style=caption_style,
                    caption_properties=caption_properties,
                )
            else:
                # Fallback: distribute words evenly across the duration
                text = trans_result.get("text", "")
                if not text:
                    logger.warning(f"Scene {scene_num}: transcription returned no text, skipping captions")
                    return None
                ass_path = await create_srt_from_text(text, duration, max_words, caption_style)

            logger.info(f"Caption file generated for scene {scene_num}: {ass_path}")
            return ass_path

        except Exception as e:
            logger.warning(f"Caption generation failed for scene {scene_num}: {e}. Skipping captions.")
            return None

    async def _assemble_scene(self, job: RenderJob, scene_num: int, video_path: str, audio_path: str) -> str:
        """Assemble scene: combine video + audio, optionally burning in animated captions."""
        settings = job.render_params.get("settings", {})
        captions_enabled = settings.get("captions_enabled", False)

        logger.info(f"Assembling scene {scene_num} (captions_enabled={captions_enabled})")

        try:
            output_path = os.path.join(job.temp_dir, f"scene_{scene_num}_assembled.mp4")

            if captions_enabled:
                # Resolve caption style: scene-level overrides global
                scene_list = job.render_params.get("scenes", [])
                scene_data = next((s for s in scene_list if s.get("scene_number") == scene_num), {})
                caption_style = scene_data.get("caption_style") or settings.get("caption_style", "highlight")
                caption_properties = settings.get("caption_properties") or {}

                ass_path = await self._generate_scene_captions(
                    job, scene_num, audio_path, caption_style, caption_properties
                )

                if ass_path and os.path.exists(ass_path):
                    # Step 1: combine video + audio (fast copy)
                    combined_path = os.path.join(job.temp_dir, f"scene_{scene_num}_combined.mp4")
                    cmd = [
                        "ffmpeg", "-y",
                        "-i", video_path,
                        "-i", audio_path,
                        "-c:v", "copy",
                        "-c:a", "aac",
                        "-shortest",
                        combined_path,
                    ]
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                    if r.returncode != 0:
                        raise RuntimeError(f"ffmpeg combine error: {r.stderr[-500:]}")

                    # Step 2: burn captions (re-encode video with ass= filter)
                    # On Linux absolute paths don't need colon escaping
                    cmd = [
                        "ffmpeg", "-y",
                        "-i", combined_path,
                        "-vf", f"ass={ass_path}",
                        "-c:v", "libx264",
                        "-preset", "fast",
                        "-crf", "18",
                        "-c:a", "copy",
                        output_path,
                    ]
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

                    if r.returncode != 0:
                        logger.warning(
                            f"Caption burn failed for scene {scene_num}: {r.stderr[-300:]}. "
                            "Falling back to version without captions."
                        )
                        shutil.copy2(combined_path, output_path)
                    else:
                        logger.info(f"Scene {scene_num} assembled with captions: {output_path}")

                    # Clean up intermediates
                    for p in (combined_path, ass_path):
                        try:
                            if os.path.exists(p):
                                os.remove(p)
                        except Exception:
                            pass

                    return output_path

                # Caption generation failed — fall through to plain assembly
                logger.warning(f"Scene {scene_num}: caption file unavailable, assembling without captions")

            # Plain assembly: video + audio, no caption burn-in
            cmd = [
                "ffmpeg", "-y",
                "-i", video_path,
                "-i", audio_path,
                "-c:v", "copy",
                "-c:a", "aac",
                "-shortest",
                output_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

            if result.returncode != 0:
                raise RuntimeError(f"ffmpeg error: {result.stderr[-500:]}")

            logger.info(f"Scene {scene_num} assembled: {output_path}")
            return output_path

        except Exception as e:
            logger.error(f"Error assembling scene {scene_num}: {e}")
            raise RuntimeError(f"Failed to assemble scene: {e}")
    
    async def _upload_scene_to_s3(self, job: RenderJob, scene_num: int, video_path: str) -> str:
        """Upload assembled scene to S3."""
        try:
            object_name = f"renders/{job.job_id}/scene_{scene_num}.mp4"
            url = storage_manager.upload_file(video_path, object_name)
            logger.info(f"Scene {scene_num} uploaded to S3: {url}")
            return url
        
        except Exception as e:
            logger.error(f"Error uploading scene {scene_num} to S3: {e}")
            raise RuntimeError(f"Failed to upload scene to S3: {e}")
    
    async def _assemble_final_video(self, job: RenderJob):
        """Assemble final video from all scenes."""
        concat_temp_dir = None
        try:
            logger.info(f"Assembling final video from {len(job.scenes)} scenes")

            # Get video URLs in scene order
            video_urls = [
                job.scenes[num]["video_url"]
                for num in sorted(job.scenes.keys())
                if job.scenes[num]["video_url"]
            ]

            if not video_urls:
                raise RuntimeError("No completed scenes found")

            settings = job.render_params.get("settings", {})

            result = await concatenate_videos(
                job_id=job.job_id,
                video_urls=video_urls,
                output_format=".mp4"
            )

            # local_path is the actual filesystem file; path is the S3 key
            final_local_path = result.get("local_path") or result.get("path")
            final_url = result.get("url")
            concat_temp_dir = result.get("_temp_dir")

            # Add background music if provided
            bg_music_url = settings.get("background_music")
            if bg_music_url and final_local_path and os.path.exists(final_local_path):
                logger.info(f"Adding background music: {bg_music_url}")
                final_local_path = await self._add_background_music(
                    job,
                    final_local_path,
                    str(bg_music_url),
                    settings.get("background_music_volume", 0.3)
                )
                raw_url = storage_manager.upload_file(final_local_path, f"renders/{job.job_id}/final.mp4")
                final_url = raw_url.split("?")[0] if raw_url and "?" in raw_url else raw_url

            job.final_video_url = final_url
            job.final_file_size = (
                os.path.getsize(final_local_path)
                if final_local_path and os.path.exists(final_local_path)
                else None
            )

            logger.info(f"Final video assembled: {final_url}")

        except Exception as e:
            logger.error(f"Error assembling final video: {e}")
            raise RuntimeError(f"Failed to assemble final video: {e}")

        finally:
            # Clean up the concat temp dir (separate from job.temp_dir)
            if concat_temp_dir and os.path.exists(concat_temp_dir):
                try:
                    shutil.rmtree(concat_temp_dir)
                    logger.info(f"Cleaned up concat temp dir: {concat_temp_dir}")
                except Exception as cleanup_err:
                    logger.warning(f"Failed to cleanup concat temp dir: {cleanup_err}")
    
    async def _add_background_music(self, job: RenderJob, video_path: str, bg_music_url: str, volume: float) -> str:
        """Add background music to final video."""
        try:
            logger.info(f"Downloading background music from {bg_music_url}")
            # download_media_file(url, temp_dir) → returns (local_path, extension)
            bg_audio_path, _ = await download_media_file(bg_music_url, job.temp_dir)
            
            # Get video duration
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    video_path
                ],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                raise RuntimeError(f"Error getting video duration: {result.stderr}")
            
            video_duration = float(result.stdout.strip())
            
            # Create looped background music to match video length
            looped_audio_path = os.path.join(job.temp_dir, "background_looped.mp3")
            
            cmd = [
                "ffmpeg",
                "-stream_loop", "-1",
                "-i", bg_audio_path,
                "-t", str(video_duration - 3),  # Slightly shorter to account for fade out
                "-y",
                looped_audio_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            
            if result.returncode != 0:
                raise RuntimeError(f"Error looping background music: {result.stderr}")
            
            # Apply fade out on last 3 seconds
            faded_audio_path = os.path.join(job.temp_dir, "background_faded.mp3")
            
            cmd = [
                "ffmpeg",
                "-i", looped_audio_path,
                "-af", "afade=t=out:st=" + str(max(0, video_duration - 3)) + ":d=3",
                "-y",
                faded_audio_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            
            if result.returncode != 0:
                raise RuntimeError(f"Error applying fade: {result.stderr}")
            
            # Mix video with background and original audio
            output_path = os.path.join(job.temp_dir, "final_with_music.mp4")
            
            # Complex filter to mix audio tracks with proper volume
            filter_complex = f"[1:a]volume={volume}[bg];[0:a]volume=1[voice];[voice][bg]amix=inputs=2:duration=longest[a]"
            
            cmd = [
                "ffmpeg",
                "-i", video_path,
                "-i", faded_audio_path,
                "-filter_complex", filter_complex,
                "-map", "0:v",
                "-map", "[a]",
                "-c:v", "copy",
                "-c:a", "aac",
                "-y",
                output_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            
            if result.returncode != 0:
                raise RuntimeError(f"Error mixing audio: {result.stderr}")
            
            logger.info(f"Background music added: {output_path}")
            return output_path
        
        except Exception as e:
            logger.error(f"Error adding background music: {e}")
            raise RuntimeError(f"Failed to add background music: {e}")
    
    async def _fire_webhook(self, job: RenderJob):
        """Fire webhook if provided."""
        webhook_url = job.render_params.get("webhook_url")
        
        if not webhook_url:
            return
        
        try:
            logger.info(f"Firing webhook: {webhook_url}")
            
            payload = {
                "job_id": job.job_id,
                "status": job.status,
                "total_scenes": len(job.scenes),
                "completed_scenes": job.completed_scenes,
                "failed_scenes": job.failed_scenes,
                "final_video_url": job.final_video_url,
                "error": job.error
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    str(webhook_url),
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    logger.info(f"Webhook response: {response.status}")
        
        except Exception as e:
            logger.error(f"Error firing webhook: {e}")
    
    def _calculate_progress(self, job: RenderJob) -> int:
        """Calculate overall progress percentage."""
        total = len(job.scenes)
        if total == 0:
            return 0
        
        # Calculate average progress of all scenes
        total_progress = sum(info["progress_percent"] for info in job.scenes.values())
        return int(total_progress / total)


# Create singleton instance
render_service = RenderService()
