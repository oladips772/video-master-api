"""
Recap render chain orchestrator.

Runs the six steps sequentially inside one job_queue job (the queue's
in-process asyncio model replaces the Celery chain from the reference
implementation — same order, same ctx handoff):

    resolve_subtitles → generate_script → extract_clips
        → generate_tts → assemble → upload_and_callback

Any step failure POSTs {status:"error", error:"<step>: <msg>"} to the Recap
Studio callback and cleans the scratch dir before re-raising so the job is
marked FAILED with the traceback.
"""
import logging
from typing import Any, Dict

from app.services.recap.assemble import assemble
from app.services.recap.clips import extract_clips
from app.services.recap.deliver import report_error, upload_and_callback
from app.services.recap.script_gen import generate_script
from app.services.recap.subtitles import resolve_subtitles
from app.services.recap.tts import generate_tts
from app.services.recap.utils import save_ctx

logger = logging.getLogger(__name__)

STEPS = [
    ("resolve_subtitles", resolve_subtitles),
    ("generate_script", generate_script),
    ("extract_clips", extract_clips),
    ("generate_tts", generate_tts),
    ("assemble", assemble),
    ("upload_and_callback", upload_and_callback),
]


async def process_recap_job(job_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Job entrypoint for job_queue.add_job."""
    project_id = payload.get("project_id")
    if not project_id:
        raise ValueError("payload.project_id is required")
    if not payload.get("callback_url"):
        raise ValueError("payload.callback_url is required")
    if not (payload.get("source") or {}).get("movie_s3_key") and not (
        payload.get("source") or {}
    ).get("movie_url"):
        raise ValueError("payload.source needs movie_s3_key or movie_url")

    ctx: Dict[str, Any] = {"payload": payload}
    save_ctx(ctx)

    for step_name, step in STEPS:
        logger.info("job=%s project=%s step=%s", job_id, project_id, step_name)
        try:
            result = await step(ctx)
        except Exception as exc:
            logger.exception(
                "job=%s project=%s step=%s failed", job_id, project_id, step_name
            )
            await report_error(payload, step_name, exc)
            raise

        if step_name == "upload_and_callback":
            return result  # final step returns the delivery summary, not ctx
        ctx = result

    raise RuntimeError("recap chain ended without delivery")  # unreachable
