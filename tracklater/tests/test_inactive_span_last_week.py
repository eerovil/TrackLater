"""
No local entry gaps >30 min without activity/commits, no overlapping entries,
and every git commit inside a same-project local entry.

Uses last_week_timeline_data.json (2026-05-25 … 2026-06-01) exported from the
real database. Regenerate:

    python3 tracklater/tests/integration/export_timeline_data.py \\
        --from 2026-05-25 --to 2026-06-01 \\
        --out tracklater/tests/integration/last_week_timeline_data.json \\
        --second-commit-group ""
"""
import pytest

from tracklater import create_app
from tracklater.database import db
from tracklater.models import Entry
from tracklater.work_inference import (
    MIN_WORK_BEFORE_COMMIT,
    assert_commits_inside_entries,
    assert_no_commits_in_same_project_gaps,
    assert_no_inactive_span_over,
    assert_no_overlapping_entries,
    assert_pre_commit_windows,
    build_entries_from_commits,
)
from tracklater.tests.integration.timeline_fixture import (
    LOCAL_GROUPS,
    allowed_projects,
    last_week_range,
    seed_last_week,
)

DIRECTORY = __import__('os').path.dirname(__import__('os').path.realpath(__file__))
app = create_app()


@pytest.fixture
def client(db):
    app.config['SQLALCHEMY_DATABASE_URI'] = (
        'sqlite:///{}/database_inactive_span_week.db'.format(DIRECTORY)
    )
    with app.app_context():
        db.create_all()
    yield app.test_client()
    path = '{}/database_inactive_span_week.db'.format(DIRECTORY)
    if __import__('os').path.exists(path):
        __import__('os').remove(path)


def test_last_week_entries_no_inactive_span_over_30_minutes(client, monkeypatch):
    from tracklater import settings as app_settings

    monkeypatch.setattr(app_settings, 'KIMAI', LOCAL_GROUPS)
    start, end = last_week_range()
    allowed = allowed_projects()

    with app.app_context():
        seed_last_week()
        entries = build_entries_from_commits(start, end, allowed)
        assert entries, 'Expected entries from last-week fixture'

        aw_rows = Entry.query.filter(
            Entry.module == 'activitywatch',
            Entry.start_time >= start - MIN_WORK_BEFORE_COMMIT,
            Entry.start_time <= end,
        ).all()
        commits = [
            (row.start_time, row.group or '')
            for row in Entry.query.filter(
                Entry.module == 'gitmodule',
                Entry.start_time >= start,
                Entry.start_time <= end,
            ).order_by(Entry.start_time)
        ]

        assert_no_inactive_span_over(entries, aw_rows, commits)
        assert_no_overlapping_entries(entries)
        assert_commits_inside_entries(entries, commits)
        assert_no_commits_in_same_project_gaps(entries, commits)
        assert_pre_commit_windows(entries, commits)


def test_may_26_commits_inside_local_entries(client, monkeypatch):
    """Every git commit on 2026-05-26 must fall inside a same-project local entry."""
    from datetime import datetime

    from tracklater import settings as app_settings

    monkeypatch.setattr(app_settings, 'KIMAI', LOCAL_GROUPS)
    start, end = last_week_range()
    allowed = allowed_projects()
    day_start = datetime(2026, 5, 26)
    day_end = datetime(2026, 5, 27)

    with app.app_context():
        seed_last_week()
        entries = build_entries_from_commits(start, end, allowed)
        commits = [
            (row.start_time, row.group or '')
            for row in Entry.query.filter(
                Entry.module == 'gitmodule',
                Entry.start_time >= day_start,
                Entry.start_time < day_end,
            ).order_by(Entry.start_time)
        ]
        assert commits, 'Fixture should include May 26 git commits'
        assert_commits_inside_entries(entries, commits)
        assert_no_commits_in_same_project_gaps(entries, commits)
