"""
Shared plumbing for the recap chain: scratch dirs, ctx persistence, async
FFmpeg helpers, and movie download.

The chain passes a single `ctx` dict step to step and persists it to
{scratch}/ctx.json after each step, so a failed job can be inspected and the
steps re-run individually.
"""
import asyncio
import json
import logging
import os
import shutil
from typing import Any, Dict, Optional

import aiohttp

from app.services.recap.config import RECAP_FFMPEG_NICE, RECAP_SCRATCH_ROOT
from app.utils.storage import storage_manager

logger = logging.getLogger(__name__)


# --- Scratch dir + ctx ---


def scratch_dir(project_id: str) -> str:
    d = os.path.join(RECAP_SCRATCH_ROOT, project_id)
    os.makedirs(d, exist_ok=True)
    return d


def save_ctx(ctx: Dict[str, Any]) -> None:
    path = os.path.join(scratch_dir(ctx["payload"]["project_id"]), "ctx.json")
    with open(path, "w") as f:
        json.dump(ctx, f, indent=2, default=str)


def cleanup_scratch(project_id: str) -> None:
    shutil.rmtree(os.path.join(RECAP_SCRATCH_ROOT, project_id), ignore_errors=True)


# --- Subprocess / FFmpeg (async: run in default executor, niced) ---


def _run_sync(cmd: list) -> str:
    import subprocess

    full_cmd = ["nice", "-n", str(RECAP_FFMPEG_NICE), *cmd]
    logger.debug("$ %s", " ".join(full_cmd))
    proc = subprocess.run(full_cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd[:8])}…\n{proc.stderr[-2000:]}"
        )
    return proc.stdout


async def run_cmd(cmd: list) -> str:
    return await asyncio.get_event_loop().run_in_executor(None, _run_sync, cmd)


async def ffmpeg(args: list) -> None:
    await run_cmd(["ffmpeg", "-hide_banner", "-y", *args])


async def ffprobe_json(path: str) -> dict:
    out = await run_cmd(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            path,
        ]
    )
    return json.loads(out)


async def media_duration(path: str) -> Optional[float]:
    try:
        info = await ffprobe_json(path)
        return float(info["format"]["duration"])
    except (RuntimeError, KeyError, ValueError):
        return None


async def has_audio_stream(path: str) -> bool:
    """Check whether a media file has at least one audio stream."""
    try:
        info = await ffprobe_json(path)
    except RuntimeError:
        return False
    streams = info.get("streams", [])
    return any(s.get("codec_type") == "audio" for s in streams)


# --- Downloads ---


async def download_url(url: str, dest: str) -> str:
    """Stream a (presigned) URL to disk — movies are multi-GB, never buffer."""
    logger.info("downloading %s -> %s", url.split("?")[0], dest)
    timeout = aiohttp.ClientTimeout(total=None, sock_read=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in resp.content.iter_chunked(1 << 20):
                    f.write(chunk)
    return dest


async def download_source(source: Dict[str, Any], url_key: str, s3_key: str, dest: str) -> str:
    """Prefer the presigned URL from the payload (48h TTL, set by Recap Studio);
    fall back to fetching the S3 key through storage_manager (same bucket)."""
    if source.get(url_key):
        return await download_url(source[url_key], dest)
    key = source.get(s3_key)
    if not key:
        raise ValueError(f"source has neither {url_key} nor {s3_key}")
    url = await asyncio.get_event_loop().run_in_executor(
        None, storage_manager.get_file_url, key
    )
    return await download_url(url, dest)
