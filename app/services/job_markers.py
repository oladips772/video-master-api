"""
Lightweight file-based markers for cross-restart job reconciliation.

Both render pipelines (recap's job_queue-based chain, and the kenburns/
documentary RenderService) are in-memory only — if the container restarts
mid-render (deploy, crash, OOM-kill), every in-progress job simply vanishes
with no notification sent anywhere, leaving the caller's project stuck at
"rendering" forever.

Fix: each pipeline writes a small marker file here when a job starts and
removes it when the job reaches a terminal state (completed/failed/
cancelled). On the NEXT process startup, `reconcile_orphaned_jobs()` scans
for leftover markers — these can only exist if the process that wrote them
died before reaching a terminal state — and fires a best-effort "failed"
notification to whatever URL was recorded, then removes the marker. This
covers both graceful restarts (where a shutdown handler might not run in
time) and hard crashes (where no shutdown handler runs at all).

No new storage dependency: markers are just one JSON file per job in a
directory under the existing temp/scratch root.
"""
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

JOB_MARKERS_DIR = os.environ.get("JOB_MARKERS_DIR", "/tmp/media-master/job_markers")

INTERRUPTED_ERROR = "Render interrupted by server restart"


def _marker_path(job_id: str) -> str:
    return os.path.join(JOB_MARKERS_DIR, f"{job_id}.json")


def write_marker(
    job_id: str,
    job_type: str,
    notify_url: Optional[str],
    project_id: Optional[str] = None,
) -> None:
    """Record that a job has started.

    `notify_url` is whichever URL that pipeline uses to report terminal
    status (recap's callback_url, kenburns's webhook_url) — may be None if
    the caller didn't provide one, in which case reconciliation can't notify
    anyone but still cleans up the marker on the next startup.
    """
    os.makedirs(JOB_MARKERS_DIR, exist_ok=True)
    marker = {
        "job_id": job_id,
        "job_type": job_type,
        "project_id": project_id,
        "notify_url": notify_url,
        "started_at": time.time(),
    }
    try:
        with open(_marker_path(job_id), "w") as f:
            json.dump(marker, f)
    except OSError:
        logger.warning("failed to write job marker for %s", job_id, exc_info=True)


def remove_marker(job_id: str) -> None:
    try:
        os.remove(_marker_path(job_id))
    except OSError:
        pass


def list_markers() -> List[Dict[str, Any]]:
    """All currently-recorded in-progress job markers."""
    if not os.path.isdir(JOB_MARKERS_DIR):
        return []
    markers = []
    for name in os.listdir(JOB_MARKERS_DIR):
        if not name.endswith(".json"):
            continue
        path = os.path.join(JOB_MARKERS_DIR, name)
        try:
            with open(path) as f:
                markers.append(json.load(f))
        except (OSError, json.JSONDecodeError):
            logger.warning("skipping unreadable job marker %s", path, exc_info=True)
    return markers


async def notify_terminal(
    notify_url: Optional[str],
    job_id: str,
    project_id: Optional[str],
    status: str,
    error: str,
) -> None:
    """Best-effort terminal-state POST — never raises. Shared by startup
    reconciliation (status="failed", error=INTERRUPTED_ERROR) and live
    cancellation (status="failed", error="Cancelled by user") so both paths
    notify callers with the same minimal, low-risk shape.
    """
    if not notify_url:
        return
    body = {
        "job_id": job_id,
        "project_id": project_id,
        "status": status,
        "error": error,
    }
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(notify_url, json=body) as resp:
                if resp.status >= 400:
                    logger.warning(
                        "terminal notify non-200 (%s) for job %s", resp.status, job_id,
                    )
    except Exception:
        logger.warning("terminal notify failed for job %s", job_id, exc_info=True)


async def reconcile_orphaned_jobs() -> int:
    """Call once at process startup, before accepting new jobs.

    Fires a best-effort "failed: Render interrupted by server restart"
    notification for every marker left over from a previous process that
    never reached a terminal state, then removes the marker regardless of
    whether the notification succeeded (a stuck marker would otherwise be
    re-notified — and re-logged as a warning — on every future restart).

    Returns the number of orphaned jobs found.
    """
    markers = list_markers()
    for marker in markers:
        job_id = marker.get("job_id", "?")
        job_type = marker.get("job_type")
        logger.warning(
            "reconciling orphaned job %s (job_type=%s) — notifying %s",
            job_id, job_type, marker.get("notify_url") or "(no notify_url)",
        )
        # Recap Studio's callback contract uses status:"error" (see
        # deliver.py's report_error); other consumers (e.g. kenburns'
        # webhook_url) use the more generic status:"failed".
        notify_status = "error" if job_type == "recap" else "failed"
        await notify_terminal(
            marker.get("notify_url"), job_id, marker.get("project_id"),
            notify_status, INTERRUPTED_ERROR,
        )
        remove_marker(job_id)
    if markers:
        logger.warning("startup reconciliation: notified %d orphaned job(s)", len(markers))
    return len(markers)
