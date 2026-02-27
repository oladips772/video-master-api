<!-- @format -->

# Multi-Scene Render Implementation

## Overview

This document describes the implementation of multi-scene video rendering with
Ken Burns and animated channels, integrated with Kie.ai for image generation and
animation.

## New Files Created

### 1. **app/services/kie_ai.py**

Kie.ai API integration service providing:

- **Image Generation**: Flux-2 Pro model for creating images from prompts
- **Image Animation**: ByteDance/Wan model for animating static images to video
- Async task polling pattern: submit → poll → download
- Error handling and timeout management
- Support for custom aspect ratios and resolutions

**Key Functions:**

- `generate_image(prompt, aspect_ratio, resolution)` → bytes
- `animate_image(image_url, prompt, duration_seconds)` → bytes

### 2. **app/services/render.py**

Main orchestrator for multi-scene rendering pipeline:

- **RenderJob Class**: Tracks job state, scene progress, and temporary files
- **RenderService Class**: Manages job lifecycle, scene batch processing, and
  final assembly

**Key Methods:**

- `start_render()` - Initiates async processing
- `get_job_status()` - Returns detailed progress
- `retry_scenes()` - Retries failed scenes
- `_process_scenes_batched()` - Parallel processing in configurable batches
  (default: 5)
- `_process_scene()` - Handles individual scene pipeline
- `_assemble_final_video()` - Concatenates scenes and adds background music

**Scene Processing Pipeline:**

1. Generate image from prompt (Kie.ai)
2. Generate voiceover from text (Kokoro TTS)
3. Get exact audio duration (ffprobe)
4. Create video (Ken Burns or Kie.ai animation)
5. Assemble scene (video + audio + subtitles)
6. Upload to S3

**Post-Processing:** 7. Concatenate all scenes with transitions 8. Download,
loop, and mix background music 9. Upload final video to S3 10. Fire webhook
notification

### 3. **app/routes/render.py**

HTTP endpoints for render operations:

- `POST /v1/render` - Start new render job (202 Accepted)
- `GET /v1/render/{job_id}/status` - Check progress
- `POST /v1/render/{job_id}/retry` - Retry failed scenes

## Updated Files

### 1. **app/models.py**

Added comprehensive Pydantic models:

- `RenderSettings` - Global video configuration
- `KenBurnsKeypoint` - Ken Burns animation keypoints
- `RenderScene` - Single scene definition
- `RenderRequest` - Complete render request
- `RenderResponse` - Job submission response
- `RenderJobStatus` - Detailed status response
- `SceneProgress` - Per-scene progress tracking
- `RenderRetryRequest` - Retry request
- `JobType.RENDER_MULTI_SCENE` - New job type enum

### 2. **app/main.py**

- Added import: `from app.routes.render import router as render_router`
- Added router:
  `app.include_router(render_router, dependencies=[Depends(get_api_key)])`
- Updated OpenAPI tags with render endpoint documentation

### 3. **.env.example**

Added Kie.ai configuration variables:

```
KIE_AI_API_KEY=your_kie_ai_api_key_here
KIE_AI_BASE_URL=https://api.kie.ai/api/v1
KIE_AI_CONCURRENCY=5
KIE_AI_TIMEOUT=300
KIE_AI_POLL_INTERVAL=5
RENDER_TEMP_DIR=/tmp/media-master
RENDER_CLEANUP=true
```

## API Endpoints

### POST /v1/render

**Start Multi-Scene Render Job**

Creates and immediately starts processing a multi-scene video rendering job.

**Request Body:**

```json
{
  "project_name": "My Video",
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
    "transition_type": "cut",
    "transition_duration_ms": 500
  },
  "scenes": [
    {
      "scene_number": 1,
      "image_prompt": "A beautiful sunset over mountains",
      "narration_text": "Once upon a time...",
      "voice_id": "af_heart",
      "pan_direction": "right",
      "ken_burns_keypoints": [
        { "x": 0.5, "y": 0.4, "zoom": 1.0 },
        { "x": 0.3, "y": 0.5, "zoom": 1.2 }
      ]
    },
    {
      "scene_number": 2,
      "image_prompt": "A mystical forest",
      "narration_text": "In a land far away...",
      "voice_id": "af_heart"
    }
  ]
}
```

**Response (202 Accepted):**

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "pending",
  "total_scenes": 2,
  "monitor_url": "/v1/render/550e8400-e29b-41d4-a716-446655440000/status"
}
```

### GET /v1/render/{job_id}/status

**Check Render Job Status**

Returns detailed progress information including per-scene status.

**Response:**

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "processing",
  "total_scenes": 2,
  "completed_scenes": 1,
  "failed_scenes": 0,
  "progress_percent": 50,
  "scenes": [
    {
      "scene_number": 1,
      "status": "completed",
      "progress_percent": 100,
      "error": null,
      "video_url": "https://s3.example.com/renders/.../scene_1.mp4"
    },
    {
      "scene_number": 2,
      "status": "processing",
      "progress_percent": 75,
      "error": null,
      "video_url": null
    }
  ],
  "final_video_url": null,
  "final_file_size": null,
  "error": null,
  "created_at": "",
  "updated_at": ""
}
```

### POST /v1/render/{job_id}/retry

**Retry Failed Scenes**

Resets specified (or all) failed scenes to pending state and reprocesses them.

**Request Body (Optional):**

```json
{
  "failed_scene_numbers": [2, 5, 8]
}
```

