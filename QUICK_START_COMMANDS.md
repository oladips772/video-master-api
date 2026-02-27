<!-- @format -->

# Media Master API - Quick Start Commands

Complete step-by-step commands to deploy and run the entire project.

## 🚀 QUICK START (5 Minutes)

### Prerequisites Check

```bash
# Check if Docker is installed
docker --version
docker-compose --version

# Check if FFmpeg is installed (needed by services)
ffmpeg -version
```

### Deploy Everything

```bash
# 1. Clone repository
git clone https://github.com/oladips772/video-master-api.git
cd video-master-api

# 2. Copy environment file
cp .env.example .env

# 3. Edit .env with your API keys
nano .env
# (Or use your preferred editor: vim, code, etc.)
# Key variables to set:
#   - API_KEY=your_secret_key
#   - KIE_AI_API_KEY=your_kie_ai_key
#   - S3 credentials (AWS or MinIO)

# 4. Start all services with Docker Compose
docker-compose up -d

# 5. Wait for services to start (30-60 seconds)
sleep 30

# 6. Check if everything is running
docker-compose ps

# 7. Verify API is working
curl http://localhost:2000/docs

# 8. Check logs
docker-compose logs -f api
```

---

## 📋 FULL DEPLOYMENT STEPS

### Step 1: Prerequisites Installation

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install -y docker.io docker-compose git ffmpeg

# Start Docker service
sudo systemctl start docker
sudo systemctl enable docker

# Add current user to docker group (optional, avoid sudo)
sudo usermod -aG docker $USER
# Re-login or: newgrp docker

# Verify installation
docker --version
docker-compose --version
ffmpeg -version
```

### Step 2: Clone & Setup Repository

```bash
# Clone the repository
git clone https://github.com/oladips772/video-master-api.git
cd video-master-api

# Copy environment template
cp .env.example .env

# List files to verify
ls -la
# Should see: .env, docker-compose.yml, requirements.txt, app/, docs/
```

### Step 3: Configure Environment Variables

```bash
# Edit .env file
nano .env
# Or use your editor (vim, code, etc.)

# The file should look like:
```

**Example `.env` content:**

```bash
# API Configuration
API_KEY=super_secret_key_change_this
DEBUG=false
LOG_LEVEL=INFO

# MinIO (Local Development)
MINIO_ROOT_USER=minio
MINIO_ROOT_PASSWORD=minioadmin123

# OR AWS S3 (Production)
# S3_ENDPOINT_URL=https://s3.amazonaws.com
# S3_REGION=us-east-1
# S3_ACCESS_KEY=your_aws_key
# S3_SECRET_KEY=your_aws_secret
# S3_BUCKET_NAME=your-bucket-name

# Kokoro TTS (Text-to-Speech Service)
KOKORO_API_URL=http://kokoro-tts:8880/v1/audio/speech
KOKORO_TIMEOUT=120
KOKORO_MAX_TEXT_LENGTH=1000

# Kie.ai (Image Generation)
KIE_AI_API_KEY=your_kie_ai_api_key_here
KIE_AI_BASE_URL=https://api.kie.ai/api/v1
KIE_AI_CONCURRENCY=5
KIE_AI_TIMEOUT=300
KIE_AI_POLL_INTERVAL=5

# Render Configuration
RENDER_TEMP_DIR=/tmp/media-master
RENDER_CLEANUP=true
```

### Step 4: Start All Services

```bash
# Start services in background
docker-compose up -d

# View running containers
docker-compose ps

# Expected output:
# NAME                 COMMAND                  STATUS
# media-master-api     "python -m uvicorn..."   Up
# kokoro-tts           "python -m uvicorn..."   Up
# minio                "/minio server ..."      Up (if using local MinIO)

# Wait for services to fully initialize
sleep 30
```

### Step 5: Verify Services Are Running

```bash
# Check API is responding
curl http://localhost:2000

# Check Swagger documentation (interactive API docs)
curl http://localhost:2000/docs

# Check MinIO (if local)
curl http://localhost:9000

# Check Kokoro TTS
curl http://localhost:8880

# View logs for all services
docker-compose logs

# View logs for specific service
docker-compose logs api
docker-compose logs kokoro-tts
docker-compose logs minio
```

### Step 6: Test the API

```bash
# Set your API key
API_KEY="super_secret_key_change_this"

# Test 1: Simple health check
curl http://localhost:2000/

# Test 2: Text-to-Speech
curl -X POST http://localhost:2000/v1/audio/text-to-speech \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "text": "Hello world",
    "voice": "af_heart"
  }'

