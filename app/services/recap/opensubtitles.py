"""
OpenSubtitles.com integration for the recap pipeline.

Sits between embedded-subtitle extraction and Whisper transcription in
`resolve_subtitles`: when the movie has no user-uploaded .srt and no embedded
text subtitle track, we clean the source filename (or fall back to the project
title) into a search query and try OpenSubtitles.com for a matching .srt
before paying for a Whisper pass.

The Recap Studio payload is expected to carry the original upload filename at
`source.movie_filename` so the cleaner has good input; when that field is
missing we fall back to `payload.title`, which will only match reliably when
the title happens to be close to the canonical movie name.

Free tier limits: ~1 request/sec, 5 downloads/day. All errors are caught and
logged so a miss silently falls through to Whisper.
"""
import asyncio
import logging
import os
import re
from typing import List, Optional

import aiohttp

logger = logging.getLogger(__name__)

OPENSUBTITLES_API_KEY = os.environ.get("OPENSUBTITLES_API_KEY", "")
OPENSUBTITLES_USER = os.environ.get("OPENSUBTITLES_USER", "")
OPENSUBTITLES_PASS = os.environ.get("OPENSUBTITLES_PASS", "")
OPENSUBTITLES_BASE = "https://api.opensubtitles.com/api/v1"
OPENSUBTITLES_USER_AGENT = os.environ.get(
    "OPENSUBTITLES_USER_AGENT", "RecapStudio v1.0"
)

# Rate limit: OpenSubtitles free tier allows ~1 req/sec.
_RATE_LIMIT_SLEEP_SEC = 1.0

_VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".mov", ".webm")

# Release-info tokens that show up in torrent-style filenames. WEB-DL kept with
# its dash so it survives the "words + hyphens" tokenizer. Matched case-
# insensitively with word boundaries so proper words like "AAC" in "MAACHIS"
# don't get eaten.
_JUNK_WORDS: List[str] = [
    "WEBRip", "WEB-DL", "BluRay", "BRRip", "HDRip", "DVDRip", "HDTV",
    "x264", "x265", "H264", "H265", "HEVC", "AAC", "DTS",
    "2160p", "1080p", "720p", "480p", "4K",
    "REPACK", "EXTENDED", "UNCUT", "DUBBED", "SUBBED",
    "DOWNLOADED", "FROM",
]

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_PARENS_RE = re.compile(r"\([^)]*\)")
_BRACKETS_RE = re.compile(r"\[[^\]]*\]")
_STRAY_BRACKETS_RE = re.compile(r"[\[\]()]")
_SEPARATOR_DASH_RE = re.compile(r"\s+-+\s+")
_MULTI_SPACE_RE = re.compile(r"\s+")
_JUNK_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _JUNK_WORDS) + r")\b",
    re.IGNORECASE,
)


def clean_movie_filename(filename: str) -> str:
    """Extract a searchable movie title from a torrent-style filename.

    Strategy: strip known video extensions, dotify to spaces, truncate at the
    first release-year (release info follows the year), drop bracket/paren
    tags, then sweep any remaining release-info junk. Hyphenated words like
    "Pout-Pout" are preserved; only " - " separators become spaces.
    """
    if not filename:
        return ""

    name = filename.strip()

    # 1. Strip a known video extension.
    for ext in _VIDEO_EXTS:
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break

    # 2. Dots/underscores → spaces (revealing the year and junk tokens).
    name = name.replace(".", " ").replace("_", " ")

    # 3. Truncate at the first 4-digit release year (release info follows).
    year_match = _YEAR_RE.search(name)
    if year_match:
        name = name[: year_match.start()]

    # 4. Drop bracket/paren content and any stragglers from the truncation.
    name = _PARENS_RE.sub(" ", name)
    name = _BRACKETS_RE.sub(" ", name)
    name = _STRAY_BRACKETS_RE.sub(" ", name)

    # 5. Sweep leftover release-info junk (catches files with no year).
    name = _JUNK_RE.sub(" ", name)

    # 6. " - " separators → space; hyphenated words untouched.
    name = _SEPARATOR_DASH_RE.sub(" ", name)

    # 7. Normalize whitespace.
    return _MULTI_SPACE_RE.sub(" ", name).strip()