**Response:**

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "retry_started",
  "message": "Failed scenes have been queued for retry"
}
```

## Channel Definitions

### "kenburns" Channel

Uses existing Ken Burns video generation with configurable keypoints:

- Generates image via Kie.ai Flux-2 Pro
- Generates voiceover via Kokoro TTS
- Creates Ken Burns video from keypoints and pan direction
- Supports customizable zoom and pan animations

### "animated" Channel

Uses Kie.ai image-to-video animation:

- Generates image via Kie.ai Flux-2 Pro
- Generates voiceover via Kokoro TTS
- Animates image using ByteDance/Wan model with optional animation_prompt
- Loops or trims video to match exact audio duration

## Configuration

### Environment Variables

| Variable               | Default                     | Description                                 |
| ---------------------- | --------------------------- | ------------------------------------------- |
| `KIE_AI_API_KEY`       | -                           | Kie.ai API key (required)                   |
| `KIE_AI_BASE_URL`      | `https://api.kie.ai/api/v1` | Kie.ai API base URL                         |
| `KIE_AI_CONCURRENCY`   | `5`                         | Number of scenes to process in parallel     |
| `KIE_AI_TIMEOUT`       | `300`                       | Timeout for image generation in seconds     |
| `KIE_AI_POLL_INTERVAL` | `5`                         | Polling interval for async tasks in seconds |
| `RENDER_TEMP_DIR`      | `/tmp/media-master`         | Directory for temporary files               |
| `RENDER_CLEANUP`       | `true`                      | Clean up temp files after completion        |

## Scene Processing Workflow

```
┌─────────────────────────────────────────────────────────────┐
│ POST /v1/render (1+ scenes, channel, settings)              │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
         ┌────────────────────────────────────┐
         │ Create RenderJob, return job_id    │
         │ Start async processing             │
         └────────────────────────────────────┘
                          │
                          ▼
    ┌──────────────────────────────────────────────┐
    │ Process scenes in parallel batches (size: 5) │
    └──────────────────────────────────────────────┘
              │ For each scene:
              │
              ├─→ Generate image (Kie.ai Flux-2)
              │
              ├─→ Generate voiceover (Kokoro TTS)
              │
              ├─→ Get audio duration (ffprobe)
              │
              ├─→ Generate video:
              │   ├─ Ken Burns: existing service
              │   └─ Animated: Kie.ai Wan model
              │
              ├─→ Assemble scene (ffmpeg: video+audio)
              │
              ├─→ Upload to S3
              │
              └─→ Update scene status
                          │
                          ▼
        ┌────────────────────────────────────────┐
        │ All scenes processed                   │
        │ Concatenate clips with transitions     │
        │ Add/loop background music (-stream_loop)
        │ Fade out last 3 seconds                │
        │ Upload final video                     │
        │ Fire webhook (if provided)             │
        └────────────────────────────────────────┘
```

## Usage Example

### 1. Start a render job:

```bash
curl -X POST http://localhost:8000/v1/render \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_api_key" \
  -d '{
    "project_name": "Story Time",
    "channel": "kenburns",
    "settings": {
      "aspect_ratio": "9:16",
      "resolution": "1K",
      "fps": 30,
      "background_music_volume": 0.2,
      "transition_type": "cut"
    },
    "scenes": [
      {
        "scene_number": 1,
        "image_prompt": "A beautiful castle in the clouds",
        "narration_text": "Once upon a time, there was a magical castle",
        "voice_id": "af_heart"
      }
    ]
  }'
```

### 2. Monitor progress:

```bash
curl http://localhost:8000/v1/render/550e8400-e29b-41d4-a716-446655440000/status \
  -H "X-API-Key: your_api_key"
```

### 3. Retry failed scenes:

```bash
curl -X POST http://localhost:8000/v1/render/550e8400-e29b-41d4-a716-446655440000/retry \
  -H "X-API-Key: your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"failed_scene_numbers": [2, 5]}'
```

## Key Features

### ✅ Implemented

1. **Multi-scene rendering** - Process 1-100+ scenes per job
2. **Parallel processing** - Configurable batch size (default: 5 scenes)
3. **Two render channels** - "kenburns" and "animated"
4. **Kie.ai integration** - Image generation and animation
5. **Existing service reuse** - Kokoro TTS, Ken Burns, concatenation, etc.
6. **Progress tracking** - Per-scene and overall progress
7. **Status endpoint** - Detailed job status with scene breakdowns
8. **Retry capability** - Retry specific or all failed scenes
9. **Webhook notifications** - Optional POST callback on completion
10. **Background music** - Download, loop, and mix with fade out
11. **Transition support** - Cut, crossfade between scenes
12. **Temporary file management** - Cleanup after completion
13. **S3 integration** - Upload all intermediate and final videos

### Storage & Cleanup

- Intermediate files in `{RENDER_TEMP_DIR}/{job_id}/`
- Final videos in S3 at `renders/{job_id}/final.mp4`
- Automatic cleanup when `RENDER_CLEANUP=true`

## Future Enhancements

1. Add timestamp tracking to RenderJob (created_at, updated_at)
2. Database persistence for job history
3. Subtitle generation from narration text
4. Advanced transition effects (dissolve, wipe, etc.)
5. Video quality/bitrate settings
6. Batch retry with automatic exponential backoff
7. Job persistence across server restarts
8. Cost estimation before rendering

## Testing Notes

1. Ensure Kie.ai API key is set: `export KIE_AI_API_KEY=your_key`
2. Verify S3/MinIO credentials before testing
3. Ensure ffmpeg and ffprobe are installed
4. Test with small batch sizes first
5. Monitor logs for detailed error tracking: `tail -f app.log`

## Dependencies

No new Python dependencies added. The implementation uses:

- Existing: `aiohttp`, `ffmpeg-python`, `pillow`, `boto3`/`minio`
- New environment variables configuration only
