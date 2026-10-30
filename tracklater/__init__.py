from flask import Flask
import os
import sys
import tempfile
from tracklater.database import db
from tracklater.settings_utils import settings_wrapper as settings  # noqa

import logging
logger = logging.getLogger(__name__)


def _database_uri():
    """Resolve the SQLite URI, binding the engine at db.init_app() time (this
    flask_sqlalchemy version ignores per-test config overrides made afterward).

    TRACKLATER_DB_URI wins when set. As a hard safety net, create_app() called
    during a pytest session NEVER falls back to the real database.db — the suite
    spins up apps at import/collection time where the env var can race, and a
    single such miss would migrate/mutate the user's live billing data."""
    uri = os.environ.get('TRACKLATER_DB_URI')
    if uri:
        return uri
    if 'pytest' in sys.modules or 'PYTEST_CURRENT_TEST' in os.environ:
        return 'sqlite:///{}'.format(
            os.path.join(tempfile.gettempdir(), 'tracklater_pytest_fallback.db'))
    directory = os.path.dirname(os.path.realpath(__file__))
    return 'sqlite:///{}/database.db'.format(directory)


def create_app(name=__name__):
    app = Flask(name)

    app.config['SQLALCHEMY_DATABASE_URI'] = _database_uri()
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    from tracklater.models import (  # noqa
        ApiCall, Project, Issue, Entry, SyncJob, EntrySuggestion,
    )

    db.init_app(app)
    with app.app_context():
        db.create_all()
        from tracklater.migrations import run_migrations
        run_migrations()

    from tracklater import views

    app.register_blueprint(views.bp)

    # The sync worker only serves the toggl module's draft push queue; billing
    # now writes straight through (tracklater.billing), so skip it unless toggl
    # is explicitly enabled.
    from tracklater.timemodules.toggl import MODULE_NAME as TOGGL_MODULE
    if TOGGL_MODULE in settings.ENABLED_MODULES:
        from tracklater.sync_worker import start_worker
        start_worker(app)

    return app


def run():
    app = create_app(name="tracklater")
    app.run(debug=True, port=5000, host="localhost")
