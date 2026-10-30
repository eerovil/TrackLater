"""
Background worker that lazily pushes toggl-module drafts to the Toggl API.

Saving a week enqueues a :class:`SyncJob` per draft (deduplicated by entry id).
A single daemon thread drains the queue one job at a time, pacing itself to stay
inside Toggl's rate limit and backing off on HTTP 429. The local database is the
working copy; on a successful push the draft is marked synced (and, for newly
created entries, re-keyed to its Toggl id so a later fetch reconciles cleanly).
"""
import threading
import time
from typing import Optional

from tracklater import settings
from tracklater.database import db
from tracklater.models import Entry, SyncJob
from tracklater.timemodules.toggl import MODULE_NAME, QuotaExceeded
from tracklater.utils import _str

import logging
logger = logging.getLogger(__name__)

# Safe pacing between successive Toggl writes (docs: ~1 req/s is safe).
PUSH_INTERVAL_SECONDS = 1.0
# How long the idle worker waits before re-checking the queue if not woken.
IDLE_POLL_SECONDS = 30.0

_wake = threading.Event()
_stop = threading.Event()
_started = False


def enqueue(entry_id: str, action: str, toggl_id: Optional[str] = None) -> None:
    """Queue a push for an entry, replacing any pending job for the same entry
    (so rapid re-saves collapse to one job carrying the latest intent)."""
    job = SyncJob.query.filter(SyncJob.entry_id == entry_id).first()
    if job is None:
        job = SyncJob(entry_id=entry_id)
        db.session.add(job)
    job.action = action
    job.toggl_id = toggl_id
    job.status = 'pending'
    job.error = None
    db.session.commit()
    _wake.set()


def _entry_for(entry_id: str) -> Optional[Entry]:
    return Entry.query.filter(
        Entry.module == MODULE_NAME, Entry.id == entry_id
    ).first()


def _rekey_to_toggl_id(entry: Entry, toggl_id: str) -> None:
    """Best-effort: re-key a synced entry's row to its Toggl id so a later fetch
    reconciles without a transient duplicate. The id is part of the primary key,
    so replace the row. Safe to skip — the entry is already marked synced and a
    later fetch self-heals — so failures here must not resurrect the draft."""
    if entry.id == toggl_id:
        return
    try:
        replacement = Entry(
            module=MODULE_NAME, id=toggl_id, toggl_id=toggl_id, is_draft=False,
            start_time=entry.start_time, end_time=entry.end_time, title=entry.title,
            project=entry.project, group=entry.group, issue=entry.issue,
            text=entry.text, extra_data=entry.extra_data,
        )
        db.session.delete(entry)
        db.session.flush()
        db.session.merge(replacement)
        db.session.commit()
    except Exception:  # noqa: BLE001 - cosmetic re-key only
        logger.exception("re-key to Toggl id %s failed; entry stays synced under its uuid", toggl_id)
        db.session.rollback()


def _process_job(parser, job: SyncJob) -> None:
    """Push one job. Raises QuotaExceeded to signal a back-off+retry (the job is
    left pending). Any other exception marks the job failed."""
    if job.action == 'delete':
        if job.toggl_id:
            parser.delete_remote(job.toggl_id)
        db.session.delete(job)
        db.session.commit()
        return

    entry = _entry_for(job.entry_id)
    if entry is None:
        # Entry vanished before we got to it; nothing to push.
        db.session.delete(job)
        db.session.commit()
        return

    if job.action == 'create':
        response = parser.push_entry(entry, toggl_id=None)
        new_id = _str(response['id'])
        # Persist the Toggl link and drop the job FIRST so that if anything below
        # fails the entry is no longer a draft and /saveweek won't re-create it
        # (which would duplicate the entry just created in Toggl).
        entry.toggl_id = new_id
        entry.is_draft = False
        db.session.merge(entry)
        db.session.delete(job)
        db.session.commit()
        _rekey_to_toggl_id(entry, new_id)  # best-effort cosmetic re-key
    else:  # update
        response = parser.push_entry(entry, toggl_id=job.toggl_id)
        # If the remote entry was gone, push_entry recreated it with a new id.
        new_id = _str(response['id']) if response and 'id' in response else None
        recreated = new_id is not None and new_id != _str(job.toggl_id)
        if recreated:
            entry.toggl_id = new_id
        entry.is_draft = False
        db.session.merge(entry)
        db.session.delete(job)
        db.session.commit()
        if recreated:
            _rekey_to_toggl_id(entry, new_id)  # best-effort cosmetic re-key


def process_pending_jobs(parser=None, pace: bool = False) -> int:
    """Drain all currently-pending jobs once. Returns the number processed.
    Synchronous and self-contained so it can be driven directly from tests."""
    from tracklater.timemodules.toggl import Parser as TogglParser
    if parser is None:
        parser = TogglParser(None, None)
    processed = 0
    jobs = SyncJob.query.filter(SyncJob.status == 'pending').order_by(SyncJob.created).all()
    for job in jobs:
        try:
            _process_job(parser, job)
            processed += 1
        except QuotaExceeded as exc:
            logger.warning("Toggl quota hit; backing off %ss", exc.reset_seconds)
            db.session.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - leave entry as draft, record why
            logger.exception("Sync job %s failed", job.entry_id)
            db.session.rollback()
            job.status = 'failed'
            job.error = str(exc)
            db.session.commit()
        if pace:
            time.sleep(PUSH_INTERVAL_SECONDS)
    return processed


def _run(app) -> None:
    while not _stop.is_set():
        try:
            with app.app_context():
                while not _stop.is_set():
                    try:
                        process_pending_jobs(pace=True)
                    except QuotaExceeded as exc:
                        if _stop.wait(timeout=exc.reset_seconds):
                            return
                        continue
                    break
        except Exception:  # noqa: BLE001 - worker must never die
            logger.exception("Sync worker cycle errored")
        _wake.wait(timeout=IDLE_POLL_SECONDS)
        _wake.clear()


def stop_worker() -> None:
    """Signal the worker loop to exit (used by tests for clean teardown)."""
    _stop.set()
    _wake.set()


def start_worker(app) -> None:
    """Start the daemon worker thread once. No-op under TESTING."""
    global _started
    if getattr(settings, 'TESTING', False):
        return
    if _started:
        return
    _started = True
    _stop.clear()
    thread = threading.Thread(target=_run, args=(app,), daemon=True, name="toggl-sync")
    thread.start()
    logger.warning("Toggl sync worker started")
