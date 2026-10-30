from datetime import datetime, timedelta

from tracklater.models import Entry
from tracklater.work_inference import (
    MIN_WORK_AFTER_COMMIT,
    WorkAnchor,
    MAX_LOCAL_ENTRY_DURATION,
    MIN_LOCAL_ENTRY_DURATION,
    _anchors_fit_together,
    _aw_entry_is_clearly_non_work,
    _post_commit_end,
    _segments_within_evidence,
    _session_chunks,
    _trim_overlaps_without_shifting,
    infer_work_window_before_commit,
)


def test_infer_work_window_minimum_30_minutes():
    commit = datetime(2020, 5, 22, 10, 0, 0)
    start, end = infer_work_window_before_commit(commit, [])
    assert end == commit
    assert (commit - start) == timedelta(minutes=30)


def test_infer_work_window_extends_with_not_afk():
    commit = datetime(2020, 5, 22, 10, 0, 0)
    presence = [
        (datetime(2020, 5, 22, 9, 10, 0), datetime(2020, 5, 22, 10, 0, 0), True),
    ]
    start, end = infer_work_window_before_commit(commit, presence)
    assert end == commit
    assert start == datetime(2020, 5, 22, 9, 10, 0)


def test_anchors_do_not_fit_when_span_too_long():
    presence = [
        (datetime(2020, 5, 22, 8, 0, 0), datetime(2020, 5, 22, 12, 0, 0), True),
    ]
    run = [
        WorkAnchor('commit', 'outdoor', datetime(2020, 5, 22, 8, 0, 0),
                   datetime(2020, 5, 22, 8, 0, 0), 'first'),
        WorkAnchor('commit', 'outdoor', datetime(2020, 5, 22, 10, 30, 0),
                   datetime(2020, 5, 22, 10, 30, 0), 'second'),
    ]
    assert not _anchors_fit_together(run, presence)


def test_trim_keeps_commit_anchored_second_block():
    entries = [
        {
            'start_time': datetime(2020, 5, 22, 8, 0, 0),
            'end_time': datetime(2020, 5, 22, 9, 0, 0),
            'title': 'A',
            'project': 'g1:p',
        },
        {
            'start_time': datetime(2020, 5, 22, 9, 30, 0),
            'end_time': datetime(2020, 5, 22, 10, 30, 0),
            'title': 'B',
            'project': 'g1:p',
            'anchor_time': datetime(2020, 5, 22, 10, 0, 0),
        },
    ]
    out = _trim_overlaps_without_shifting(entries, [])
    assert len(out) == 2
    assert out[1]['start_time'] >= datetime(2020, 5, 22, 9, 0, 0)


def test_segments_within_evidence_splits_inactive_gap():
    mon = datetime(2020, 5, 20, 10, 0, 0)
    fri = datetime(2020, 5, 24, 10, 0, 0)
    segments = _segments_within_evidence(
        mon - timedelta(hours=1),
        fri + timedelta(hours=1),
        [mon, fri],
        'outdoor',
        [],
    )
    assert len(segments) == 2
    assert segments[0][1] <= mon + timedelta(minutes=30)
    assert segments[1][0] >= fri - timedelta(minutes=30)


def test_session_chunks_splits_over_ten_hours():
    start = datetime(2020, 5, 22, 8, 0, 0)
    end = start + timedelta(hours=25)
    meta = {'title': 'Work', 'project': 'g:p', 'anchor_time': start + timedelta(hours=12)}
    chunks = _session_chunks(start, end, meta)
    assert len(chunks) == 3
    for chunk in chunks:
        span = chunk['end_time'] - chunk['start_time']
        assert MIN_LOCAL_ENTRY_DURATION <= span <= MAX_LOCAL_ENTRY_DURATION


def test_aw_entry_clearly_non_work_when_leisure_dominates():
    row = Entry(
        module='activitywatch',
        start_time=datetime(2020, 5, 22, 10, 0, 0),
        end_time=datetime(2020, 5, 22, 10, 45, 0),
        group='outdoor',
        text=(
            'outdoor\n'
            '300s - Google Chrome - Silo in Minecraft! - YouTube - Google Chrome\n'
            '5s - Cursor - api.py — code'
        ),
    )
    assert _aw_entry_is_clearly_non_work(row, 'outdoor')


def test_aw_entry_not_non_work_when_mostly_project_windows(monkeypatch):
    monkeypatch.setattr(
        'tracklater.work_inference._work_keywords_for_group',
        lambda group: (
            ['ScandinavianOutdoor', 'devcontainer'] if group == 'outdoor' else []
        ),
    )
    row = Entry(
        module='activitywatch',
        start_time=datetime(2020, 5, 22, 10, 0, 0),
        end_time=datetime(2020, 5, 22, 10, 45, 0),
        group='outdoor',
        text=(
            'outdoor\n'
            '200s - Cursor - api.py — code [Container Sos project devcontainer]\n'
            '30s - Google Chrome - Pull Request - ScandinavianOutdoor/store'
        ),
    )
    assert not _aw_entry_is_clearly_non_work(row, 'outdoor')


def test_post_commit_stops_at_clearly_non_work_activity(monkeypatch):
    monkeypatch.setattr(
        'tracklater.work_inference._work_keywords_for_group',
        lambda group: (
            ['devcontainer', 'ScandinavianOutdoor'] if group == 'outdoor' else []
        ),
    )
    commit_time = datetime(2020, 5, 22, 10, 0, 0)
    commit = WorkAnchor(
        'commit', 'outdoor', commit_time, commit_time, 'Fix bug',
    )
    aw_rows = [
        Entry(
            module='activitywatch',
            id='aw-yt',
            start_time=commit_time + timedelta(minutes=2),
            end_time=commit_time + timedelta(minutes=40),
            group='outdoor',
            text=(
                'outdoor\n'
                '400s - Google Chrome - Relaxing stream - YouTube\n'
                '3s - Cursor - api.py — devcontainer'
            ),
        ),
    ]
    end = _post_commit_end(
        commit, [], aw_rows, commit_time + timedelta(hours=2),
    )
    assert end <= commit_time + MIN_WORK_AFTER_COMMIT + timedelta(minutes=2)


def test_trim_does_not_append_into_gap():
    presence = []
    entries = [
        {
            'start_time': datetime(2020, 5, 22, 8, 0, 0),
            'end_time': datetime(2020, 5, 22, 10, 0, 0),
            'title': 'A',
            'project': 'g1:p',
        },
        {
            'start_time': datetime(2020, 5, 22, 9, 0, 0),
            'end_time': datetime(2020, 5, 22, 11, 0, 0),
            'title': 'B',
            'project': 'g2:p',
        },
    ]
    out = _trim_overlaps_without_shifting(entries, presence)
    assert len(out) <= 2
    if len(out) == 2:
        assert out[1]['start_time'] >= out[0]['end_time'] or out[1]['start_time'] == datetime(
            2020, 5, 22, 9, 0, 0
        )
