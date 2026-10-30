"""
Lightweight, idempotent schema/data migrations for the SQLite database.

There is no Alembic in this project; schema changes are applied here at startup
via ``ALTER TABLE ... ADD COLUMN`` (SQLite ignores the column if we guard with a
PRAGMA check) plus targeted one-off data moves. Every step must be safe to run
repeatedly.
"""
from sqlalchemy import text

from tracklater.database import db

import logging
logger = logging.getLogger(__name__)


def _entries_columns():
    rows = db.session.execute(text("PRAGMA table_info(entries)")).fetchall()
    return {row[1] for row in rows}  # row[1] is the column name


def _add_column_if_missing(existing, column, ddl):
    if column in existing:
        return False
    db.session.execute(text("ALTER TABLE entries ADD COLUMN {}".format(ddl)))
    logger.warning("Migration: added entries.%s", column)
    return True


def migrate_local_module_to_toggl():
    """
    Move legacy ``local`` module entries into the ``toggl`` module as unsynced
    drafts. Project pids stay in the ``group:name`` space the toggl module now
    uses for the UI, so no project rewrite is needed. Runs once: after the move
    no ``local`` rows remain.
    """
    count = db.session.execute(
        text("SELECT COUNT(*) FROM entries WHERE module = 'local'")
    ).scalar()
    if not count:
        return
    db.session.execute(text(
        "UPDATE entries SET module = 'toggl', is_draft = 1, toggl_id = NULL "
        "WHERE module = 'local'"
    ))
    db.session.commit()
    logger.warning("Migration: moved %s local entries to toggl drafts", count)


def run_migrations():
    """Apply all pending migrations. Called once at app startup under app context."""
    from tracklater import settings
    if getattr(settings, 'TESTING', False):
        # Never run destructive data migrations against whatever DB a test run is
        # pointed at; tests build their schema with db.create_all().
        return
    existing = _entries_columns()
    changed = False
    changed |= _add_column_if_missing(
        existing, 'toggl_id', 'toggl_id VARCHAR(50)')
    changed |= _add_column_if_missing(
        existing, 'is_draft', 'is_draft BOOLEAN DEFAULT 0')
    if changed:
        db.session.commit()
    migrate_local_module_to_toggl()
