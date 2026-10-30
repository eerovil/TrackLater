from datetime import datetime, timedelta

import pytest

from tracklater import create_app
from tracklater.database import db
from tracklater.models import Entry
from tracklater.work_inference import build_entries_from_commits

DIRECTORY = __import__('os').path.dirname(__import__('os').path.realpath(__file__))
app = create_app()


@pytest.fixture
def client(db):
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///{}/database_commit_driven.db'.format(
        DIRECTORY
    )
    with app.app_context():
        db.create_all()
    yield app.test_client()
    path = '{}/database_commit_driven.db'.format(DIRECTORY)
    if __import__('os').path.exists(path):
        __import__('os').remove(path)


def test_build_entries_splits_metso(client, monkeypatch):
    from tracklater import settings as app_settings
    monkeypatch.setattr(app_settings, 'KIMAI', {
        'outdoor': {'NAME': 'O', 'PROJECTS': {'Verkkokauppakehitys': 'default'}},
        'metso': {'NAME': 'M', 'PROJECTS': {'Metso-ostojärjestelmäkehitys': 'default'}},
    })
    allowed = {
        'outdoor:Verkkokauppakehitys',
        'metso:Metso-ostojärjestelmäkehitys',
    }
    start = datetime(2020, 5, 22, 6, 0, 0)
    end = datetime(2020, 5, 22, 20, 0, 0)
    with app.app_context():
        db.session.query(Entry).delete()
        t = datetime(2020, 5, 22, 10, 0, 0)
        for i, (group, mins) in enumerate([('outdoor', 0), ('metso', 90), ('outdoor', 180)]):
            db.session.add(Entry(
                module='gitmodule',
                id='git-{}'.format(i),
                start_time=t + timedelta(minutes=mins),
                group=group,
                text='{} [main] - commit {}'.format(group, i),
            ))
        db.session.add(Entry(
            module='activitywatch',
            id='aw-metso',
            start_time=t + timedelta(minutes=75),
            end_time=t + timedelta(minutes=140),
            group='metso',
            title='metso',
        ))
        db.session.commit()
        entries = build_entries_from_commits(start, end, allowed)
    assert len(entries) >= 3
    projects = {e['project'] for e in entries}
    assert 'metso:Metso-ostojärjestelmäkehitys' in projects
    metso = [e for e in entries if e['project'] == 'metso:Metso-ostojärjestelmäkehitys'][0]
    assert (metso['end_time'] - metso['start_time']) >= timedelta(minutes=30)


def test_sparse_commits_do_not_fill_inactive_gap(client, monkeypatch):
    """Same project commits days apart must not produce 10h blocks across the gap."""
    from tracklater import settings as app_settings
    monkeypatch.setattr(app_settings, 'KIMAI', {
        'outdoor': {'NAME': 'O', 'PROJECTS': {'Verkkokauppakehitys': 'default'}},
    })
    allowed = {'outdoor:Verkkokauppakehitys'}
    start = datetime(2020, 5, 20, 0, 0, 0)
    end = datetime(2020, 5, 26, 23, 59, 59)
    with app.app_context():
        db.session.query(Entry).delete()
        db.session.add(Entry(
            module='gitmodule',
            id='git-mon',
            start_time=datetime(2020, 5, 20, 10, 0, 0),
            group='outdoor',
            text='outdoor [main] - monday',
        ))
        db.session.add(Entry(
            module='gitmodule',
            id='git-fri',
            start_time=datetime(2020, 5, 24, 10, 0, 0),
            group='outdoor',
            text='outdoor [main] - friday',
        ))
        db.session.commit()
        entries = build_entries_from_commits(start, end, allowed)
    gap_mid = datetime(2020, 5, 22, 12, 0, 0)
    for entry in entries:
        in_gap = entry['start_time'] <= gap_mid <= entry['end_time']
        assert not in_gap