# Test 3: Start a multi-scene render
curl -X POST http://localhost:2000/v1/render \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
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
# Save the job_id from response

# Test 4: Check render progress (replace {job_id})
curl http://localhost:2000/v1/render/{job_id}/status \
  -H "X-API-Key: $API_KEY"
```

---

## 🛑 Stopping Services

```bash
# Stop all services (containers keep running)
docker-compose stop

# Stop and remove containers (keep volumes)
docker-compose down

# Complete cleanup (remove containers, volumes, networks)
docker-compose down -v

# View stopped containers
docker-compose ps -a
```

---

## 📊 Monitoring & Logs

```bash
# Real-time logs for all services
docker-compose logs -f

# Logs for specific service (last 100 lines)
docker-compose logs --tail=100 api
docker-compose logs --tail=100 kokoro-tts
docker-compose logs --tail=100 minio

# Logs with timestamps
docker-compose logs -f --timestamps

# Follow only new logs (skip history)
docker-compose logs -f --follow

# Filter logs
docker-compose logs api | grep "error"
docker-compose logs api | grep "render"
```

---

## 🔄 Common Management Commands

```bash
# Restart all services
docker-compose restart

# Restart specific service
docker-compose restart api
docker-compose restart kokoro-tts

# Rebuild containers (after code changes)
docker-compose build --no-cache api

# Update and restart
docker-compose pull
docker-compose up -d --no-deps --build api

# Check resource usage
docker stats

# Execute command in running container
docker-compose exec api python -c "print('Hello')"
docker-compose exec api bash  # Enter shell

# View container details
docker-compose logs api --follow
docker inspect video-master-api_api_1
```

---

## 📈 Scaling & Performance

```bash
# Adjust parallel scene processing (in .env)
KIE_AI_CONCURRENCY=10  # Default: 5, increase for faster processing

# Increase API worker threads (modify docker-compose.yml)
# Change: python -m uvicorn app.main:app --host 0.0.0.0 --port 2000 --workers 4

# Monitor performance
docker stats --no-stream

# Check disk usage
df -h
du -sh /tmp/media-master

# Cleanup old temp files
rm -rf /tmp/media-master/old_job_ids
```

---

## 🌐 Production Deployment Commands

### Option 1: AWS EC2 Deployment

```bash
# SSH into EC2 instance
ssh -i your-key.pem ubuntu@your-instance-ip

# Install prerequisites
sudo apt update
sudo apt install -y docker.io docker-compose git ffmpeg

# Clone and deploy
git clone https://github.com/oladips772/video-master-api.git
cd video-master-api
cp .env.example .env

# Edit .env with production credentials
nano .env

# Start services
sudo docker-compose up -d

# View logs
sudo docker-compose logs -f api
```

### Option 2: Docker Push to Registry

```bash
# Build image
docker build -t media-master-api .

# Tag image
docker tag media-master-api:latest your-registry/media-master-api:latest

# Push to registry
docker push your-registry/media-master-api:latest

# On production server
docker pull your-registry/media-master-api:latest
docker run -d \
  -p 2000:8000 \
  -e API_KEY=$API_KEY \
  -e KIE_AI_API_KEY=$KIE_AI_API_KEY \
  -e S3_ACCESS_KEY=$S3_ACCESS_KEY \
  --name api \
  your-registry/media-master-api:latest
```

### Option 3: Using systemd for Auto-Start

```bash
# Create systemd service file
sudo nano /etc/systemd/system/media-master.service

# Content:
[Unit]
Description=Media Master API
After=docker.service
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=/home/ubuntu/video-master-api
ExecStart=/usr/bin/docker-compose up
ExecStop=/usr/bin/docker-compose down
Restart=on-failure

[Install]
WantedBy=multi-user.target

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable media-master
sudo systemctl start media-master
sudo systemctl status media-master
```

---

## 🔐 Security Hardening

```bash
# Change API key immediately
API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
echo "API_KEY=$API_KEY" >> .env

# Backup .env file
cp .env .env.backup

# Set file permissions
chmod 600 .env

# Update firewall to restrict access
sudo ufw allow 22/tcp        # SSH
sudo ufw allow 2000/tcp      # API
sudo ufw default deny incoming
sudo ufw enable
```

---

## 📦 Updates & Maintenance

```bash
# Pull latest code
git pull origin main

# Rebuild container with new code
docker-compose build --no-cache api

# Restart services
docker-compose down
docker-compose up -d

# Verify new version is running
docker-compose logs api | head -20

# Check application version
curl http://localhost:2000
```

---

## 🆘 Troubleshooting Commands

```bash
# Check if ports are already in use
netstat -tlnp | grep 2000
netstat -tlnp | grep 8880
netstat -tlnp | grep 9000

