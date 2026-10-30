"""
Integration coverage for the lazy Toggl sync that the unit tests don't reach:
the threaded background worker, HTTP 429 back-off, Toggl->app reconciliation,
and the worker path (enqueue -> worker -> entry re-keyed to its Toggl id).

Uses the mock Toggl provider (settings.TESTING is True in test_settings).
"""
import os
import time
import threading

import pytest

from datetime import datetime, timedelta

from tracklater.models import Entry, SyncJob
from tracklater.timemodules.toggl import Parser, Provider, MODULE_NAME, QuotaExceeded
from tracklater import sync_worker

DIRECTORY = os.path.dirname(os.path.realpath(__file__))


@pytest.fixture()
def app(db):
    from flask import Flask
    flask_app = Flask(__name__)
    flask_app.config['SQLALCHEMY_DATABASE_URI'] = \
        'sqlite:///{}/database_sync_int.db'.format(DIRECTORY)
    flask_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(flask_app)
    with flask_app.app_context():
        db.create_all()
        yield flask_app
        db.session.remove()
        db.drop_all()
    path = '{}/database_sync_int.db'.format(DIRECTORY)
    if os.path.exists(path):
        os.remove(path)


def _add_draft(db, entry_id, project="group1:Development", toggl_id=None):
    now = datetime.utcnow()
    db.session.add(Entry(
        module=MODULE_NAME, id=entry_id, toggl_id=toggl_id, is_draft=True,
        start_time=now - timedelta(hours=2), end_time=now - timedelta(hours=1),
        title="draft " + entry_id, project=project, group="group1",
    ))
    db.session.commit()


# --------------------------------------------------------------------------- #
# 1. Threaded background worker (real _run loop, not synchronous drain)
# --------------------------------------------------------------------------- #
def test_threaded_worker_processes_queue(app, db):
    _add_draft(db, "tw-1")
    sync_worker.enqueue("tw-1", "create")

    # start_worker() no-ops under TESTING, so drive the real loop directly.
    sync_worker._stop.clear()
    t = threading.Thread(target=sync_worker._run, args=(app,), daemon=True)
    t.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline and SyncJob.query.count() > 0:
            time.sleep(0.1)

        assert SyncJob.query.count() == 0, "worker did not drain the queue"
        synced = Entry.query.filter(Entry.module == MODULE_NAME).first()
        assert synced is not None and synced.is_draft is False
        assert synced.toggl_id == synced.id  # re-keyed to its Toggl id
    finally:
        sync_worker.stop_worker()
        t.join(timeout=5)


# --------------------------------------------------------------------------- #
# 2. HTTP 429 back-off
# --------------------------------------------------------------------------- #
def test_parse_reset_seconds_from_header():
    class FakeResp:
        def __init__(self, val):
            self.headers = {'X-Toggl-Quota-Resets-In': val}
    assert Provider._parse_reset_seconds(FakeResp('42')) == 42
    assert Provider._parse_reset_seconds(FakeResp('12.9')) == 12
    assert Provider._parse_reset_seconds(FakeResp('')) == 60      # default
    assert Provider._parse_reset_seconds(FakeResp('weird')) == 60  # default


def test_quota_exceeded_keeps_job_pending(app, db):
    _add_draft(db, "q-1")
    sync_worker.enqueue("q-1", "create")

    class QuotaParser(Parser):
        def push_entry(self, entry, toggl_id=None):
            raise QuotaExceeded(7)

    with pytest.raises(QuotaExceeded) as exc:
        sync_worker.process_pending_jobs(parser=QuotaParser(None, None))
    assert exc.value.reset_seconds == 7
    # Quota is a back-off, not a failure: the job survives for retry.
    job = SyncJob.query.filter(SyncJob.entry_id == "q-1").first()
    assert job is not None and job.status == "pending"


def test_non_quota_failure_marks_job_failed_and_keeps_draft(app, db):
    _add_draft(db, "f-1")
    sync_worker.enqueue("f-1", "create")

    class BoomParser(Parser):
        def push_entry(self, entry, toggl_id=None):
            raise RuntimeError("toggl 500")

    processed = sync_worker.process_pending_jobs(parser=BoomParser(None, None))
    assert processed == 0
    job = SyncJob.query.filter(SyncJob.entry_id == "f-1").first()
    assert job.status == "failed" and "toggl 500" in (job.error or "")
    # Entry stays a draft so the user can retry.
    entry = Entry.query.filter(Entry.id == "f-1").first()
    assert entry.is_draft is True


# --------------------------------------------------------------------------- #
# 3. Toggl -> app reconciliation: a local draft supersedes the fetched copy
# --------------------------------------------------------------------------- #
def test_fetch_skips_entries_superseded_by_local_draft(app, db):
    # Mock me/time_entries returns ids "1","2","3". Seed a draft linked to "2".
    db.session.add(Entry(
        module=MODULE_NAME, id="2", toggl_id="2", is_draft=True,
        start_time=datetime(2019, 5, 13, 7, 42, 55),
        end_time=datetime(2019, 5, 13, 8, 34, 52),
        title="locally edited", project="group1:Development", group="group1",
    ))
    db.session.commit()

    parser = Parser(datetime(2019, 5, 1), datetime(2019, 5, 31))
    fetched = parser.get_entries()
    ids = {e.id for e in fetched}
    assert "2" not in ids, "draft-linked Toggl entry must be skipped on fetch"
    assert ids == {"1", "3"}


# --------------------------------------------------------------------------- #
# 4. Full HTTP path: create draft -> save week -> worker -> fetch shows synced
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(app, monkeypatch):
    from tracklater import views
    from tracklater import settings as app_settings
    # Trim modules so /updateentry's Parser() only builds toggl + gitmodule.
    monkeypatch.setattr(app_settings, 'ENABLED_MODULES', ['gitmodule', 'toggl'])
    app.register_blueprint(views.bp)
    return app.test_client()


def test_running_entry_without_end_time_pushes(app, db):
    """A draft with no end_time (running entry) must push as duration=-1, not crash."""
    now = datetime.utcnow()
    db.session.add(Entry(
        module=MODULE_NAME, id="run-1", toggl_id=None, is_draft=True,
        start_time=now - timedelta(hours=1), end_time=None,
        title="running", project="group1:Development", group="group1",
    ))
    db.session.commit()
    sync_worker.enqueue("run-1", "create")
    processed = sync_worker.process_pending_jobs()
    assert processed == 1
    synced = Entry.query.filter(Entry.module == MODULE_NAME, Entry.is_draft == False).first()  # noqa: E712
    assert synced is not None and synced.toggl_id
