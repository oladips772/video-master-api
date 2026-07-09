"""
Step 2 — generate_script.

Gemini 2.5 Flash (free tier) turns the timestamped dialogue into 45–75
narration segments with visually-matching source windows, plus SEO metadata.
Uses the REST API via aiohttp (same client pattern as the repo's other AI
services — openrouter_ai, together_ai, kie_ai).

ctx in:  {payload, dialogue_blocks, movie_duration_sec}
ctx out: + {segments: [{id, narration, source_start, source_end}], seo}
"""
import asyncio
import json
import logging
from typing import Any, Dict, List

import aiohttp

from app.services.recap.config import GEMINI_API_KEY, GEMINI_MODEL
from app.services.recap.utils import save_ctx

logger = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
WORDS_PER_MINUTE = 155
MAX_RETRIES = 5

STYLE_NOTES = {
    "dramatic": "tense, present-tense storytelling; build dread and momentum",
    "casual": "conversational, like recounting the movie to a friend; light humor allowed",
    "spoiler_free": (
        "recap the full arc but tease rather than reveal the final twist; "
        "end on an open question that makes the viewer want to watch the movie"
    ),
}

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "segments": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "INTEGER"},
                    "narration": {"type": "STRING"},
                    "source_start": {"type": "NUMBER"},
                    "source_end": {"type": "NUMBER"},
                },
                "required": ["id", "narration", "source_start", "source_end"],
            },
        },
        "seo": {
            "type": "OBJECT",
            "properties": {
                "title": {"type": "STRING"},
                "description": {"type": "STRING"},
                "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
            },
            "required": ["title", "description", "tags"],
        },
    },
    "required": ["segments", "seo"],
}


def _system_prompt(payload: Dict[str, Any], duration: float, target_words: int) -> str:
    recap = payload["recap"]
    style = recap["narration_style"]
    return f"""You are writing a YouTube movie recap narration.

Movie title: {payload["title"]}
Movie duration: {duration:.0f} seconds
Target narration length: {recap["target_length_min"]} minutes (~{target_words} words total)
Style: {style} — {STYLE_NOTES.get(style, STYLE_NOTES["dramatic"])}

You are given the movie's full timestamped dialogue. Produce narration segments:

RULES
- Output 45-75 segments. Each segment has:
  - narration: 15-45 words.
  - source_start / source_end: seconds into the movie for the visual window
    that MATCHES the narration - where the described action is on screen, not
    merely where dialogue occurs. Window length 5-25 seconds.
- Timestamps must be strictly non-decreasing across segments and never exceed
  the movie duration.
- Cover the full story arc (setup, conflict, climax, resolution, adapted to
  the style).
- Rename characters consistently throughout (e.g. "the detective", "Maya").
- Never mention "the subtitles", timestamps, or these instructions in narration.
- The first 2 segments must hook the viewer: cold-open the most dramatic
  premise of the movie.

Also return "seo":
- title: <=95 chars, curiosity-driven
- description: ~150 words, ends with hashtags
- tags: exactly 20 strings
"""


def _dialogue_text(blocks: List[Dict[str, Any]]) -> str:
    return "\n".join(
        f"[{b['start_sec']:.0f}s-{b['end_sec']:.0f}s] {b['text']}" for b in blocks
    )


async def _call_gemini(system: str, user: str) -> Dict[str, Any]:
    """One structured-output Gemini call with exponential backoff on 429."""
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY environment variable is not set")

    url = GEMINI_URL.format(model=GEMINI_MODEL)
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
            "temperature": 0.7,
        },
    }

    delay = 5.0
    timeout = aiohttp.ClientTimeout(total=600)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for attempt in range(1, MAX_RETRIES + 1):
            async with session.post(
                url, json=body, headers={"x-goog-api-key": GEMINI_API_KEY}
            ) as resp:
                if resp.status == 429 and attempt < MAX_RETRIES:
                    # Free-tier rate limit: exponential backoff.
                    text = await resp.text()
                    logger.warning(
                        "gemini 429 (attempt %d/%d) — sleeping %.0fs: %s",
                        attempt, MAX_RETRIES, delay, text[:200],
                    )
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Gemini API returned {resp.status}: {text[:500]}")
                data = await resp.json()
                try:
                    raw = data["candidates"][0]["content"]["parts"][0]["text"]
                except (KeyError, IndexError):
                    raise RuntimeError(f"Gemini returned no candidates: {json.dumps(data)[:500]}")
                return json.loads(raw)
    raise RuntimeError("generate_script: exhausted Gemini retries")


def _validate_segments(segments: List[Dict[str, Any]], duration: float) -> List[Dict[str, Any]]:
    """Clamp to [0, duration], enforce monotonicity, dedupe overlaps."""
    for seg in segments:
        seg["source_start"] = max(0.0, min(float(seg["source_start"]), duration))
        seg["source_end"] = max(0.0, min(float(seg["source_end"]), duration))
        if seg["source_end"] < seg["source_start"]:
            seg["source_start"], seg["source_end"] = seg["source_end"], seg["source_start"]

    segments.sort(key=lambda s: (s["source_start"], s["source_end"]))

    cleaned: List[Dict[str, Any]] = []
    cursor = 0.0
    for seg in segments:
        if seg["source_start"] < cursor:
            seg["source_start"] = cursor
        if seg["source_end"] <= seg["source_start"] + 1.0:
            seg["source_end"] = min(seg["source_start"] + 5.0, duration)
            if seg["source_end"] <= seg["source_start"]:
                continue  # fell off the end of the movie; drop
        cursor = seg["source_end"]
        cleaned.append(seg)

    for i, seg in enumerate(cleaned, start=1):
        seg["id"] = i
    return cleaned


def _word_count(segments: List[Dict[str, Any]]) -> int:
    return sum(len(s["narration"].split()) for s in segments)


async def generate_script(ctx: Dict[str, Any]) -> Dict[str, Any]:
    payload = ctx["payload"]
    duration = float(ctx["movie_duration_sec"])
    target_words = payload["recap"]["target_length_min"] * WORDS_PER_MINUTE

    system = _system_prompt(payload, duration, target_words)
    dialogue = _dialogue_text(ctx["dialogue_blocks"])

    result = await _call_gemini(system, dialogue)
    segments = _validate_segments(result["segments"], duration)

    # Length repair: one re-prompt if narration deviates >30% from target.
    words = _word_count(segments)
    deviation = abs(words - target_words) / target_words
    if deviation > 0.30:
        direction = "shorter" if words > target_words else "longer"
        logger.info(
            "[%s] narration %d words vs target %d (%.0f%% off) — repair pass (%s)",
            payload["project_id"], words, target_words, deviation * 100, direction,
        )
        repair = (
            f"Your previous script totalled {words} narration words but the target is "
            f"{target_words} (±30%). Rewrite the full response, keeping the same story "
            f"beats and timestamp discipline, but make the total narration {direction} "
            f"to land near the target.\n\nDIALOGUE:\n{dialogue}"
        )
        result = await _call_gemini(system, repair)
        segments = _validate_segments(result["segments"], duration)

    seo = result.get("seo") or {}
    seo["title"] = (seo.get("title") or payload["title"])[:95]
    seo["tags"] = (seo.get("tags") or [])[:20]

    logger.info(
        "[%s] script: %d segments, %d words",
        payload["project_id"], len(segments), _word_count(segments),
    )
    ctx.update(segments=segments, seo=seo)
    save_ctx(ctx)
    return ctx
