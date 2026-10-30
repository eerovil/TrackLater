from datetime import datetime, timedelta

import pytest
import requests_mock

from tracklater.timemodules.activitywatch import (
    NOT_AFK_LABEL,
    Parser,
    Provider,
    classify_bucket,
    merge_overlapping_entries,
)


@pytest.fixture()
def parser():
    return Parser(datetime.utcnow() - timedelta(days=7), datetime.utcnow())


def test_get_entries(parser):
    data = parser.get_entries()
    assert len(data) == 1


def test_classify_bucket():
    assert classify_bucket('aw-watcher-window_hostname', {'type': 'currentwindow'}) == 'window'
    assert classify_bucket('aw-watcher-afk_hostname', {'type': 'afkstatus'}) == 'afk'
    assert classify_bucket('aw-watcher-web_hostname', {'type': 'web.tab.current'}) is None


def test_discover_and_merge_buckets(requests_mock, monkeypatch):
    monkeypatch.setattr('tracklater.settings.TESTING', False)
    base = 'http://127.0.0.1:5600/api/0'
    requests_mock.get(
        '{}/buckets/'.format(base),
        json={
            'aw-watcher-window_host': {'type': 'currentwindow', 'client': 'aw-watcher-window'},
            'aw-watcher-afk_host': {'type': 'afkstatus', 'client': 'aw-watcher-afk'},
        },
    )
    requests_mock.get(
        '{}/buckets/aw-watcher-window_host/events'.format(base),
        json=[
            {
                'timestamp': '2022-09-04T10:00:00+00:00',
                'duration': 60,
                'data': {'app': 'Code', 'title': 'main.py'},
            },
        ],
    )
    requests_mock.get(
        '{}/buckets/aw-watcher-afk_host/events'.format(base),
        json=[
            {
                'timestamp': '2022-09-04T09:00:00+00:00',
                'duration': 300,
                'data': {'status': 'afk'},
            },
            {
                'timestamp': '2022-09-04T11:00:00+00:00',
                'duration': 120,
                'data': {'status': 'not-afk'},
            },
        ],
    )

    start = datetime(2022, 9, 4)
    end = datetime(2022, 9, 5)
    provider = Provider()
    events = provider.fetch_events(start, end)
    assert len(events) == 3

    p = Parser(start, end)
    parsed = p._parse_events(events)
    labels = {e['active_window'] for e in parsed}
    assert 'Code - main.py' in labels
    assert NOT_AFK_LABEL in labels
    assert 'afk' not in labels


def test_merge_overlapping_duplicate_buckets():
    t0 = datetime(2022, 9, 4, 10, 0, 0)
    entries = [
        {
            'active_window': 'Code',
            'time': t0,
            'end_time': t0 + timedelta(seconds=60),
            'category': 'work',
        },
        {
            'active_window': 'Code - main.py',
            'time': t0 + timedelta(seconds=5),
            'end_time': t0 + timedelta(seconds=55),
            'category': 'work',
        },
    ]
    merged = merge_overlapping_entries(entries)
    assert len(merged) == 1
    assert merged[0]['active_window'] == 'Code - main.py'
    assert merged[0]['end_time'] == t0 + timedelta(seconds=60)


def test_session_has_real_duration():
    p = Parser(datetime(2022, 9, 4), datetime(2022, 9, 5))
    t0 = datetime(2022, 9, 4, 10, 0, 0)
    entries = [
        {
            'active_window': 'Code - main.py',
            'time': t0,
            'end_time': t0 + timedelta(minutes=20),
            'category': 'work',
        },
    ]
    sessions = p._generate_sessions(entries)
    assert len(sessions) == 1
    assert sessions[0].duration == 20 * 60
    assert sessions[0].start_time == t0
    assert sessions[0].end_time == t0 + timedelta(minutes=20)


def test_afk_interval_excludes_window_overlap():
    p = Parser(datetime(2022, 9, 4), datetime(2022, 9, 5))
    window = {
        'timestamp': '2022-09-04T10:00:00+00:00',
        'duration': 60,
        'data': {'app': 'Code'},
        '_aw_bucket_kind': 'window',
    }
    afk = {
        'timestamp': '2022-09-04T10:00:00+00:00',
        'duration': 60,
        'data': {'status': 'afk'},
        '_aw_bucket_kind': 'afk',
    }
    parsed = p._parse_events([window, afk])
    assert parsed == []
