import os
from datetime import datetime, timedelta

import pytest

from tracklater import create_app
from tracklater.models import Entry
from tracklater.ai_local import persist_local_entries, populate_local_entries

DIRECTORY = os.path.dirname(os.path.realpath(__file__))
app = create_app()


@pytest.fixture
def client(db):
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///{}/database_ai_test.db'.format(DIRECTORY)
    with app.app_context():
        db.create_all()
    yield app.test_client()
    path = '{}/database_ai_test.db'.format(DIRECTORY)
    if os.path.exists(path):
        os.remove(path)


def test_populate_local_from_git_commit(client, monkeypatch):
    from tracklater import settings as app_settings
    from tracklater.database import db

    monkeypatch.setattr(app_settings, 'ENABLED_MODULES', ['gitmodule', 'kimai'])
    monkeypatch.setattr(app_settings, 'KIMAI', {
        'global': {'API_KEY': 'x', 'URL': 'https://kimai.test'},
        'group1': {
            'NAME': 'First',
            'PROJECTS': {'Development': 'default'},
        },
    })

    start = datetime(2020, 5, 22, 6, 0, 0)
    end = datetime(2020, 5, 22, 20, 0, 0)
    with app.app_context():
        db.session.query(Entry).delete()
        db.session.add(Entry(
            module='gitmodule',
            id='git-pop-1',
            start_time=datetime(2020, 5, 22, 9, 0, 0),
            group='group1',
            text='repo [main] - auto translate updates',
        ))
        db.session.commit()
        created = populate_local_entries(start, end)
    assert len(created) == 1
    assert created[0].project == "group1:Development"
    assert 'auto translate updates' in (created[0].title or '').lower()
    assert (created[0].end_time - created[0].start_time) >= timedelta(minutes=30)


def test_persist_replaces_selected_day_without_touching_next_day(client, monkeypatch):
    from tracklater import settings as app_settings
    from tracklater.database import db

    monkeypatch.setattr(app_settings, 'ENABLED_MODULES', ['kimai'])
    monkeypatch.setattr(app_settings, 'KIMAI', {
        'global': {'API_KEY': 'x', 'URL': 'https://kimai.test'},
        'group1': {
            'NAME': 'First',
            'PROJECTS': {'Development': 'default'},
        },
    })
    day_start = datetime(2026, 7, 15, 21, 0)  # Jul 16 midnight in Helsinki
    next_day_start = datetime(2026, 7, 16, 21, 0)
    with app.app_context():
        db.session.query(Entry).delete()
        db.session.add(Entry(
            module='kimai', id='old-selected-day', start_time=day_start,
            end_time=day_start + timedelta(hours=1), is_draft=True,
        ))
        db.session.add(Entry(
            module='kimai', id='next-day', start_time=next_day_start,
            end_time=next_day_start + timedelta(hours=1), is_draft=True,
        ))
        db.session.commit()

        persist_local_entries([{
            'start_time': day_start + timedelta(hours=8),
            'end_time': day_start + timedelta(hours=9),
            'title': 'Replacement',
            'project': 'group1:Development',
        }], day_start, next_day_start, replace_existing=True)

        assert Entry.query.filter_by(id='old-selected-day').first() is None
        assert Entry.query.filter_by(id='next-day').first() is not None
        assert Entry.query.filter_by(title='Replacement').first() is not None
