<!-- @format -->

# Multi-Scene Render - Implementation Guide

## What Was Built

A complete multi-scene video rendering system with two processing channels:

### **Channel: "kenburns"**

- Generates custom images via Kie.ai Flux-2 Pro
- Creates zooming/panning Ken Burns effects from provided keypoints
- Uses Pan Direction setting for directional movement
- Per-scene voiceover via Kokoro TTS
- Exact video duration matched to audio

### **Channel: "animated"**

- Generates custom images via Kie.ai Flux-2 Pro
- Animates images via Kie.ai ByteDance/Wan model
- Optional animation_prompt for motion description
- Per-scene voiceover via Kokoro TTS
- Video duration matched to audio (loops or trims)

## Files Modified/Created

### New Files:

1. **app/services/kie_ai.py** (280+ lines)
   - Kie.ai API client
   - Image generation and animation
   - Async polling for task completion

2. **app/services/render.py** (600+ lines)
   - Main render orchestrator
   - Scene batch processing
   - Job state tracking
   - Final assembly logic

3. **app/routes/render.py** (250+ lines)
   - HTTP endpoints
   - Request validation
   - Status reporting

4. **RENDER_IMPLEMENTATION.md**
   - Complete technical documentation

5. **RENDER_EXAMPLES.sh**
   - Executable examples and quick reference

### Modified Files:

1. **app/models.py** (+200 lines)
   - 8 new Pydantic models
   - 1 new JobType enum value

2. **app/main.py**
   - Added render router import
   - Added router registration
   - Updated OpenAPI tags

3. **.env.example**
   - 7 new configuration variables

## Setup Instructions

### 1. Environment Configuration

Create or update your `.env` file with:

```bash
# Kie.ai Configuration
KIE_AI_API_KEY=your_kie_ai_api_key_here
KIE_AI_BASE_URL=https://api.kie.ai/api/v1
KIE_AI_CONCURRENCY=5                    # Scenes to process in parallel
KIE_AI_TIMEOUT=300                      # Max seconds per image/animation
KIE_AI_POLL_INTERVAL=5                  # Check task status every N seconds

# Render Configuration
RENDER_TEMP_DIR=/tmp/media-master       # Where to store temp files
RENDER_CLEANUP=true                     # Clean up after completion
```

### 2. Verify Dependencies

The implementation uses existing dependencies:

- `aiohttp` - for async HTTP requests
- `ffmpeg-python` - for video processing
- `pillow` - for image validation
- `minio` or `boto3` - for S3 storage

All should already be installed. No pip install needed.

### 3. Start the Server

```bash
cd /home/oladipupo-akorede/video-master-api
python -m uvicorn app.main:app --reload --port 8000
```

### 4. Verify Endpoints

Check that new endpoints are registered:

```bash
curl http://localhost:8000/docs  # Swagger UI
```

Look for the "render" tag in the OpenAPI documentation.

## Quick Start

### 1. Start a Simple Render

```bash
curl -X POST http://localhost:8000/v1/render \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_api_key" \
  -d '{
    "project_name": "Test",
    "channel": "kenburns",
    "settings": {
      "aspect_ratio": "16:9",
      "resolution": "1K",
      "fps": 30
    },
    "scenes": [
      {
        "scene_number": 1,
        "image_prompt": "A beautiful sunset",
        "narration_text": "Hello world",
        "voice_id": "af_heart"
      }
    ]
  }'
```

Response:

```json
{
  "job_id": "abc123...",
  "status": "pending",
  "total_scenes": 1,
  "monitor_url": "/v1/render/abc123.../status"
}
```

### 2. Check Progress

```bash
curl http://localhost:8000/v1/render/abc123.../status \
  -H "X-API-Key: your_api_key"
```

Response will show:

- Overall status and progress %
- Per-scene status
- Final video URL when complete

### 3. View Full Status

```bash
curl http://localhost:8000/v1/render/abc123.../status \
  -H "X-API-Key: your_api_key" | jq .
```

## Advanced Features

