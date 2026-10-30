import json
from datetime import datetime

import pytest

from tracklater import views
from tracklater.timemodules.interfaces import AddEntryMixin, UpdateEntryMixin
from tracklater.tests.test_app import client  # noqa: F401 (fixture)


class FakeModule(AddEntryMixin, UpdateEntryMixin):
    def __init__(self, error=None):
        self.error = error
        self.seen = []

    def get_entries(self):
        return []

    def create_entry(self, new_entry, issue):
        self.seen.append(new_entry)
        if self.error:
            raise ValueError(self.error)
        new_entry.id = '1'
        return new_entry

    def update_entry(self, entry_id, new_entry, issue):
        return self.create_entry(new_entry, issue)


@pytest.fixture
def fake(monkeypatch, client):
    module = FakeModule()

    class FakeParser:
        def __init__(self, *args, **kwargs):
            self.modules = {'kimai': module}

    monkeypatch.setattr(views, 'Parser', FakeParser)
    return module


def _post(client, **extra):
    body = {'module': 'kimai', 'start_time': 1791633600000,  # 2026-10-10 12:00 UTC
            'end_time': 1791635400000, 'title': 'x', 'project_id': 'g:P'}
    body.update(extra)
    return client.post('/updateentry', data=json.dumps(body),
                       content_type='application/json')


def test_updateentry_reads_ms_as_utc(fake, client):
    """Epoch ms is absolute; the entry must land at the same UTC instant
    whatever the server's local timezone is."""
    response = _post(client)
    assert response.status_code == 200
    assert fake.seen[0].start_time == datetime(2026, 10, 10, 12, 0)
    assert fake.seen[0].end_time == datetime(2026, 10, 10, 12, 30)


def test_updateentry_refusal_is_a_400_with_the_reason(fake, client):
    fake.error = 'No Kimai project for entry project None'
    response = _post(client, project_id='')
    assert response.status_code == 400
    assert json.loads(response.data) == {'error': fake.error}
