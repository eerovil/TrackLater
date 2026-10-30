from sqlalchemy import Column, Integer, String, DateTime, Text, PickleType, Boolean
from datetime import datetime, timedelta
from typing import Optional

from tracklater.database import db
from tracklater import settings

import logging
logger = logging.getLogger(__name__)


class ApiCall(db.Model):
    pk: int = Column(Integer, primary_key=True)
    module: str = Column(String(50), nullable=False)
    created = Column(DateTime, default=datetime.utcnow)
    start_date: datetime = Column(DateTime)
    end_date: Optional[datetime] = Column(DateTime)


class Project(db.Model):
    __tablename__ = 'projects'
    module: str = Column(String(50), primary_key=True)
    pid: str = Column(String(50), primary_key=True, nullable=True)
    group: str = Column(String(50))
    title: str = Column(String(50))

    def to_dict(self):
        return {
            "title": self.title,
            "id": self.pid,
            "group": self.group,
        }


class Issue(db.Model):
    __tablename__ = 'issues'
    module: str = Column(String(50), primary_key=True)
    id: str = Column(String(50), primary_key=True, nullable=True)
    key: str = Column(String(50), primary_key=True, nullable=True)
    group: str = Column(String(50))
    title: str = Column(String(50))
    uuid: Optional[str] = Column(String(50))
    extra_data: dict = Column(PickleType)  # For custom js

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "key": self.key,
            "group": self.group,
            "extra_data": self.extra_data,
            "uuid": self.uuid
        }


class Entry(db.Model):
    __tablename__ = 'entries'
    module: str = Column(String(50), primary_key=True)
    id: str = Column(String(50), primary_key=True, nullable=True, unique=True)
    start_time: datetime = Column(DateTime, primary_key=True)
    group: Optional[str] = Column(String(50))
    end_time: Optional[datetime] = Column(DateTime)
    date_group: Optional[str] = Column(String(50))
    issue: Optional[str] = Column(String(50))  # Issue id
    project: Optional[str] = Column(String(50))  # Project id
    title: str = Column(String(255), default="")  # Title to show in timeline
    text: str = Column(Text())  # Text to show in timeline hover
    extra_data: dict = Column(PickleType)  # For custom js
    # Toggl-only (see tracklater.billing): the id this entry has in Toggl, None
    # until first pushed. Rows from every other module leave it NULL.
    toggl_id: Optional[str] = Column(String(50), nullable=True)
    # True when the entry has local changes not yet pushed to Toggl. Only the
    # toggl module ever sets this; every other module's rows stay False so the
    # per-module wipe-and-refresh in store_parser_to_database keeps working.
    is_draft: bool = Column(Boolean, default=False)

    def __init__(self, **kwargs):
        super(Entry, self).__init__(**kwargs)
        # Calculate date_group immediately
        item_time = self.start_time
        _cutoff = getattr(settings, 'CUTOFF_HOUR', 3)
        if item_time.hour >= _cutoff:
            self.date_group = item_time.strftime('%Y-%m-%d')
        else:
            self.date_group = (item_time - timedelta(days=1)).strftime('%Y-%m-%d')

    @property
    def duration(self) -> int:
        if not self.end_time:
            return 0
        return int((self.end_time - self.start_time).total_seconds())

    def to_dict(self):
        return {
            "start_time": self.start_time,
            "end_time": self.end_time,
            "id": self.id,
            "date_group": self.date_group,
            "issue": self.issue,
            "project": self.project,
            "title": self.title,
            "text": self.text,
            "extra_data": self.extra_data,
            "duration": self.duration,
            "group": self.group,
            "toggl_id": self.toggl_id,
            "is_draft": bool(self.is_draft),
        }


class EntrySuggestion(db.Model):
    """Codex-precomputed project/title hints for a time window, produced during the
    Fill (and double-click) so the editor can offer ranked picks with no live call.
    Non-authoritative: matched to an entry by time-window overlap and never
    auto-applied. Left stale if the entry is later moved (it's only a hint)."""
    __tablename__ = 'entry_suggestions'
    pk: int = Column(Integer, primary_key=True)
    start_time: datetime = Column(DateTime, nullable=False)  # UTC, like every module
    end_time: Optional[datetime] = Column(DateTime)
    date_group: Optional[str] = Column(String(50))
    # Ranked lists; project strings are Project.pid ("group:Project"), the exact
    # value the editor dropdown binds to, so no resolution is needed client-side.
    projects: list = Column(PickleType)
    titles: list = Column(PickleType)
    created = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "start_time": self.start_time,
            "end_time": self.end_time,
            "date_group": self.date_group,
            "projects": list(self.projects or []),
            "titles": list(self.titles or []),
        }


class SyncJob(db.Model):
    """
    A pending push of a toggl-module entry to the Toggl API. The background
    sync worker (tracklater.sync_worker) drains these one at a time, respecting
    Toggl's rate limit. Deduplicated by entry_id: a newer save for the same
    entry replaces the pending job rather than queueing a second one.
    """
    __tablename__ = 'sync_jobs'
    pk: int = Column(Integer, primary_key=True)
    entry_id: str = Column(String(50), unique=True, nullable=False)
    action: str = Column(String(10), nullable=False)  # create | update | delete
    # Toggl id for update/delete (the entry row may already be gone for delete).
    toggl_id: Optional[str] = Column(String(50), nullable=True)
    status: str = Column(String(10), default='pending')  # pending | failed
    error: Optional[str] = Column(Text(), nullable=True)
    created = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "pk": self.pk,
            "entry_id": self.entry_id,
            "action": self.action,
            "toggl_id": self.toggl_id,
            "status": self.status,
            "error": self.error,
            "created": self.created,
        }
