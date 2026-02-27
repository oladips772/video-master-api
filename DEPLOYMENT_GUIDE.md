<!-- @format -->

# Media Master API - Complete Deployment & Usage Guide

## 📋 What This Project Does

The Media Master API is a comprehensive video generation and media processing
service that provides:

### Core Capabilities:

1. **Multi-Scene Video Rendering** (NEW)
   - Create professional videos from multiple scenes (1-100+ scenes)
   - Two render channels: Ken Burns effects or AI-animated videos
   - Automatic image generation from prompts (Kie.ai Flux-2 Pro)
   - Scene-based voiceover generation (Kokoro TTS)
   - Background music mixing with fade effects
   - Scene transitions (cut, crossfade, dissolve)
   - Webhook notifications on completion

2. **Image-to-Video Conversion**
   - Convert static images to videos with zoom effects
   - Optional narrator audio and background music
   - Automatic caption generation
   - Ken Burns camera movement effects

3. **Text-to-Speech (Kokoro TTS)**
   - 30+ natural-sounding voices in multiple languages
   - Audio generation from text
   - High-quality speech synthesis

4. **Media Transcription (Whisper)**
   - Audio/video transcription to text
   - SRT subtitle generation
   - Word-level timestamp support

5. **Video Processing**
   - Video concatenation with transitions
   - Audio mixing and volume control
   - Caption/subtitle overlay
   - S3 cloud storage integration

---

## 🔧 System Requirements

### Minimum Requirements:

- **OS**: Linux, macOS, or Windows with WSL2
- **CPU**: 4 cores (8+ recommended for parallel processing)
- **RAM**: 8GB minimum (16GB+ recommended)
- **Storage**: 50GB+ free space (for temp files and models)
- **Network**: Stable internet connection

### Required Software:

- Docker & Docker Compose (latest version)
- Python 3.10+ (if running without Docker)
- FFmpeg and FFprobe
- Git

### Required Accounts:

- **S3 Storage**: AWS S3 or compatible (MinIO for local development)
- **Kie.ai API Key**: For image generation and animation
- **Optional**: Webhook server for render completion notifications

---

## 📦 Prerequisites & Setup

### 1. Install Docker & Docker Compose

**MacOS/Linux:**

```bash
# Install Docker Desktop from: https://www.docker.com/products/docker-desktop/
# Or use package manager:
# Ubuntu:
sudo apt-get install docker.io docker-compose

# Mac with Homebrew:
brew install docker docker-compose
```

**Windows:**

- Install Docker Desktop with WSL2 backend
- Enable WSL2 in Windows settings

### 2. Get Required API Keys

**Kie.ai API Key:**

