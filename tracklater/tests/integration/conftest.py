import os

import pytest

from tracklater import create_app
from tracklater.database import db
from tracklater.tests.integration.timeline_fixture import LOCAL_GROUPS

DIRECTORY = os.path.dirname(os.path.realpath(__file__))


@pytest.fixture(scope='module')
def integration_app():
    app = create_app()
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///{}/database_populate_local.db'.format(
        DIRECTORY
    )
    return app


@pytest.fixture
def integration_db(integration_app, monkeypatch):
    from tracklater import settings as app_settings

    monkeypatch.setattr(
        app_settings, 'ENABLED_MODULES',
        ['activitywatch', 'gitmodule', 'kimai'],
        raising=False,
    )
    # The kimai module is the billing module; default_project_pid reads KIMAI.
    monkeypatch.setattr(app_settings, 'KIMAI', dict(
        LOCAL_GROUPS, **{'global': {'API_KEY': 'x', 'URL': 'https://kimai.test'}}), raising=False)
    monkeypatch.setattr(app_settings, 'LOCAL', LOCAL_GROUPS, raising=False)

    with integration_app.app_context():
        db.create_all()
        yield db
        db.session.remove()
    path = '{}/database_populate_local.db'.format(DIRECTORY)
    if os.path.exists(path):
        os.remove(path)
