import requests
import json
import uuid
from typing import List, Union, cast, Any, Optional, Dict, Tuple
from datetime import timedelta

from tracklater.utils import parse_time, _str
from tracklater import settings
from tracklater.database import db
from .interfaces import (
    EntryMixin, AddEntryMixin, UpdateEntryMixin, DeleteEntryMixin, ProjectMixin, AbstractParser,
    AbstractProvider
)
from tracklater.models import Entry, Project, Issue


import logging
logger = logging.getLogger(__name__)

MODULE_NAME = 'toggl'


def get_setting(key, default=None, group='global'):
    return settings.helper('TOGGL', key, group=group, default=default)


class QuotaExceeded(Exception):
    """Raised when Toggl returns HTTP 429. ``reset_seconds`` is how long the
    caller should back off before retrying (best-effort from response headers)."""

    def __init__(self, reset_seconds: int = 60):
        super().__init__("Toggl rate limit hit; reset in {}s".format(reset_seconds))
        self.reset_seconds = reset_seconds


def default_project_pid(group: str) -> Optional[str]:
    """Default project pid for a group, in the synthetic ``group:name`` space the
    UI and work inference use (skips Vikatilanteet-style names)."""
    toggl_settings = cast(Any, getattr(settings, 'TOGGL', {}))
    data = toggl_settings.get(group, {})
    if not isinstance(data, dict):
        return None
    projects = data.get('PROJECTS', {})
    for project_name in projects:
        if 'vika' in project_name.lower():
            continue
        return '{}:{}'.format(group, project_name)
    if projects:
        return '{}:{}'.format(group, next(iter(projects)))
    return None


def resolve_entry_group_project(entry: Entry) -> Entry:
    if entry.project and not entry.group:
        pid = str(entry.project)
        if ':' in pid:
            entry.group = pid.split(':', 1)[0]
    if entry.group and not entry.project:
        entry.project = default_project_pid(entry.group)
    return entry