# Kill process using port
sudo fuser -k 2000/tcp

# Check disk space
df -h

# Check memory usage
free -h
docker stats

# View container IP
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' container_name

# Test S3 connectivity
aws s3 ls --endpoint-url http://localhost:9000

# Test Kie.ai connectivity
curl -X GET "https://api.kie.ai/api/v1/jobs/recordInfo?taskId=test" \
  -H "Authorization: Bearer YOUR_KEY"

# Check container network
docker network ls
docker network inspect video-master-api_default

# Clean up unused Docker resources
docker system prune -a
docker volume prune
```

---

## 📝 Useful Commands Reference

```bash
# Container operations
docker-compose up -d          # Start in background
docker-compose up             # Start in foreground (see logs)
docker-compose stop           # Stop services
docker-compose down           # Stop and remove
docker-compose restart        # Restart services
docker-compose ps             # List running containers
docker-compose logs -f        # Follow logs

# Building
docker build -t name .        # Build image
docker-compose build          # Build from compose file

# Debugging
docker exec -it id bash       # Enter container shell
docker logs id                # View container logs
docker inspect id             # View container details
docker stats                  # View resource usage

# Cleanup
docker system prune           # Remove unused resources
docker volume prune           # Remove unused volumes
rm -rf /tmp/media-master/*    # Clear temp files
```

---

## 📖 Next Steps

1. **Access API Documentation**
   - http://localhost:2000/docs (Swagger UI)
   - Interactive testing for all endpoints

2. **Read Full Documentation**
   - `DEPLOYMENT_GUIDE.md` - Detailed setup guide
   - `RENDER_SETUP_GUIDE.md` - Multi-scene rendering guide
   - `RENDER_EXAMPLES.sh` - API usage examples

3. **Monitor Logs**

   ```bash
   docker-compose logs -f api
   ```

4. **Test Endpoints**
   - Use Swagger UI at http://localhost:2000/docs
   - Or use curl commands from examples

5. **Production Setup**
   - Setup AWS S3 bucket instead of MinIO
   - Configure domain and SSL/TLS
   - Setup monitoring and alerting
   - Configure auto-scaling

---

## 🐳 Docker Compose Services

| Service      | Port      | Purpose                          |
| ------------ | --------- | -------------------------------- |
| `api`        | 2000      | FastAPI application              |
| `kokoro-tts` | 8880      | Text-to-speech service           |
| `minio`      | 9000/9001 | S3-compatible storage (dev only) |

All services communicate via Docker network: `video-master-api_default`

---

## 💡 Pro Tips

1. **For Local Development:**
   - Use MinIO (included in docker-compose.yml)
   - Set `DEBUG=true` in .env
   - Run `docker-compose logs -f` to see real-time logs

2. **For Production:**
   - Use AWS S3 instead of MinIO
   - Set `DEBUG=false`
   - Enable webhook notifications for job completion
   - Setup auto-scaling for high load
   - Configure health checks

3. **Monitoring:**
   - Watch container logs: `docker-compose logs -f api`
   - Check disk space: `df -h`
   - Monitor resource usage: `docker stats`
   - Track render jobs: `/v1/render/{job_id}/status`

4. **Optimization:**
   - Increase `KIE_AI_CONCURRENCY` for faster processing
   - Use SSD for `/tmp/media-master`
   - Scale horizontally with load balancer
   - Cache frequently used assets

---

## 🎯 Common Use Cases

### Use Case 1: Single Scene Video

```bash
curl -X POST http://localhost:2000/v1/render \
  -H "X-API-Key: $API_KEY" \
  -d '{"project_name":"Test","channel":"kenburns","settings":{"resolution":"1K"},"scenes":[{"scene_number":1,"image_prompt":"sunset","narration_text":"hello","voice_id":"af_heart"}]}'
```

### Use Case 2: Multi-Scene Story (10+ scenes)

See `RENDER_EXAMPLES.sh` for complete example

### Use Case 3: Monitor Rendering Progress

```bash
watch -n 5 "curl -s http://localhost:2000/v1/render/JOB_ID/status -H 'X-API-Key: $API_KEY' | jq '.progress_percent'"
```

### Use Case 4: Batch Processing Multiple Videos

Use retry endpoint to reprocess failed scenes:

```bash
curl -X POST http://localhost:2000/v1/render/JOB_ID/retry \
  -H "X-API-Key: $API_KEY" \
  -d '{"failed_scene_numbers":[2,5,8]}'
```

---

Start with the **Quick Start** section above, then refer to detailed docs as
needed!
