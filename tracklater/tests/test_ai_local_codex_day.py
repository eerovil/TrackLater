import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from tracklater import ai_local_codex as codex


def test_run_codex_uses_sol_structured_ephemeral_exec(monkeypatch):
    captured = {}

    def fake_run(cmd, input, capture_output, text, timeout, cwd):
        captured.update(cmd=cmd, input=input, cwd=cwd, timeout=timeout)
        output_path = cmd[cmd.index('--output-last-message') + 1]
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('{"entries":[]}')
        schema_path = cmd[cmd.index('--output-schema') + 1]
        with open(schema_path, 'r', encoding='utf-8') as f:
            captured['schema'] = json.load(f)
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    monkeypatch.setattr(codex, '_binary', lambda: 'codex')
    monkeypatch.setattr(codex, '_model', lambda: 'gpt-5.6-sol')
    monkeypatch.setattr(codex, '_effort', lambda: 'low')
    monkeypatch.setattr(codex.subprocess, 'run', fake_run)

    assert codex.run_codex('billing prompt', timeout=12) == '{"entries":[]}'
    assert captured['cmd'][:2] == ['codex', 'exec']
    assert captured['cmd'][captured['cmd'].index('--model') + 1] == 'gpt-5.6-sol'
    assert 'model_reasoning_effort="low"' in captured['cmd']
    assert '--ephemeral' in captured['cmd']
    assert '--ignore-user-config' in captured['cmd']
    assert captured['cmd'][captured['cmd'].index('--sandbox') + 1] == 'read-only'
    assert captured['input'] == 'billing prompt'
    assert captured['schema']['type'] == 'object'


@pytest.mark.parametrize(
    'day, expected_hours',
    [('2026-03-29', 23), ('2026-10-25', 25)],
)
def test_single_day_validation_accepts_dst_days(monkeypatch, day, expected_hours):
    from tracklater import settings

    monkeypatch.setattr(settings, 'TIMEZONE', 'Europe/Helsinki', raising=False)
    start, end = codex._local_day_utc_bounds(day)

    assert (end - start) == timedelta(hours=expected_hours)
    assert codex.validate_single_local_day_range(start, end) == day


def test_single_day_validation_rejects_partial_day(monkeypatch):
    from tracklater import settings

    monkeypatch.setattr(settings, 'TIMEZONE', 'Europe/Helsinki', raising=False)
    start, end = codex._local_day_utc_bounds('2026-07-16')

    with pytest.raises(ValueError, match='midnight-to-midnight'):
        codex.validate_single_local_day_range(
            start + timedelta(minutes=30), end,
        )


def _configure_stream(monkeypatch, result):
    from tracklater import settings

    target = '2026-07-16'
    monkeypatch.setattr(settings, 'ENABLED_MODULES', ['gitmodule', 'kimai'])
    monkeypatch.setattr(settings, 'TIMEZONE', 'Europe/Helsinki', raising=False)
    monkeypatch.setattr(codex, '_has_source_data', lambda *_: True)
    monkeypatch.setattr(
        codex, '_allowed_local_projects', lambda *_: {'group1:Development'},
    )
    monkeypatch.setattr(codex, 'gather_signal', lambda *_: {
        '2026-07-15': 'adjacent signal',
        target: 'selected signal',
        '2026-07-17': 'adjacent signal',
    })
    monkeypatch.setattr(codex, 'run_codex', lambda *_: result)
    monkeypatch.setattr(codex, '_write_suggestions', lambda *_args, **_kwargs: None)
    return target


def test_single_day_stream_pins_prompt_and_persistence(monkeypatch):
    target = '2026-07-16'
    result = json.dumps([{
        'date': target,
        'start': '09:00',
        'end': '10:00',
        'project': 'group1:Development',
        'title': 'Selected work',
    }])
    target = _configure_stream(monkeypatch, result)
    persisted = []
    monkeypatch.setattr(
        codex, 'persist_local_entries',
        lambda entries, start, end, replace_existing: persisted.append(
            (entries, start, end, replace_existing)
        ),
    )
    start, end = codex._local_day_utc_bounds(target)

    events = list(codex.stream_populate_local_entries_ai(
        start, end, replace_existing=True, only_day=target,
    ))

    assert [event['type'] for event in events] == ['progress', 'done']
    assert events[0]['day'] == target
    assert len(persisted) == 1
    assert persisted[0][1:3] == (start, end)
    assert persisted[0][3] is True


