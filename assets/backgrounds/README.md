# Background videos for the Reddit render channel

Drop background video files in this directory. Each file is referenced by
the `background` key in a `POST /v1/render/reddit` request and is mapped
to a path in `app/services/backgrounds.py` (`BACKGROUNDS` dict).

## Expected files

| Background key   | File path                              |
|------------------|----------------------------------------|
| `minecraft`      | `assets/backgrounds/minecraft.mp4`     |
| `subway_surfers` | `assets/backgrounds/subway_surfers.mp4`|
| `gta`            | `assets/backgrounds/gta.mp4`           |
| `satisfying`     | `assets/backgrounds/satisfying.mp4`    |

## Recommendations

- Format: `.mp4`, H.264, AAC (or no audio — the audio track is discarded
  during the loop step).
- Aspect ratio: any. The render pipeline scales + crops to the requested
  output aspect (1080×1920 for 9:16, 1920×1080 for 16:9).
- Length: 3–10 minutes works well. The pipeline uses `-stream_loop -1`
  to loop to exact narration duration, so the source length only matters
  for visual variety.
- Bitrate: ~3–6 Mbps is plenty.
- No watermarks / copyrighted music.

To add a new background key, drop the file here and add an entry to
`BACKGROUNDS` in `app/services/backgrounds.py`.

Files in this directory are **not** committed to git (see `.gitignore`)
since they are large binary assets. Distribute them out-of-band (S3,
shared volume, or upload manually on each server).