async def fetch_subtitles_from_opensubtitles(
    movie_title: str, dest_dir: str
) -> Optional[str]:
    """Search OpenSubtitles by title, download best English match to
    `dest_dir/opensubtitles.srt`, and return that path. Any failure returns
    None so the pipeline can fall through to Whisper.
    """
    if not (OPENSUBTITLES_API_KEY and OPENSUBTITLES_USER and OPENSUBTITLES_PASS):
        logger.info("OpenSubtitles credentials not configured, skipping")
        return None

    if not movie_title:
        return None

    headers = {
        "Api-Key": OPENSUBTITLES_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": OPENSUBTITLES_USER_AGENT,
    }

    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # 1. Login.
            async with session.post(
                f"{OPENSUBTITLES_BASE}/login",
                json={
                    "username": OPENSUBTITLES_USER,
                    "password": OPENSUBTITLES_PASS,
                },
                headers=headers,
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "OpenSubtitles login failed: %s %s",
                        resp.status, (await resp.text())[:200],
                    )
                    return None
                token = (await resp.json()).get("token")
                if not token:
                    logger.warning("OpenSubtitles login returned no token")
                    return None

            auth_headers = {**headers, "Authorization": f"Bearer {token}"}

            # 2. Search — English, most-downloaded first.
            async with session.get(
                f"{OPENSUBTITLES_BASE}/subtitles",
                params={
                    "query": movie_title,
                    "languages": "en",
                    "order_by": "download_count",
                    "order_direction": "desc",
                },
                headers=auth_headers,
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "OpenSubtitles search failed: %s %s",
                        resp.status, (await resp.text())[:200],
                    )
                    return None
                search_data = await resp.json()

            results = search_data.get("data") or []
            if not results:
                logger.info("OpenSubtitles: no results for '%s'", movie_title)
                return None

            best = results[0]
            attrs = best.get("attributes") or {}
            files = attrs.get("files") or []
            if not files:
                logger.info(
                    "OpenSubtitles: top result has no downloadable files for '%s'",
                    movie_title,
                )
                return None
            file_id = files[0].get("file_id")
            if not file_id:
                logger.warning("OpenSubtitles: top result missing file_id")
                return None

            sub_title = (
                (attrs.get("feature_details") or {}).get("title") or "unknown"
            )
            logger.info(
                "OpenSubtitles: found '%s' (file_id=%s) for query '%s'",
                sub_title, file_id, movie_title,
            )

            # Respect the 1-req/sec free-tier limit before hitting /download.
            await asyncio.sleep(_RATE_LIMIT_SLEEP_SEC)

            # 3. Request a download link.
            async with session.post(
                f"{OPENSUBTITLES_BASE}/download",
                json={"file_id": file_id},
                headers=auth_headers,
            ) as resp:
                if resp.status == 406:
                    # 406 = daily quota (5/day on free tier) exhausted.
                    logger.warning(
                        "OpenSubtitles: daily download quota exhausted (406)"
                    )
                    return None
                if resp.status != 200:
                    logger.warning(
                        "OpenSubtitles download request failed: %s %s",
                        resp.status, (await resp.text())[:200],
                    )
                    return None
                download_link = (await resp.json()).get("link")
                if not download_link:
                    logger.warning("OpenSubtitles: no download link in response")
                    return None

            # 4. Fetch the actual SRT (a plain CDN URL, no auth).
            async with session.get(download_link) as resp:
                if resp.status != 200:
                    logger.warning(
                        "OpenSubtitles: could not fetch srt (%s)", resp.status
                    )
                    return None
                srt_content = await resp.read()

            if not srt_content:
                logger.warning("OpenSubtitles: empty srt payload")
                return None

            os.makedirs(dest_dir, exist_ok=True)
            srt_path = os.path.join(dest_dir, "opensubtitles.srt")
            with open(srt_path, "wb") as f:
                f.write(srt_content)

            logger.info(
                "OpenSubtitles: saved subtitle to %s (%d bytes)",
                srt_path, len(srt_content),
            )
            return srt_path

    except Exception as exc:
        logger.warning("OpenSubtitles error: %s", exc)
        return None
