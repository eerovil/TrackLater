"""
Populate local from the real 2026-06-01 timeline fixture (git + ActivityWatch).

    pytest tracklater/tests/integration/test_populate_local_timeline.py -v
"""
from datetime import timedelta

import pytest

from tracklater.ai_local import populate_local_entries
from tracklater.work_inference import PROJECT_HANDOFF_AFTER_COMMIT
from tracklater.tests.integration.timeline_fixture import (
    day_range,
    project_id,
    seed_two_commit_morning,
)

MIN_ENTRY = timedelta(minutes=30)
MAX_CONTIGUITY_GAP = timedelta(hours=2)


def _entries_covering(entries, moment):
    return [
        e for e in entries
        if e.start_time <= moment <= e.end_time
    ]


def assert_commits_covered(created, commit_specs):
    for commit_time, group, title_hint in commit_specs:
        expected_project = project_id(group)
        dedicated = [
            e for e in created
            if e.project == expected_project
            and e.start_time <= commit_time <= e.end_time
        ]
        assert dedicated, (
            'Commit at {} needs a {} entry; got {}'.format(
                commit_time, expected_project,
                [(e.project, e.start_time, e.end_time) for e in created],
            )
        )


def assert_minimum_duration(entries):
    for entry in entries:
        duration = entry.end_time - entry.start_time
        assert duration >= MIN_ENTRY, (
            'Entry {}–{} is only {}'.format(
                entry.start_time, entry.end_time, duration
            )
        )


def assert_contiguous_same_project(entries, project):
    project_entries = sorted(
        [e for e in entries if e.project == project],
        key=lambda e: e.start_time,
    )
    assert len(project_entries) >= 2
    for i in range(1, len(project_entries)):
        gap = project_entries[i].start_time - project_entries[i - 1].end_time
        assert gap <= MAX_CONTIGUITY_GAP, (
            'Expected contiguous {} blocks, gap {}'.format(project, gap)
        )


def assert_outdoor_metso_session_times(created, commit_specs):
    """Outdoor until ~09:10, metso from commit until ~10:50 (local UTC+3)."""
    outdoor = sorted(
        [e for e in created if e.project == project_id('outdoor')],
        key=lambda e: e.start_time,
    )
    metso = sorted(
        [e for e in created if e.project == project_id('metso')],
        key=lambda e: e.start_time,
    )
    assert outdoor and metso, 'Expected one outdoor and one metso block'
    o, m = outdoor[0], metso[0]
    c1, c2 = commit_specs[0][0], commit_specs[1][0]
    pre_metso = c2 - timedelta(minutes=30)
    assert o.start_time <= c1
    assert abs((o.end_time - pre_metso).total_seconds()) <= timedelta(minutes=5).total_seconds()
    assert m.start_time <= c2
    assert m.start_time >= o.end_time - timedelta(minutes=1)
    assert m.start_time <= pre_metso + timedelta(minutes=1)
    assert o.end_time <= pre_metso + timedelta(minutes=1)
    from tracklater.work_inference import MAX_WORK_AFTER_COMMIT, MIN_WORK_AFTER_COMMIT

    assert m.end_time >= c2 + MIN_WORK_AFTER_COMMIT
    assert m.end_time <= c2 + MAX_WORK_AFTER_COMMIT


def assert_two_block_morning_shape(entries, commit_specs):
    assert len(entries) >= 2
    assert len(entries) <= 6
    ordered = sorted(entries, key=lambda e: e.start_time)
    first_commit, second_commit = commit_specs[0][0], commit_specs[1][0]
    block1 = _entries_covering(ordered, first_commit)[0]
    block2 = _entries_covering(ordered, second_commit)[0]
    gap = block2.start_time - block1.end_time
    assert gap <= MAX_CONTIGUITY_GAP, (
        'Second block should start where first ends (got gap {})'.format(gap)
    )


def test_populate_two_commits_same_project(integration_db):
    start, end = day_range()
    commit_specs = seed_two_commit_morning(second_commit_group='outdoor')
    created = populate_local_entries(start, end, replace_existing=True)
    assert len(created) >= 2
    assert_commits_covered(created, commit_specs)
    assert_minimum_duration(created)
    assert_contiguous_same_project(created, project_id('outdoor'))
    assert_two_block_morning_shape(created, commit_specs)

    assert any(
        'remove' in (e.title or '').lower() or 'log' in (e.title or '').lower()
        for e in created
    )
    assert any(
        'extraclean' in (e.title or '').lower() or 'update' in (e.title or '').lower()
        for e in created
    )


def test_populate_second_commit_different_project(integration_db):
    start, end = day_range()
    commit_specs = seed_two_commit_morning(second_commit_group='metso')
    created = populate_local_entries(start, end, replace_existing=True)
    assert_commits_covered(created, commit_specs)
    assert_minimum_duration(created)

    outdoor_entries = [e for e in created if e.project == project_id('outdoor')]
    metso_entries = [e for e in created if e.project == project_id('metso')]
    assert outdoor_entries, 'Expected an outdoor entry for the first commit'
    assert metso_entries, 'Expected a metso entry for the second commit'
    assert _entries_covering(metso_entries, commit_specs[1][0])
    assert_outdoor_metso_session_times(created, commit_specs)
