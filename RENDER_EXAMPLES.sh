#!/bin/bash

# Multi-Scene Render API - Quick Reference Examples

# Set your API key and base URL
API_KEY="your_api_key_here"
BASE_URL="http://localhost:8000"

echo "=== Multi-Scene Render API Examples ==="
echo ""

# ============================================================================
# Example 1: Simple Ken Burns render with 2 scenes
# ============================================================================
echo "Example 1: Starting a Ken Burns render with 2 scenes..."
echo ""

curl -X POST "$BASE_URL/v1/render" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "project_name": "My Story",
    "channel": "kenburns",
    "webhook_url": "https://example.com/webhook",
    "settings": {
      "aspect_ratio": "16:9",
      "resolution": "1K",
      "fps": 30,
      "background_music": "https://example.com/music.mp3",
      "background_music_volume": 0.3,
      "subtitle_enabled": true,
      "subtitle_style": "bold_center",
      "transition_type": "cut",
      "transition_duration_ms": 500
    },
    "scenes": [
      {
        "scene_number": 1,
        "image_prompt": "A beautiful sunset over a calm ocean",
        "narration_text": "Once upon a time, in a land far away...",
        "voice_id": "af_heart",
        "pan_direction": "right",
        "ken_burns_keypoints": [
          {"x": 0.5, "y": 0.4, "zoom": 1.0},
          {"x": 0.3, "y": 0.5, "zoom": 1.2}
        ]
      },
      {
        "scene_number": 2,
        "image_prompt": "A mystical forest with ancient trees",
        "narration_text": "There was a mysterious forest that held many secrets.",
        "voice_id": "am_adam",
        "pan_direction": "left",
        "ken_burns_keypoints": [
          {"x": 0.6, "y": 0.5, "zoom": 1.0},
          {"x": 0.4, "y": 0.6, "zoom": 1.3}
        ]
      }
    ]
  }' | tee job_response.json

echo ""
echo "Response saved to job_response.json. Extract the job_id for next steps."
echo ""

# ============================================================================
# Example 2: Animated Channel Render
# ============================================================================
echo "Example 2: Starting an Animated render with custom animation prompts..."
echo ""

curl -X POST "$BASE_URL/v1/render" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "project_name": "Animated Story",
    "channel": "animated",
    "settings": {
      "aspect_ratio": "9:16",
      "resolution": "1K",
      "fps": 30,
      "background_music_volume": 0.2,
      "subtitle_enabled": true,
      "transition_type": "cut",
      "transition_duration_ms": 300
    },
    "scenes": [
      {
        "scene_number": 1,
        "image_prompt": "A dragon flying through clouds",
        "animation_prompt": "The dragon soars gracefully through white fluffy clouds with smooth wingbeats",
        "narration_text": "A mighty dragon awakens...",
        "voice_id": "af_bella"
      },
      {
        "scene_number": 2,
        "image_prompt": "A waterfall in a tropical jungle",
        "animation_prompt": "Water cascades down the cliff with mist and motion",
        "narration_text": "Deep in the jungle, ancient waters flow.",
        "voice_id": "af_bella"
      }
    ]
  }' | jq .

echo ""

# ============================================================================
# Example 3: Check Job Status
# ============================================================================
echo "Example 3: Checking render job status..."
echo ""
echo "Usage: CHECK_JOB_STATUS <job_id>"
echo ""
echo "curl $BASE_URL/v1/render/{job_id}/status \\"
echo "  -H 'X-API-Key: $API_KEY' | jq ."
echo ""
echo "Replace {job_id} with the ID from your render job response."
echo ""

# Example with placeholder
JOB_ID="550e8400-e29b-41d4-a716-446655440000"
echo "Example call:"
echo "curl $BASE_URL/v1/render/$JOB_ID/status -H 'X-API-Key: $API_KEY' | jq ."
echo ""

# ============================================================================
# Example 4: Check Status Periodically
# ============================================================================
echo "Example 4: Monitoring job progress in real-time..."
echo ""
echo "#!/bin/bash"
echo "JOB_ID='$JOB_ID'"
echo "while true; do"
echo "  STATUS=\$(curl -s $BASE_URL/v1/render/\$JOB_ID/status -H 'X-API-Key: $API_KEY' | jq -r '.status')"
echo "  PROGRESS=\$(curl -s $BASE_URL/v1/render/\$JOB_ID/status -H 'X-API-Key: $API_KEY' | jq '.progress_percent')"
echo "  echo \"Status: \$STATUS, Progress: \$PROGRESS%\""
echo "  ["" \"\$STATUS"" == ""completed"" ] && break"
echo "  ["" \"\$STATUS"" == ""failed"" ] && break"
echo "  sleep 5"
echo "done"
echo ""