1. Go to https://kie.ai/login
2. Create an account or sign in
3. Navigate to API Keys section
4. Copy your API key
5. Store it securely (you'll need it in .env)

**AWS S3 or MinIO:**

- For production: Use AWS S3 credentials
- For development: Use MinIO (included in docker-compose)

---

## 🚀 Local Deployment (Docker)

### Step 1: Clone & Setup

```bash
# Clone the repository
git clone https://github.com/Elvito-AI-Tools/media-master-api.git
cd media-master-api

# Copy environment template
cp .env.example .env
```

### Step 2: Configure .env File

Edit `.env` with your settings:

```bash
# API Configuration
API_KEY=your_super_secret_api_key_here

# App Settings
DEBUG=false
LOG_LEVEL=INFO

# MinIO Configuration (for local development)
MINIO_ROOT_USER=minio
MINIO_ROOT_PASSWORD=minioadmin123

# OR AWS S3 (for production)
S3_ENDPOINT_URL=https://s3.amazonaws.com
S3_REGION=us-east-1
S3_ACCESS_KEY=your_aws_access_key
S3_SECRET_KEY=your_aws_secret_key
S3_BUCKET_NAME=your-bucket-name

# Kokoro TTS Configuration
KOKORO_API_URL=http://kokoro-tts:8880/v1/audio/speech
KOKORO_TIMEOUT=120
KOKORO_MAX_TEXT_LENGTH=1000

# Kie.ai Configuration (IMAGE GENERATION)
KIE_AI_API_KEY=your_kie_ai_api_key_here
KIE_AI_BASE_URL=https://api.kie.ai/api/v1
KIE_AI_CONCURRENCY=5
KIE_AI_TIMEOUT=300
KIE_AI_POLL_INTERVAL=5

# Render Configuration
RENDER_TEMP_DIR=/tmp/media-master
RENDER_CLEANUP=true
```

### Step 3: Start Services with Docker Compose

```bash
# Start all services (MinIO + Kokoro TTS + API)
docker-compose up -d

# Check service status
docker-compose ps

# View logs
docker-compose logs -f api

# Stop services
docker-compose down
```

### Step 4: Verify Installation

```bash
# Test API is running
curl http://localhost:8000/docs

# This should open Swagger UI in your browser
# Or check terminal output for the docs URL
```

---

## 🌐 Production Deployment

### Option 1: AWS EC2 + ECS (Recommended)

#### Prerequisites:

- AWS Account with S3 and EC2 access
- Docker images pushed to ECR (Elastic Container Registry)

#### Steps:

**1. Create S3 Bucket**

```bash
aws s3 mb s3://your-bucket-name --region us-east-1
```

**2. Push Docker Image to ECR**

```bash
# Create ECR repository
aws ecr create-repository --repository-name media-master-api

# Get login token
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin [YOUR_AWS_ACCOUNT_ID].dkr.ecr.us-east-1.amazonaws.com

# Build and push image
docker build -t media-master-api .
docker tag media-master-api:latest [YOUR_AWS_ACCOUNT_ID].dkr.ecr.us-east-1.amazonaws.com/media-master-api:latest
docker push [YOUR_AWS_ACCOUNT_ID].dkr.ecr.us-east-1.amazonaws.com/media-master-api:latest
```

**3. Create ECS Task Definition**

```json
{
  "family": "media-master-api",
  "containerDefinitions": [
    {
      "name": "api",
      "image": "[YOUR_AWS_ACCOUNT_ID].dkr.ecr.us-east-1.amazonaws.com/media-master-api:latest",
      "portMappings": [{ "containerPort": 8000 }],
      "environment": [
        { "name": "API_KEY", "value": "your_api_key" },
        { "name": "S3_BUCKET_NAME", "value": "your-bucket" },
        { "name": "KIE_AI_API_KEY", "value": "your_kie_key" }
      ],
      "memory": 2048,
      "cpu": 1024,
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/media-master",
          "awslogs-region": "us-east-1"
        }
      }
    }
  ],
  "requiresCompatibilities": ["FARGATE"],
  "networkMode": "awsvpc",
  "cpu": "1024",
  "memory": "2048"
}
```

**4. Create Fargate Service**

```bash
# Via AWS Console:
# ECS → Clusters → Create Cluster
# Create Service → Select Task Definition
# Set desired count: 1+ (for auto-scaling)
# Set Load Balancer: Enable ALB
```

### Option 2: DigitalOcean App Platform

1. Push code to GitHub
2. Go to DigitalOcean App Platform
3. Connect GitHub repository
4. Set environment variables in dashboard
5. Deploy

### Option 3: Heroku

```bash
# Install Heroku CLI
curl https://cli.heroku.com/install.sh | sh

# Login
heroku login

# Create app
heroku create your-app-name

# Add buildpack
heroku buildpacks:add heroku/python
heroku buildpacks:add https://github.com/jonathanong/heroku-buildpack-ffmpeg-latest

# Set environment variables
heroku config:set API_KEY=your_key KIE_AI_API_KEY=your_kie_key

# Deploy
git push heroku main
```

---

## 💻 How to Use the API

### 1. Get API Key

Your API key is defined in `.env` as `API_KEY`.

### 2. Access Swagger Documentation

```
http://localhost:8000/docs
```

This provides an interactive interface to test all endpoints.

### 3. Basic Usage Examples

#### Example 1: Multi-Scene Video Rendering (NEW)

```bash
curl -X POST http://localhost:8000/v1/render \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_api_key" \
  -d '{
    "project_name": "My Story",
    "channel": "kenburns",
    "webhook_url": "https://your-domain.com/webhook",
    "settings": {
      "aspect_ratio": "9:16",
      "resolution": "1K",
      "fps": 30,
      "background_music": "https://example.com/music.mp3",
      "background_music_volume": 0.3,
      "subtitle_enabled": true,
      "transition_type": "cut",
      "transition_duration_ms": 500
    },
    "scenes": [
      {
        "scene_number": 1,
        "image_prompt": "A beautiful sunset over mountains",
        "narration_text": "Once upon a time...",
        "voice_id": "af_heart",
        "pan_direction": "right"
      }
    ]
  }'
```

**Response:**

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "pending",
  "total_scenes": 1,
  "monitor_url": "/v1/render/550e8400-e29b-41d4-a716-446655440000/status"
}
```

#### Example 2: Check Render Progress

```bash
curl http://localhost:8000/v1/render/550e8400-e29b-41d4-a716-446655440000/status \
  -H "X-API-Key: your_api_key" | jq .
