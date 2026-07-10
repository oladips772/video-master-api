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
import os
import logging
from typing import Any, Dict, List

import aiohttp

from app.services.recap.config import GEMINI_API_KEY, GEMINI_MODEL
from app.services.recap.utils import save_ctx

logger = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# --- Provider toggle: set AI_PROVIDER=groq and GROQ_API_KEY to use Groq ---
AI_PROVIDER = os.environ.get("AI_PROVIDER", "gemini").lower()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

WORDS_PER_MINUTE = 155
MAX_RETRIES = 5

STYLE_NOTES = {
    "dramatic": (
        "Style modifier: dramatic. Still present tense and short sentences — no poetic "
        "language, no metaphors. Allow slightly more intense word choices for stakes and "
        "action verbs (e.g. \"slams\", \"panics\") but keep it plain."
    ),
    "casual": (
        "Style modifier: casual. Even more conversational, like telling a friend. Occasional "
        "light asides are fine (\"and yeah, that goes badly\"). Still present tense, still "
        "short sentences, still no filler."
    ),
    "spoiler_free": (
        "Style modifier: spoiler-free. Cover the full arc, but at the ending tease instead "
        "of revealing the final twist. End on an open question that makes the viewer want "
        "to watch the movie. Still present tense throughout."
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
    style_note = STYLE_NOTES.get(style, STYLE_NOTES["dramatic"])
    return f"""You are writing a YouTube movie recap narration. You will receive timestamped subtitles from a movie.

Movie title: {payload["title"]}
Movie duration: {duration:.0f} seconds
Target narration length: {recap["target_length_min"]} minutes (~{target_words} words total, ~155 words per minute)

STYLE RULES (critical — follow exactly):
- PRESENT TENSE always ("He walks", "She finds", NOT "He walked", "She found")
- Short punchy sentences. Max 20 words per sentence.
- Every sentence advances the plot. Zero filler, zero dramatic flourishes, zero metaphors, zero poetic language.
- Conversational narrator tone — like you're telling a friend what happens in the movie. Example: "So he shuts it down." "It seems paranoia has taken over." "This causes him to fall back."
- Name characters immediately when introduced and use their names consistently throughout.
- Cover the ENTIRE plot — beginning, middle, end, including the ending and any twists. Don't skip subplots or secondary characters.
- Include key dialogue paraphrased naturally: "He tells her that..." "She explains that..." "He mentions that..."
- When a character performs actions, describe them simply: "He grabs a gun", "She drives away", "They hide behind the car"
- Target density: approximately 155 words per minute of target output length

FORMAT:
- Output 60-90 segments (more segments = better scene coverage and more visual variety)
- Each segment: 1-3 short sentences, 15-40 words of narration
- source_start / source_end: the timestamp window in seconds whose VISUALS match what you're narrating. Pick the moment where the action you describe is visually happening on screen, not just where dialogue occurs.
- Timestamps must be non-decreasing across segments and never exceed the movie duration.
- Each clip window: 3-20 seconds (prefer 5-15 second windows)
- Cover the movie chronologically from the opening scene to the final scene. Do not skip ahead or reorder events.

DO NOT:
- Use dramatic, poetic, or literary language
- Use past tense (this is the #1 rule)
- Write long compound sentences with semicolons or em dashes
- Skip any major plot point, subplot, or character introduction
- Add your own commentary, opinions, or moral judgments
- Use phrases like "little did he know", "unbeknownst to", "in a twist of fate", "the weight of", "suffocating", "relentless", "crushing", "haunting"
- Use filler transitions like "Meanwhile", "In the meantime", "As fate would have it"
- Editorialize about character emotions — show through actions instead

EXAMPLE of correct style (from a real YouTube recap):
"Zach wakes up covered in sweat in a motel. He looks out the window and notices that the sun looks weird. Then he goes to the kitchen to drink some water and turns on the radio, but there's only static."

EXAMPLE of WRONG style (what to avoid):
"Deep within the ocean, a creature of pure gloom dwells. He is a sullen swimmer whose face is a permanent mask of misery, haunting the vibrant reef with his relentless, suffocating sorrow."

{style_note}

Also return "seo":
- title: <=95 chars, curiosity-driven
- description: ~150 words, ends with hashtags
- tags: exactly 20 strings
"""


def _dialogue_text(blocks: List[Dict[str, Any]]) -> str:
    return "\n".join(
        f"[{b['start_sec']:.0f}s-{b['end_sec']:.0f}s] {b['text']}" for b in blocks
    )




async def _call_groq(system: str, user: str) -> Dict[str, Any]:
    """Groq call using OpenAI-compatible API with JSON mode."""
    if not GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY environment variable is not set")

    schema_instruction = (
        "\nYou MUST respond with valid JSON only, no markdown, no backticks.\n"
        "Use this exact schema:\n"
        '{"segments": [{"id": int, "narration": str, "source_start": float, "source_end": float}], '
        '"seo": {"title": str, "description": str, "tags": [str]}}'
    )

    body = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system + schema_instruction},
            {"role": "user", "content": user},
        ],
        "temperature": 0.7,
        "response_format": {"type": "json_object"},
        "max_tokens": 8000,
    }

    delay = 5.0
    timeout = aiohttp.ClientTimeout(total=600)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for attempt in range(1, MAX_RETRIES + 1):
            async with session.post(
                GROQ_URL,
                json=body,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
            ) as resp:
                if resp.status in (429, 503) and attempt < MAX_RETRIES:
                    text = await resp.text()
                    logger.warning(
                        "groq %d (attempt %d/%d) — sleeping %.0fs: %s",
                        resp.status, attempt, MAX_RETRIES, delay, text[:200],
                    )
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Groq API returned {resp.status}: {text[:500]}")
                data = await resp.json()
                try:
                    raw = data["choices"][0]["message"]["content"]
                except (KeyError, IndexError):
                    raise RuntimeError(f"Groq returned no choices: {json.dumps(data)[:500]}")
                return json.loads(raw)
    raise RuntimeError("generate_script: exhausted Groq retries")

async def _call_gemini(system: str, user: str) -> Dict[str, Any]:
    """One structured-output Gemini call with exponential backoff on 429/503."""
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
                if resp.status in (429, 503) and attempt < MAX_RETRIES:
                    # Rate-limited (429) or transient overload (503): exponential backoff.
                    text = await resp.text()
                    logger.warning(
                        "gemini %d (attempt %d/%d) — sleeping %.0fs: %s",
                        resp.status, attempt, MAX_RETRIES, delay, text[:200],
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

    if AI_PROVIDER == "groq":
        result = await _call_groq(system, dialogue)
    else:
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
        if AI_PROVIDER == "groq":
            result = await _call_groq(system, repair)
        else:
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
