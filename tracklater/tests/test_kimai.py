from datetime import datetime

import pytest

from tracklater import settings, test_settings
from tracklater.models import Entry
from tracklater.timemodules import kimai
from tracklater.timemodules.kimai import Parser, to_kimai_time


CUSTOMERS = [
    {'id': 1, 'name': 'First Client'},
    {'id': 2, 'name': 'Second Client'},
]
PROJECTS = [
    {'id': 10, 'name': 'Development', 'customer': 1},
    {'id': 11, 'name': 'Bug fixing', 'customer': 1},
    {'id': 20, 'name': 'Development', 'customer': 2},
    {'id': 99, 'name': 'Not in settings', 'customer': 1},
]
TIMESHEETS = [
    # Kimai serializes with the instance's UTC offset; +03:00 is Helsinki DST.
    {'id': 501, 'begin': '2026-08-27T09:00:00+0300', 'end': '2026-08-27T10:30:00+0300',
     'project': 10, 'activity': 7, 'description': 'Worked on the parser'},
    {'id': 502, 'begin': '2026-08-27T11:00:00+0300', 'end': '2026-08-27T12:00:00+0300',
     'project': 99, 'activity': 7, 'description': 'Unmapped project'},
    # Still running: no end, so it never reaches the timeline.
    {'id': 503, 'begin': '2026-08-27T13:00:00+0300', 'end': None,
     'project': 10, 'activity': 7, 'description': 'Running'},
]


class FakeProvider:
    """Stands in for the HTTP layer, recording writes so they can be asserted on."""

    def __init__(self, *args, **kwargs):
        self.calls = []

    def paged(self, endpoint, params=None):
        return {
            'customers': CUSTOMERS,
            'projects': PROJECTS,
            'timesheets': TIMESHEETS,
            'activities': [{'id': 42, 'name': 'Fallback activity'}],
        }[endpoint]

    def request(self, endpoint, **kwargs):
        import json
        self.calls.append((kwargs.get('method'), endpoint, json.loads(kwargs['data'])
                           if kwargs.get('data') else None))
        body = self.calls[-1][2] or {}
        return {
            'id': 777,
            'begin': '2026-08-27T09:00:00+0300',
            'end': '2026-08-27T10:30:00+0300',
            'project': body.get('project'),
            'description': body.get('description'),
        }


@pytest.fixture(autouse=True)
def kimai_settings(monkeypatch):
    # conftest's mock_settings only mirrors attributes that already exist on
    # settings, and a live config need not have a KIMAI block, so set it here.
    monkeypatch.setattr(settings, 'KIMAI', test_settings.KIMAI, raising=False)
    monkeypatch.setattr(settings, 'TIMEZONE', 'Europe/Helsinki', raising=False)


@pytest.fixture()
def parser(monkeypatch):
    monkeypatch.setattr(kimai, 'Provider', FakeProvider)
    return Parser(datetime(2026, 8, 27, 0, 0), datetime(2026, 8, 27, 23, 59))


def test_to_kimai_time_converts_utc_to_local_wall_clock():
    # 06:00 UTC is 09:00 in Helsinki summer time, and Kimai wants no offset.
    assert to_kimai_time(datetime(2026, 8, 27, 6, 0, 0)) == '2026-08-27T09:00:00'


def test_get_entries_maps_projects_and_skips_running(parser):
    entries = parser.get_entries()
    assert [e.id for e in entries] == ['501', '502']  # 503 is still running
    assert entries[0].start_time == datetime(2026, 8, 27, 6, 0)  # naive UTC
    assert entries[0].end_time == datetime(2026, 8, 27, 7, 30)
    assert entries[0].title == 'Worked on the parser'
    assert entries[0].project == 'group1:Development'
    # A project missing from settings.KIMAI stays unmapped rather than guessing.
    assert entries[1].project is None


def test_get_projects_are_synthetic_and_need_no_api_call(parser):
    projects = parser.get_projects()
    assert {p.pid for p in projects} == {
        'group1:Development', 'group1:Bug fixing', 'group2:Development'
    }
    assert {p.group for p in projects} == {'group1', 'group2'}


def test_create_entry_posts_local_times_and_numeric_ids(parser):
    created = parser.create_entry(Entry(
        start_time=datetime(2026, 8, 27, 6, 0),
        end_time=datetime(2026, 8, 27, 7, 30),
        title='New entry',
        project='group1:Development',
    ), None)
    method, endpoint, body = parser.provider.calls[-1]
    assert (method, endpoint) == ('POST', 'timesheets')
    assert body == {
        'begin': '2026-08-27T09:00:00',
        'end': '2026-08-27T10:30:00',
        'project': 10,
        'activity': 7,  # from settings.KIMAI['group1']['ACTIVITY']
        'description': 'New entry',
    }
    assert created.id == '777'
    assert created.project == 'group1:Development'


def test_activity_falls_back_to_first_project_activity(parser):
    # group2 configures no ACTIVITY, so the project's first visible one is used.
    parser.create_entry(Entry(
        start_time=datetime(2026, 8, 27, 6, 0),
        end_time=datetime(2026, 8, 27, 7, 30),
        title='No configured activity',
        project='group2:Development',
    ), None)
    assert parser.provider.calls[-1][2]['activity'] == 42


def test_group_only_entry_resolves_to_default_project(parser):
    parser.create_entry(Entry(
        start_time=datetime(2026, 8, 27, 6, 0),
        end_time=datetime(2026, 8, 27, 7, 30),
        title='Group only',
    ), type('Issue', (), {'group': 'group1'})())
    assert parser.provider.calls[-1][2]['project'] == 10


def test_update_and_delete_hit_the_right_endpoints(parser):
    parser.update_entry('501', Entry(
        start_time=datetime(2026, 8, 27, 6, 0),
        end_time=datetime(2026, 8, 27, 7, 30),
        title='Edited',
        project='group1:Development',
    ), None)
    assert parser.provider.calls[-1][:2] == ('PATCH', 'timesheets/501')
    parser.delete_entry('501')
    assert parser.provider.calls[-1][:2] == ('DELETE', 'timesheets/501')


def test_unmapped_project_is_an_error_not_a_silent_write(parser):
    with pytest.raises(ValueError):
        parser.create_entry(Entry(
            start_time=datetime(2026, 8, 27, 6, 0),
            end_time=datetime(2026, 8, 27, 7, 30),
            title='Nowhere',
            project='nosuch:Project',
        ), None)


def test_missing_end_time_is_rejected(parser):
    with pytest.raises(ValueError):
        parser.create_entry(Entry(
            start_time=datetime(2026, 8, 27, 6, 0),
            title='Unfinished',
            project='group1:Development',
        ), None)


def test_entries_carry_group_for_timeline_colouring(parser):
    """The timeline colours by group; an entry without one renders in the
    module's global colour, so every write and read must set it."""
    created = parser.create_entry(
        Entry(
            start_time=datetime(2026, 8, 24, 6, 0),
            end_time=datetime(2026, 8, 24, 7, 0),
            title='colour me',
            project='group1:Development',
        ),
        None,
    )
    assert created.project == 'group1:Development'
    assert created.group == 'group1'

    updated = parser.update_entry('123', Entry(
        start_time=datetime(2026, 8, 24, 6, 0),
        end_time=datetime(2026, 8, 24, 7, 0),
        title='colour me too',
        project='group2:Development',
    ), None)
    assert updated.group == 'group2'