class Parser(EntryMixin, AddEntryMixin, UpdateEntryMixin, DeleteEntryMixin, ProjectMixin,
             AbstractParser):
    """
    Toggl is the system of record, but TrackLater keeps a local working copy in
    the database. Entries are created/edited locally as *drafts* (is_draft=True)
    and pushed to the Toggl API lazily by the background sync worker. Fetching
    from Toggl refreshes synced entries while preserving local drafts.

    Project ids live in two spaces: the UI/DB use synthetic ``group:name`` pids
    (so entries stay valid without an API round-trip); the numeric Toggl project
    id is only resolved at the push boundary via :meth:`project_maps`.
    """

    def __init__(self, *args, **kwargs):
        super(Parser, self).__init__(*args, **kwargs)
        self.provider = Provider(get_setting('API_KEY'))
        self._workspace_id = None
        self._project_maps: Optional[Tuple[Dict[str, Any], Dict[str, str]]] = None

    @property
    def workspace_id(self):
        # Lazily resolved so merely constructing a Parser (done all over views.py)
        # costs no API call / quota; only push and fetch need it.
        if self._workspace_id is None:
            workspaces = self.provider.request('workspaces', method='GET')
            self._workspace_id = workspaces[0]['id']
        return self._workspace_id

    def project_maps(self) -> Tuple[Dict[str, Any], Dict[str, str]]:
        """(synthetic->numeric, numeric->synthetic) built from settings.TOGGL +
        the live Toggl client/project list. Cached per parser instance."""
        if self._project_maps is not None:
            return self._project_maps
        clients = self.provider.request('me/clients', method='GET') or []
        projects = self.provider.request('me/projects', method='GET') or []
        name_to_client_id = {c['name']: c['id'] for c in clients}
        toggl_settings = cast(Any, settings.TOGGL)
        synthetic_to_numeric: Dict[str, Any] = {}
        numeric_to_synthetic: Dict[str, str] = {}
        for group, data in toggl_settings.items():
            if group == 'global' or not isinstance(data, dict):
                continue
            client_id = name_to_client_id.get(data.get('NAME'))
            for project_name in data.get('PROJECTS', {}):
                for project in projects:
                    if (project.get('name') == project_name
                            and project.get('client_id') == client_id):
                        synthetic = '{}:{}'.format(group, project_name)
                        synthetic_to_numeric[synthetic] = project['id']
                        numeric_to_synthetic[str(project['id'])] = synthetic
                        break
        self._project_maps = (synthetic_to_numeric, numeric_to_synthetic)
        return self._project_maps

    def get_entries(self) -> List[Entry]:
        if not self.start_date:
            return []
        params = {'start_date': self.start_date.isoformat() + "+00:00",
                  'end_date': self.end_date.isoformat() + "+00:00"}
        data = self.provider.request('me/time_entries', params=params, method='GET')
        _, numeric_to_synthetic = self.project_maps()
        # Entries edited locally after a previous sync live in the DB as drafts
        # carrying their toggl_id. The Toggl copy is stale relative to the draft,
        # so skip it on fetch and let the draft win until it is pushed.
        draft_toggl_ids = {
            row.toggl_id for row in Entry.query.filter(
                Entry.module == MODULE_NAME,
                Entry.is_draft == True,  # noqa: E712
                Entry.toggl_id.isnot(None),
            ).all()
        }
        time_entries = []
        for entry in data or []:
            toggl_id = _str(entry['id'])
            if toggl_id in draft_toggl_ids:
                continue
            time_entries.append(Entry(
                id=toggl_id,
                toggl_id=toggl_id,
                is_draft=False,
                start_time=parse_time(entry['start']),
                end_time=parse_time(entry['stop']) if entry.get('stop') else None,
                title=entry.get('description', ''),
                project=numeric_to_synthetic.get(_str(entry.get('pid') or entry.get('project_id'))),
            ))
        return time_entries

    def get_projects(self) -> List[Project]:
        """Synthetic ``group:name`` projects from settings.TOGGL (no API call)."""
        projects = []
        toggl_settings = cast(Any, getattr(settings, 'TOGGL', {}))
        for group, data in toggl_settings.items():
            if group == 'global' or not isinstance(data, dict):
                continue
            client_name = data.get('NAME', group)
            for project_name in data.get('PROJECTS', {}):
                pid = '{}:{}'.format(group, project_name)
                projects.append(Project(
                    pid=pid,
                    title='{} - {}'.format(client_name, project_name),
                    group=group,
                ))
        return projects

    def create_entry(self, new_entry: Entry, issue: Optional[Issue]) -> Entry:
        # Local draft only; the sync worker pushes it to Toggl when the week is saved.
        if issue and issue.group:
            new_entry.group = issue.group
        resolve_entry_group_project(new_entry)
        return Entry(
            id=str(uuid.uuid4()),
            toggl_id=None,
            is_draft=True,
            start_time=new_entry.start_time,
            end_time=new_entry.end_time,
            title=new_entry.title,
            project=new_entry.project,
            group=new_entry.group,
            text=new_entry.text or "",
            extra_data=new_entry.extra_data,
        )

    def update_entry(self, entry_id: str, new_entry: Entry, issue: Optional[Issue]) -> Entry:
        if issue and issue.group:
            new_entry.group = issue.group
        resolve_entry_group_project(new_entry)
        existing = Entry.query.filter(
            Entry.module == MODULE_NAME, Entry.id == entry_id
        ).first()
        return Entry(
            id=entry_id,
            # Preserve the link to Toggl so the worker UPDATEs instead of creating
            # a duplicate; editing flips the entry back to draft.
            toggl_id=existing.toggl_id if existing else None,
            is_draft=True,
            start_time=new_entry.start_time,
            end_time=new_entry.end_time,
            title=new_entry.title,
            project=new_entry.project,
            group=new_entry.group,
            text=new_entry.text or "",
            extra_data=new_entry.extra_data,
        )

    def delete_entry(self, entry_id: str) -> None:
        # The DB row + Toggl delete enqueue are handled in views.deleteentry,
        # which has the toggl_id before the row is removed. Keep the in-memory
        # list consistent for callers that hold a parsed Parser.
        for i, entry in enumerate(self.entries):
            if entry.id == str(entry_id):
                del self.entries[i]
                break

    # --------------------------------------------------------------------- #
    # Push boundary — called by the background sync worker (hits the API).
    # --------------------------------------------------------------------- #
    def _project_id_for(self, entry: Entry) -> Optional[int]:
        if not entry.project:
            return None
        synthetic_to_numeric, _ = self.project_maps()
        pid = synthetic_to_numeric.get(str(entry.project))
        return int(pid) if pid is not None else None

    def push_entry(self, entry: Entry, toggl_id: Optional[str] = None) -> dict:
        """Create (toggl_id None) or update an entry in Toggl. Returns the Toggl
        response dict (with its ``id``). Raises QuotaExceeded on 429."""
        headers = {"Content-Type": "application/json"}
        data = {
            "description": entry.title,
            "start": entry.start_time.isoformat() + "+00:00",
            "created_with": "tracklater",
            "workspace_id": self.workspace_id,
            "billable": True,
            "project_id": self._project_id_for(entry),
        }
        if entry.end_time:
            # v9 expects a positive duration (seconds) for a completed entry;
            # without it the entry can be treated as still running.
            data["stop"] = entry.end_time.isoformat() + "+00:00"
            data["duration"] = int((entry.end_time - entry.start_time).total_seconds())
        else:
            # Running/open entry: Toggl v9 uses duration -1.
            data["duration"] = -1
        if toggl_id:
            try:
                return self.provider.request(
                    'workspaces/{}/time_entries/{}'.format(self.workspace_id, toggl_id),
                    data=json.dumps(data), headers=headers, method='PUT'
                )
            except QuotaExceeded:
                raise
            except Exception as exc:
                if 'not found' not in str(exc).lower():
                    raise
                # The entry was deleted in Toggl since we last synced; fall through
                # and recreate it (the caller updates the stored toggl_id).
                logger.warning(
                    "Toggl entry %s not found on update; creating a new one", toggl_id
                )
        return self.provider.request(
            'workspaces/{}/time_entries'.format(self.workspace_id),
            data=json.dumps(data), headers=headers, method='POST'
        )

    def delete_remote(self, toggl_id: str) -> None:
        self.provider.request(
            'workspaces/{}/time_entries/{}'.format(self.workspace_id, toggl_id),
            method='DELETE'
        )