def test_build_entries_one_per_commit_same_group(client, monkeypatch):
    from tracklater import settings as app_settings
    monkeypatch.setattr(app_settings, 'KIMAI', {
        'outdoor': {'NAME': 'O', 'PROJECTS': {'Verkkokauppakehitys': 'default'}},
    })
    allowed = {'outdoor:Verkkokauppakehitys'}
    start = datetime(2020, 5, 22, 6, 0, 0)
    end = datetime(2020, 5, 22, 20, 0, 0)
    with app.app_context():
        db.session.query(Entry).delete()
        t = datetime(2020, 5, 22, 9, 0, 0)
        for i in range(4):
            db.session.add(Entry(
                module='gitmodule',
                id='git-many-{}'.format(i),
                start_time=t + timedelta(hours=i * 2),
                group='outdoor',
                text='outdoor [main] - commit {}'.format(i),
            ))
        db.session.commit()
        entries = build_entries_from_commits(start, end, allowed)
    assert len(entries) >= 3


def test_aw_skipped_when_spanning_other_project_commits(client, monkeypatch):
    """Outdoor AW must not replace a metso commit entry inside the same window."""
    from tracklater import settings as app_settings
    from tracklater.tests.integration.timeline_fixture import (
        day_range,
        seed_timeline,
        _DATA,
        _parse_db_time,
    )
    monkeypatch.setattr(app_settings, 'KIMAI', {
        'outdoor': {'NAME': 'O', 'PROJECTS': {'Verkkokauppakehitys': 'default'}},
        'metso': {'NAME': 'M', 'PROJECTS': {'Metso-ostojärjestelmäkehitys': 'default'}},
    })
    allowed = {
        'outdoor:Verkkokauppakehitys',
        'metso:Metso-ostojärjestelmäkehitys',
    }
    start, end = day_range()
    with app.app_context():
        seed_timeline()
        entries = build_entries_from_commits(start, end, allowed)
    metso = [e for e in entries if e['project'] == 'metso:Metso-ostojärjestelmäkehitys']
    assert metso, 'metso commit must produce metso entry'
    metso_commit = _parse_db_time(_DATA['git_commits'][1]['start_time'])
    assert metso[0]['start_time'] <= metso_commit <= metso[0]['end_time']


def test_session_blocks_outdoor_then_metso(client, monkeypatch):
    """Outdoor ~04:15–06:10 UTC, metso ~06:10–07:50 (matches 07:15–09:10 / 09:10–10:50 local)."""
    from tracklater import settings as app_settings
    from tracklater.tests.integration.timeline_fixture import (
        day_range,
        seed_timeline,
        _DATA,
        _parse_db_time,
    )
    from tracklater.work_inference import (
        PROJECT_HANDOFF_AFTER_COMMIT,
        build_entries_from_commits,
    )

    monkeypatch.setattr(app_settings, 'KIMAI', {
        'outdoor': {'NAME': 'O', 'PROJECTS': {'Verkkokauppakehitys': 'default'}},
        'metso': {'NAME': 'M', 'PROJECTS': {'Metso-ostojärjestelmäkehitys': 'default'}},
    })
    allowed = {
        'outdoor:Verkkokauppakehitys',
        'metso:Metso-ostojärjestelmäkehitys',
    }
    start, end = day_range()
    with app.app_context():
        seed_timeline()
        entries = build_entries_from_commits(start, end, allowed)

    assert len(entries) == 2
    outdoor = [e for e in entries if e['project'] == 'outdoor:Verkkokauppakehitys'][0]
    metso = [e for e in entries if e['project'] == 'metso:Metso-ostojärjestelmäkehitys'][0]

    c1 = _parse_db_time(_DATA['git_commits'][0]['start_time'])
    c2 = _parse_db_time(_DATA['git_commits'][1]['start_time'])
    pre_metso = c2 - timedelta(minutes=30)

    assert outdoor['start_time'] <= c1
    assert abs((outdoor['end_time'] - pre_metso).total_seconds()) < 60
    assert metso['start_time'] >= outdoor['end_time'] - timedelta(seconds=2)
    assert metso['start_time'] <= pre_metso + timedelta(seconds=2)
    assert metso['end_time'] >= c2
    assert outdoor['end_time'] <= pre_metso + timedelta(seconds=2)
    from tracklater.work_inference import MAX_WORK_AFTER_COMMIT, MIN_WORK_AFTER_COMMIT

    assert metso['end_time'] >= c2 + MIN_WORK_AFTER_COMMIT
    assert metso['end_time'] <= c2 + MAX_WORK_AFTER_COMMIT