```

**Response:**

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "processing",
  "total_scenes": 1,
  "completed_scenes": 0,
  "failed_scenes": 0,
  "progress_percent": 45,
  "scenes": [
    {
      "scene_number": 1,
      "status": "processing",
      "progress_percent": 45,
      "error": null,
      "video_url": null
    }
  ],
  "final_video_url": null,
  "final_file_size": null,
  "error": null
}
```

#### Example 3: Text-to-Speech

```bash
curl -X POST http://localhost:8000/v1/audio/text-to-speech \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_api_key" \
  -d '{
    "text": "Hello world",
    "voice": "af_heart"
  }'
```

#### Example 4: Image-to-Video

```bash
curl -X POST http://localhost:8000/v1/image/to-video \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_api_key" \
  -d '{
    "image_url": "https://example.com/image.jpg",
    "video_length": 10,
    "frame_rate": 30,
    "zoom_speed": 10,
    "narrator_speech_text": "A beautiful scene",
    "voice": "af_heart",
    "narrator_vol": 80,
    "should_add_captions": true,
    "background_music_url": "https://example.com/music.mp3",
    "background_music_vol": 20,
    "match_length": "audio"
  }'
```

---

## 📊 API Endpoints Overview

### Render (Multi-Scene) - NEW

| Endpoint                     | Method | Purpose                      |
| ---------------------------- | ------ | ---------------------------- |
| `/v1/render`                 | POST   | Start multi-scene render job |
| `/v1/render/{job_id}/status` | GET    | Check job progress           |
| `/v1/render/{job_id}/retry`  | POST   | Retry failed scenes          |

### Image Processing

| Endpoint                      | Method | Purpose                           |
| ----------------------------- | ------ | --------------------------------- |
| `/v1/image/to-video`          | POST   | Convert image to video with audio |
| `/v1/image/to-video/{job_id}` | GET    | Check image-to-video job status   |
| `/v1/image/add-overlay-image` | POST   | Add image overlay to video        |
| `/v1/image/video-overlay`     | POST   | Add video overlay to video        |

### Audio Processing

| Endpoint                            | Method | Purpose                |
| ----------------------------------- | ------ | ---------------------- |
| `/v1/audio/text-to-speech`          | POST   | Convert text to speech |
| `/v1/audio/text-to-speech/{job_id}` | GET    | Check TTS job status   |

### Video Processing

| Endpoint                          | Method | Purpose                     |
| --------------------------------- | ------ | --------------------------- |
| `/v1/video/concatenate`           | POST   | Concatenate multiple videos |
| `/v1/video/concatenate/{job_id}`  | GET    | Check concatenation status  |
| `/v1/video/add-audio`             | POST   | Add audio track to video    |
| `/v1/video/add-audio/{job_id}`    | GET    | Check audio mixing status   |
| `/v1/video/add-captions`          | POST   | Add captions to video       |
| `/v1/video/add-captions/{job_id}` | GET    | Check caption status        |

### Media Processing

| Endpoint                           | Method | Purpose                    |
| ---------------------------------- | ------ | -------------------------- |
| `/v1/media/transcription`          | POST   | Transcribe audio/video     |
| `/v1/media/transcription/{job_id}` | GET    | Check transcription status |

---

## 🔑 Available Kokoro Voices

**Female Voices:** af_alloy, af_aoede, af_bella, af_heart, af_jadzia,
af_jessica, af_kore, af_nicole, af_nova, af_river, af_sarah, af_sky, af_v0,
af_v0bella, af_v0irulan, af_v0nicole, af_v0sarah, af_v0sky

**Male Voices:** am_adam, am_echo, am_eric, am_fenrir, am_liam, am_michael,
am_onyx, am_puck, am_santa, am_v0adam, am_v0gurney, am_v0michael

**Other Languages:**

- English: ef*\*, em*\*
- French: ff*\*, fm*\*
- German: hf*\*, hm*\*
- Italian: if*\*, im*\*
- Japanese: jf*\*, jm*\*
- Portuguese: pf*\*, pm*\*
- Chinese: zf*\*, zm*\*

---

## 🐛 Troubleshooting

### Issue: "KIE_AI_API_KEY environment variable is not set"

**Solution:**

```bash
# Add to .env file
KIE_AI_API_KEY=your_actual_api_key

# Restart containers
docker-compose restart api
```

### Issue: "S3 connection failed"

**Solution:**

```bash
# Check MinIO is running
docker-compose logs minio

# Verify S3 credentials and endpoint
# For MinIO, use: http://minio:9000 (internal)
# For AWS, use standard endpoint
```

### Issue: "Kokoro TTS service not responding"