def test_empty_single_day_result_preserves_existing_entries(monkeypatch):
    target = _configure_stream(monkeypatch, '[]')
    persisted = []
    monkeypatch.setattr(
        codex, 'persist_local_entries', lambda *_args, **_kwargs: persisted.append(1),
    )
    start, end = codex._local_day_utc_bounds(target)

    events = list(codex.stream_populate_local_entries_ai(
        start, end, replace_existing=True, only_day=target,
    ))

    assert persisted == []
    assert events[0]['type'] == 'day_error'
    assert 'preserved' in events[0]['error']
    assert events[-1]['type'] == 'done'


def test_week_stream_skips_days_that_already_have_entries(monkeypatch):
    result = json.dumps([{
        'date': '2026-07-15',
        'start': '09:00',
        'end': '10:00',
        'project': 'group1:Development',
        'title': 'Work',
    }])
    _configure_stream(monkeypatch, result)
    monkeypatch.setattr(
        codex, '_local_day_has_billing_entries', lambda day: day != '2026-07-15',
    )
    prompted = []
    monkeypatch.setattr(
        codex, 'build_day_prompt', lambda day, *_: prompted.append(day) or day,
    )
    monkeypatch.setattr(codex, 'persist_local_entries', lambda *_a, **_k: None)
    start, _ = codex._local_day_utc_bounds('2026-07-15')
    _, end = codex._local_day_utc_bounds('2026-07-17')

    events = list(codex.stream_populate_local_entries_ai(
        start, end, skip_filled_days=True,
    ))

    assert prompted == ['2026-07-15']
    assert events[0]['total'] == 1
    assert events[-1]['skipped'] == ['2026-07-16', '2026-07-17']


@pytest.fixture()
def app_db_with_signal(monkeypatch, db):
    """A day whose per-group activity is holey but whose work is continuous."""
    import os
    import tempfile
    from flask import Flask
    from tracklater import settings
    from tracklater.models import Entry

    monkeypatch.setattr(settings, 'TIMEZONE', 'Europe/Helsinki', raising=False)
    app = Flask(__name__)
    path = os.path.join(tempfile.gettempdir(), 'tracklater_codex_signal.db')
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///{}'.format(path)
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)

    def utc(h, m, day=12):
        # Helsinki is UTC+3 in August; the digest buckets by local time.
        return datetime(2026, 8, day, h - 3, m)

    with app.app_context():
        db.create_all()
        db.session.query(Entry).delete()
        db.session.add_all([
            Entry(module='activitywatch', id='aw-1', group='outdoor',
                  start_time=utc(8, 0), end_time=utc(9, 0)),
            Entry(module='activitywatch', id='aw-2', group='outdoor',
                  start_time=utc(11, 0), end_time=utc(12, 0)),
            Entry(module='activitywatch', id='aw-3', group='storm',
                  start_time=utc(9, 20), end_time=utc(10, 0)),
            Entry(module='gitmodule', id='c-1', group='outdoor',
                  start_time=utc(10, 30), text='outdoor [master] - orders: fix'),
            Entry(module='gitmodule', id='c-2', group='outdoor',
                  start_time=utc(20, 0, day=13), text='outdoor [master] - late one'),
        ])
        db.session.commit()
        yield codex.gather_signal(
            datetime(2026, 8, 12), datetime(2026, 8, 13, 23, 59),
        )
        db.session.remove()
        db.drop_all()
    if os.path.exists(path):
        os.remove(path)


def test_work_sessions_bridge_across_groups_and_commits(app_db_with_signal):
    """A hole in one group's spans that another group's work fills is a client
    switch, not a break -- reading it as a break is what under-bills a day."""
    digests = app_db_with_signal
    line = next(
        line for line in digests['2026-08-12'].splitlines()
        if 'WORK SESSIONS' in line
    )
    # outdoor alone reads as 08:00-09:00 + 11:00-12:00 with an hour missing; the
    # gap holds storm activity and a commit, so the day is one 08:00-12:00 span.
    assert '08:00-12:00' in line
    assert '09:00-11:00' not in line

    per_group = [
        line for line in digests['2026-08-12'].splitlines()
        if line.strip().startswith('outdoor:')
    ]
    assert per_group and '08:00-09:00' in per_group[0]  # per-group spans unchanged


def test_isolated_commit_is_not_a_zero_length_session(app_db_with_signal):
    line = next(
        line for line in app_db_with_signal['2026-08-13'].splitlines()
        if 'WORK SESSIONS' in line
    )
    assert '19:55-20:05' in line  # nominal width, not 20:00-20:00