class Provider(AbstractProvider):
    def __init__(self, api_key):
        self.api_key = api_key
        self.id_counter = 4

    @staticmethod
    def _parse_reset_seconds(response) -> int:
        raw = response.headers.get('X-Toggl-Quota-Resets-In', '')
        try:
            return max(1, int(float(raw)))
        except (TypeError, ValueError):
            return 60

    def request(self, endpoint: str, **kwargs) -> Union[List[dict], dict]:
        url = 'https://api.track.toggl.com/api/v9/{}'.format(endpoint)
        kwargs['headers'] = kwargs.get('headers', {
            "Content-Type": "application/json"
        })
        kwargs['auth'] = kwargs.get('auth', (self.api_key, 'api_token'))

        method = kwargs.get('method', 'POST').lower()
        try:
            del kwargs['method']
        except KeyError:
            pass
        response = getattr(requests, method)(url, **kwargs)
        if response.status_code == 429:
            raise QuotaExceeded(self._parse_reset_seconds(response))
        if not response.content:
            return
        try:
            ret = response.json()
            if response.status_code >= 400:
                if method == "delete" and "not found" in str(ret).lower():
                    return  # This is ok
                # Redact auth so the API token never lands in the logs.
                safe = {k: ('<redacted>' if k == 'auth' else v) for k, v in kwargs.items()}
                logger.exception("%s: %s, - %s", url, safe, response.content)
                raise Exception(ret)
            return ret
        except QuotaExceeded:
            raise
        except Exception as e:
            logger.exception("%s - %s: %s", url, response.content, e)
            raise

    def test_request(self, endpoint: str, **kwargs) -> Union[List[dict], dict, str]:
        method = kwargs.get('method', 'POST').lower()
        if endpoint == "workspaces" and method == 'get':
            return [{"id": "ws1", "name": "Test Workspace"}]
        elif endpoint == "me/time_entries" and method == 'get':
            return [
                {
                    "id": "1",
                    "pid": "10",
                    "start": "2019-05-09T08:00:00+00:00",
                    "stop": "2019-05-09T09:00:00+00:00",
                    "description": "Toggl entry 1",
                },
                {
                    "id": "2",
                    "pid": "11",
                    "start": "2019-05-13T07:42:55+00:00",
                    "stop": "2019-05-13T08:34:52+00:00",
                    "description": "Toggl entry 2",
                },
                {
                    "id": "3",
                    "pid": "20",
                    "start": "2019-05-13T09:35:11+00:00",
                    "stop": "2019-05-13T10:34:02+00:00",
                    "description": "Toggl entry 3",
                }
            ]
        elif endpoint == "me/clients" and method == 'get':
            return [
                {"id": 1, "name": "First Client"},
                {"id": 2, "name": "Second Client"},
            ]
        elif endpoint == "me/projects" and method == 'get':
            return [
                {"id": 10, "name": "Development", "client_id": 1},
                {"id": 11, "name": "Bug fixing", "client_id": 1},
                {"id": 20, "name": "Development", "client_id": 2},
                {"id": 21, "name": "Bug fixing", "client_id": 2},
            ]
        elif endpoint.startswith("workspaces/") and "time_entries" in endpoint and method == 'post':
            entry = json.loads(kwargs['data'])
            entry['id'] = self.id_counter
            self.id_counter += 1
            return entry
        elif endpoint.startswith("workspaces/") and "time_entries" in endpoint and method == 'put':
            entry = json.loads(kwargs['data'])
            entry['id'] = endpoint.rsplit('/', 1)[1]
            return entry
        elif endpoint.startswith("workspaces/") and "time_entries" in endpoint and method == 'delete':
            return endpoint.rsplit('/', 1)[1]
        return [{}]