**Solution:**

```bash
# Check Kokoro is running
docker-compose logs kokoro-tts

# Wait for model download (first start takes 5-10 minutes)
# Check logs for "Model downloaded successfully"
```

### Issue: "Task did not complete after X seconds" (Image generation timeout)

**Solutions:**

1. Increase `KIE_AI_TIMEOUT` in .env (default: 300s)
2. Simplify image prompts (too complex = slower generation)
3. Reduce batch size: `KIE_AI_CONCURRENCY=2`
4. Check Kie.ai service status: https://status.kie.ai

### Issue: "FFmpeg not found"

**Solution:**

```bash
# Inside container, ffmpeg is pre-installed
# If running locally (not Docker):
# Ubuntu/Debian:
sudo apt-get install ffmpeg

# macOS:
brew install ffmpeg

# Windows (Chocolatey):
choco install ffmpeg
```

### Issue: "Disk space full"

**Solution:**

```bash
# Clear temp files
rm -rf /tmp/media-master/*

# Clean up Docker system
docker system prune -a --volumes

# Check disk usage
df -h
```

---

## 📈 Performance Tuning

### For Large Projects (50+ scenes):

```env
# Reduce concurrency to avoid server overload
KIE_AI_CONCURRENCY=2

# Longer timeout for slower systems
KIE_AI_TIMEOUT=600

# Check status less frequently
KIE_AI_POLL_INTERVAL=10

# More disk space
RENDER_TEMP_DIR=/volumes/large-storage/media-master
```

### For Fast Iteration:

```env
# Higher concurrency
KIE_AI_CONCURRENCY=10

# Shorter timeout
KIE_AI_TIMEOUT=180

# Keep temp files for inspection
RENDER_CLEANUP=false
```

### Docker Resource Limits:

```yaml
# docker-compose.yml
services:
  api:
    deploy:
      resources:
        limits:
          cpus: "2"
          memory: 4G
        reservations:
          cpus: "1"
          memory: 2G
```

---

## 📝 Important Notes

1. **API Key Security**: Never expose your API key in public repositories or
   logs
2. **S3 Costs**: Monitor S3 usage as video files are large
3. **Temporary Files**: Set `RENDER_CLEANUP=true` to avoid disk space issues
4. **Rate Limiting**: Kie.ai has rate limits; adjust polling intervals if needed
5. **Webhook Timeouts**: Webhooks have 10-second timeout; ensure your handler is
   fast
6. **File Retention**: Consider S3 lifecycle policies to expire old videos

---

## 🔒 Security Best Practices

1. **Use environment variables for secrets:**

   ```bash
   # Never commit .env file
   echo ".env" >> .gitignore
   ```

2. **Rotate API keys regularly:**

   ```bash
   # Update in .env and redeploy
   docker-compose restart api
   ```

3. **Use HTTPS in production:**

   ```yaml
   # Add reverse proxy (Nginx/Traefik)
   # Or use AWS ALB with SSL
   ```

4. **Restrict API access:**

   ```bash
   # Only allow requests from known IPs
   # Use API Gateway or WAF
   ```

5. **Monitor logs for suspicious activity:**
   ```bash
   docker-compose logs api | grep "ERROR\|400\|401\|403"
   ```

---

## 🚦 Getting Help

**Check Logs:**

```bash
# API logs
docker-compose logs -f api

# All services
docker-compose logs -f

# Specific service
docker-compose logs -f kokoro-tts
```

**Test Connectivity:**

```bash
# Check API is responding
curl -s http://localhost:8000/ | jq .

# Check S3 connectivity
curl -s http://localhost:49000/minio/bootstrap.html

# Check Kokoro TTS
curl -s http://localhost:8880/docs
```

**Restart Everything:**

```bash
docker-compose down
docker-compose up -d
```

---

## 📚 Additional Resources

- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [Docker Compose Docs](https://docs.docker.com/compose/)
- [Kie.ai API Docs](https://docs.kie.ai)
- [AWS S3 Documentation](https://docs.aws.amazon.com/s3/)
- [Kokoro TTS GitHub](https://github.com/remsky/kokoro-fastapi)
- [FFmpeg Documentation](https://ffmpeg.org/documentation.html)

---

## 🎯 Next Steps

1. **Deploy locally** using Docker
2. **Test basic endpoints** via Swagger UI (/docs)
3. **Create your first render** with 1-2 scenes
4. **Monitor progress** with status endpoint
5. **Scale to production** using AWS/DigitalOcean
6. **Integrate webhooks** for automated workflows
7. **Build your application** on top of the API

Good luck! 🚀