# ============================================================================
# Example 5: Retry Failed Scenes
# ============================================================================
echo "Example 5: Retrying failed scenes..."
echo ""
echo "# Retry all failed scenes:"
echo "curl -X POST $BASE_URL/v1/render/$JOB_ID/retry \\"
echo "  -H 'X-API-Key: $API_KEY' | jq ."
echo ""
echo "# Retry specific failed scenes:"
curl -X POST "$BASE_URL/v1/render/$JOB_ID/retry" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "failed_scene_numbers": [2, 5, 8]
  }' | jq . 2>/dev/null || echo "Example command (would need real job_id)"

echo ""

# ============================================================================
# Available Kokoro Voices
# ============================================================================
echo "=== Available Kokoro TTS Voices ==="
echo ""
echo "Female Voices:"
echo "  af_alloy, af_aoede, af_bella, af_heart, af_jadzia, af_jessica, af_kore"
echo "  af_nicole, af_nova, af_river, af_sarah, af_sky, af_v0, af_v0bella"
echo "  af_v0irulan, af_v0nicole, af_v0sarah, af_v0sky"
echo ""
echo "Male Voices:"
echo "  am_adam, am_echo, am_eric, am_fenrir, am_liam, am_michael, am_onyx"
echo "  am_puck, am_santa, am_v0adam, am_v0gurney, am_v0michael"
echo ""
echo "Other Languages:"
echo "  English (ef, em), French (ff, fm), German (hf, hm), Italian (if, im)"
echo "  Japanese (jf, jm), Portuguese (pf, pm), Chinese (zf, zm)"
echo ""

# ============================================================================
# Render Settings Reference
# ============================================================================
echo "=== Render Settings Reference ==="
echo ""
echo "aspect_ratio options:"
echo "  '1:1', '16:9', '9:16', '4:3', '3:4'"
echo ""
echo "resolution options:"
echo "  '512', '1K', '1080', '2K', etc."
echo ""
echo "fps options:"
echo "  24, 30, 60 (recommended: 30)"
echo ""
echo "transition_type options:"
echo "  'cut', 'crossfade', 'dissolve'"
echo ""
echo "subtitle_style options:"
echo "  'bold_center', 'highlight', 'word_by_word', etc."
echo ""

# ============================================================================
# Ken Burns Specific
# ============================================================================
echo "=== Ken Burns Channel Details ==="
echo ""
echo "pan_direction options:"
echo "  'left', 'right', 'up', 'down', 'diagonal'"
echo ""
echo "ken_burns_keypoints:"
echo "  - Define camera movement as list of keypoints"
echo "  - Each keypoint: {x: 0-1, y: 0-1, zoom: 1-3}"
echo "  - x/y represent position, zoom is magnification factor"
echo "  - At least 2 keypoints recommended for smooth effect"
echo ""
echo "Example Ken Burns keypoints:"
echo "  Start wide view, pan to center while zooming:"
echo "  ["
echo "    {x: 0.3, y: 0.2, zoom: 1.0},"
echo "    {x: 0.5, y: 0.5, zoom: 1.5},"
echo "    {x: 0.7, y: 0.8, zoom: 2.0}"
echo "  ]"
echo ""

# ============================================================================
# Animated Channel Specific
# ============================================================================
echo "=== Animated Channel Details ==="
echo ""
echo "animation_prompt:"
echo "  - Describes the motion/animation for the image"
echo "  - Examples:"
echo "    * 'smooth camera pan from left to right'"
echo "    * 'water flows gently down the cliff'"
echo "    * 'clouds drift across the sky slowly'"
echo "  - If not provided, uses image_prompt as default"
echo ""

# ============================================================================
# Tips & Tricks
# ============================================================================
echo "=== Tips & Tricks ==="
echo ""
echo "1. Monitor progress continuously:"
echo "   watch -n 5 'curl -s $BASE_URL/v1/render/\$JOB_ID/status -H \"X-API-Key: $API_KEY\" | jq .'"
echo ""
echo "2. Get final video URL when complete:"
echo "   curl -s $BASE_URL/v1/render/\$JOB_ID/status -H 'X-API-Key: $API_KEY' | jq '.final_video_url'"
echo ""
echo "3. Parse scene details:"
echo "   curl -s $BASE_URL/v1/render/\$JOB_ID/status -H 'X-API-Key: $API_KEY' | jq '.scenes[] | {scene: .scene_number, status: .status, progress: .progress_percent}'"
echo ""
echo "4. Check for errors:"
echo "   curl -s $BASE_URL/v1/render/\$JOB_ID/status -H 'X-API-Key: $API_KEY' | jq '.error'"
echo ""
echo "5. Retry with specific scenes:"
echo "   curl -X POST $BASE_URL/v1/render/\$JOB_ID/retry -H 'X-API-Key: $API_KEY' -H 'Content-Type: application/json' -d '{\"failed_scene_numbers\": [1, 2]}'"
echo ""

echo "=== End of Examples ==="