### Webhook Notifications

When a render completes, if you provided a `webhook_url`, it will POST:

```json
{
  "job_id": "abc123...",
  "status": "completed",
  "total_scenes": 30,
  "completed_scenes": 30,
  "failed_scenes": 0,
  "final_video_url": "https://s3.example.com/renders/.../final.mp4",
  "error": null
}
```

### Retry Failed Scenes

If some scenes fail, retry them:

```bash
# Retry all failed scenes
curl -X POST http://localhost:8000/v1/render/abc123.../retry \
  -H "X-API-Key: your_api_key"

# Or retry specific scenes
curl -X POST http://localhost:8000/v1/render/abc123.../retry \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_api_key" \
  -d '{"failed_scene_numbers": [2, 5]}'
```

### Background Music

Add a looping background track:

```json
{
  "settings": {
    "background_music": "https://example.com/music.mp3",
    "background_music_volume": 0.2,  // 0.0 = silent, 1.0 = full
    ...
  }
}
```

The background music will:

1. Download from the provided URL
2. Loop with ffmpeg `-stream_loop -1`
3. Trim/extend to match final video length
4. Fade out the last 3 seconds
5. Mix with scene audio at specified volume

### Transitions Between Scenes

Configure how scenes blend together:

```json
{
  "settings": {
    "transition_type": "cut", // "cut", "crossfade", "dissolve"
    "transition_duration_ms": 500 // milliseconds
  }
}
```

### Ken Burns Keypoints (Advanced)

Define camera movement for Ken Burns channel:

```json
{
  "scene_number": 1,
  "image_prompt": "...",
  "ken_burns_keypoints": [
    { "x": 0.5, "y": 0.4, "zoom": 1.0 }, // Start: center, no zoom
    { "x": 0.3, "y": 0.5, "zoom": 1.2 }, // Middle: move left, zoom 1.2x
    { "x": 0.2, "y": 0.6, "zoom": 1.5 } // End: more left, zoom 1.5x
  ],
  "pan_direction": "left" // Direction hint
}
```

X/Y range: 0.0 to 1.0 (0=edge, 1=opposite edge) Zoom range: 1.0 to 3.0 (1=no
zoom, 3=3x magnification)

### Animation Prompts (Animated Channel)

Describe the motion for image animation:

```json
{
  "scene_number": 1,
  "image_prompt": "A red dragon in the sky",
  "animation_prompt": "The dragon slowly turns its head left and breathes fire downward",
  "narration_text": "Once the dragon awoke...",
  "voice_id": "af_bella"
}
```

If `animation_prompt` is omitted, it defaults to `image_prompt`.

## Parallel Processing

Scenes are processed in configurable batches (default: 5):

```
Batch 1: Process scenes 1-5 (in parallel, ~10-20 minutes)
Batch 2: Process scenes 6-10 (in parallel, ~10-20 minutes)
...
All: Concatenate + background music + final upload
```

Adjust concurrency in `.env`:

```bash
KIE_AI_CONCURRENCY=5    # scenes per batch (1-10 recommended)
```

## Troubleshooting

### Issue: "KIE_AI_API_KEY environment variable is not set"

**Solution:** Set the environment variable before starting the server:

```bash
export KIE_AI_API_KEY=your_key
python -m uvicorn app.main:app
```

### Issue: "Task did not complete" / Timeouts

**Solutions:**

1. Increase `KIE_AI_TIMEOUT` in .env (default 300s = 5 min)
2. Increase `KIE_AI_POLL_INTERVAL` if you're getting rate limited
3. Reduce scene complexity - ensure image prompts are descriptive but concise

### Issue: "Failed to add background music"

**Solutions:**

1. Verify the background_music URL is publicly accessible
2. Check ffmpeg is installed: `which ffmpeg`
3. Verify S3 credentials for temporary uploads
4. Check disk space in RENDER_TEMP_DIR

### Issue: "S3 upload failed"

**Solutions:**

