import os
import tempfile

# Point every create_app() at a throwaway database BEFORE any test module is
# imported (test_app/test_ai_local call create_app() at import time). Without
# this, the engine binds to the real tracklater/database.db and tests mutate it.
# One file per run: two checkouts testing at once must not wipe each other's rows.
_TEST_DB = os.path.join(tempfile.gettempdir(), 'tracklater_pytest_{}.db'.format(os.getpid()))
os.environ.setdefault('TRACKLATER_DB_URI', 'sqlite:///{}'.format(_TEST_DB))

import pytest
from tracklater import settings
from tracklater import test_settings


@pytest.fixture(autouse=True)
def mock_settings(monkeypatch):
    """
    Replace settings completely with test_settings
    """
    # Union both sides: keys the developer's real config happens to carry get
    # blanked, and keys only test_settings defines get created. Iterating just
    # dir(settings) made the test settings depend on whatever ~/.config held.
    names = {item for item in dir(settings) if not item.startswith("__")}
    names |= {item for item in dir(test_settings) if not item.startswith("__")}
    for module_setting in sorted(names):
        if module_setting == 'helper':
            continue
        monkeypatch.setattr(
            'tracklater.settings.{}'.format(module_setting),
            getattr(test_settings, module_setting, {}),
            raising=False,
        )


@pytest.fixture()
def db():
    from tracklater.database import db
    return db


def pytest_sessionfinish(session, exitstatus):
    if os.path.exists(_TEST_DB):
        os.remove(_TEST_DB)