1. Verify S3 credentials are correct
2. Check S3 bucket exists and is accessible
3. Verify IAM permissions for PutObject
4. Check network connectivity to S3

### Issue: Scene processing seems slow

**Causes & Solutions:**

1. Timeout waiting for Kie.ai
   - Images may be complex - simplify prompts
   - Check Kie.ai service status
2. Batch size too small
   - Increase KIE_AI_CONCURRENCY if system can handle it
3. Audio generation slow
   - Kokoro service might be overloaded
   - Reduce text length per scene

### Issue: "audio duration is NaN" or video duration mismatch

**Solutions:**

1. Verify ffprobe is installed: `which ffprobe`
2. Check narration_text generates valid audio
3. Try simpler text in narration_text
4. Check Kokoro TTS logs for errors

## Logging & Debugging

### View Application Logs

```bash
# If running with uvicorn directly
tail -f /tmp/media_master.log

# Or check console output
# stderr/stdout from the running process
```

### Enable Debug Logging

```bash
export LOG_LEVEL=DEBUG
python -m uvicorn app.main:app --reload --port 8000
```

### Monitor Temp Files

```bash
# Watch temp directory growth
watch -n 5 'du -sh /tmp/media-master/*'

# List all scenes for a job
ls -la /tmp/media-master/{job_id}/
```

### Test Individual Components

```bash
# Test Kie.ai connection
python -c "
import asyncio
from app.services.kie_ai import kie_ai_service
result = asyncio.run(kie_ai_service.generate_image('test'))
print(f'Image bytes: {len(result)}')
"

# Test Kokoro TTS
python -c "
import asyncio
from app.services.audio.text_to_speech import generate_speech
result = asyncio.run(generate_speech('Hello world', 'af_heart'))
print(f'Audio bytes: {len(result)}')
"
```

## Performance Tuning

### For Large Projects (50+ scenes)

```env
# Reduce concurrency to avoid overwhelming system
KIE_AI_CONCURRENCY=2

# Longer timeout for slower systems
KIE_AI_TIMEOUT=600

# Longer polling interval to avoid rate limiting
KIE_AI_POLL_INTERVAL=10

# More disk space for temp files
RENDER_TEMP_DIR=/volumes/media-master
```

### For Fast Iteration

```env
# Higher concurrency
KIE_AI_CONCURRENCY=10

# Shorter timeout (for responsive feedback)
KIE_AI_TIMEOUT=180

# Keep temp files for inspection
RENDER_CLEANUP=false
```

## API Contract Guarantees

1. **Scene Ordering**: Scenes always concatenated in scene_number order
2. **Audio Duration**: Video duration always matches exact audio duration
3. **Idempotency**: Retry on same scenes produces identical output
4. **Atomicity**: Either all scenes succeed or individual scenes can be retried
5. **S3 Persistence**: All outputs remain in S3 (not deleted with cleanup)

## Differences from Single-Scene Endpoints

| Aspect           | Single-Scene         | Multi-Scene Render               |
| ---------------- | -------------------- | -------------------------------- |
| Input            | One image/prompt     | Multiple scenes (1-100+)         |
| Processing       | Synchronous          | Asynchronous                     |
| Response         | Direct URL           | job_id, status endpoint          |
| Parallelization  | N/A                  | 5-scene batches                  |
| Transitions      | N/A                  | Crossfade/cut/dissolve           |
| Background Music | Via separate request | Built in                         |
| Retry            | N/A                  | Reprocess failed scenes          |
| Webhook          | N/A                  | Optional completion notification |

## Next Steps

1. Test with small projects (1-3 scenes)
2. Monitor processing time and resource usage
3. Adjust batch size based on your server capacity
4. Set up webhook handlers for completion notifications
5. Implement UI for scene composition and preview

## Support

For issues or questions:

1. Check logs first: `tail -f /tmp/media_master.log`
2. Verify .env configuration
3. Test Kie.ai and S3 connectivity separately
4. Check ffmpeg installation: `ffmpeg -version`
5. Review examples in `RENDER_EXAMPLES.sh`
