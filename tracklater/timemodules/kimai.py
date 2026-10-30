import json
from typing import Any, Dict, List, Optional, Tuple, Union, cast
from datetime import datetime

import pytz
import requests

from tracklater import settings
from tracklater.models import Entry, Issue, Project
from tracklater.utils import parse_time, _str
from .interfaces import (
    AbstractParser, AbstractProvider, AddEntryMixin, DeleteEntryMixin, EntryMixin,
    ProjectMixin, UpdateEntryMixin
)

import logging
logger = logging.getLogger(__name__)

MODULE_NAME = 'kimai'

# Kimai's API takes and returns HTML5 local date-times ("Y-m-d\TH:i:s") that are
# interpreted in the *user's* Kimai timezone, with no offset attached. TrackLater
# stores naive UTC everywhere, so every boundary crossing goes through
# to_kimai_time / parse_time.
KIMAI_TIME_FORMAT = '%Y-%m-%dT%H:%M:%S'

# GET collections are paginated; 500 is the documented maximum page size.
PAGE_SIZE = 500


def get_setting(key, default=None, group='global'):
    """settings.helper raises when a key is missing and the default is falsy, so
    optional settings (ACTIVITY) go through this and come back as None."""
    if not hasattr(settings, 'KIMAI'):
        raise KeyError("No KIMAI block in tracklater.json")
    try:
        return settings.helper('KIMAI', key, group=group, default=default)
    except KeyError:
        return default


def local_timezone():
    return pytz.timezone(getattr(settings, 'TIMEZONE', None) or 'Europe/Helsinki')


def to_kimai_time(value: datetime) -> str:
    """Naive UTC -> the local wall-clock string Kimai expects (no offset)."""
    return pytz.utc.localize(value).astimezone(local_timezone()).strftime(KIMAI_TIME_FORMAT)


def default_project_pid(group: str) -> Optional[str]:
    """Default project pid for a group, in the synthetic ``group:name`` space the
    UI and work inference use (skips Vikatilanteet-style names)."""
    kimai_settings = cast(Any, getattr(settings, 'KIMAI', {}))
    data = kimai_settings.get(group, {})
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
    Kimai (https://www.kimai.org) timesheets, via its REST API v1.1.

    Project ids live in two spaces, exactly as in the toggl module: the UI, the
    DB and work inference use synthetic ``group:name`` pids (so an entry stays
    valid with no API round-trip), while the numeric Kimai project id is only
    resolved at the write boundary via :meth:`project_maps`. That keeps this
    module a drop-in alongside toggl for everything downstream.

    Unlike toggl, writes here are immediate rather than queued as local drafts
    (is_draft / toggl_id are toggl-owned columns); create/update/delete each hit
    the API straight away.
    """

    def __init__(self, *args, **kwargs):
        super(Parser, self).__init__(*args, **kwargs)
        self.provider = Provider(get_setting('API_KEY'), get_setting('URL'))
        self._project_maps: Optional[Tuple[Dict[str, int], Dict[str, str]]] = None
        self._activity_cache: Dict[int, Optional[int]] = {}

    def project_maps(self) -> Tuple[Dict[str, int], Dict[str, str]]:
        """(synthetic->numeric, numeric->synthetic) built from settings.KIMAI plus
        the live Kimai customer/project lists. Cached per parser instance."""
        if self._project_maps is not None:
            return self._project_maps
        customers = self.provider.paged('customers', params={'visible': 3})
        projects = self.provider.paged('projects', params={'visible': 3, 'ignoreDates': 1})
        name_to_customer_id = {c['name']: c['id'] for c in customers}
        kimai_settings = cast(Any, getattr(settings, 'KIMAI', {}))
        synthetic_to_numeric: Dict[str, int] = {}
        numeric_to_synthetic: Dict[str, str] = {}
        for group, data in kimai_settings.items():
            if group == 'global' or not isinstance(data, dict):
                continue
            customer_id = name_to_customer_id.get(data.get('NAME'))
            for project_name in data.get('PROJECTS', {}):
                for project in projects:
                    if (project.get('name') == project_name
                            and project.get('customer') == customer_id):
                        synthetic = '{}:{}'.format(group, project_name)
                        synthetic_to_numeric[synthetic] = project['id']
                        numeric_to_synthetic[str(project['id'])] = synthetic
                        break
                else:
                    logger.warning(
                        "Kimai project %r not found for customer %r (group %s)",
                        project_name, data.get('NAME'), group
                    )
        self._project_maps = (synthetic_to_numeric, numeric_to_synthetic)
        return self._project_maps

    def get_entries(self) -> List[Entry]:
        if not self.start_date:
            return []
        # 'end' filters on the *start* of a record, so widen nothing: a record
        # starting inside the window is ours, whatever time it ends.
        params = {
            'begin': to_kimai_time(self.start_date),
            'end': to_kimai_time(self.end_date),
            'orderBy': 'begin',
            'order': 'ASC',
        }
        data = self.provider.paged('timesheets', params=params)
        _, numeric_to_synthetic = self.project_maps()
        entries = []
        for entry in data:
            if not entry.get('end'):
                continue  # still running; it has no duration to place on the timeline
            entries.append(resolve_entry_group_project(Entry(
                id=_str(entry['id']),
                start_time=parse_time(entry['begin']),
                end_time=parse_time(entry['end']),
                title=entry.get('description') or '',
                project=numeric_to_synthetic.get(_str(entry.get('project')) or ''),
            )))
        return entries

    def get_projects(self) -> List[Project]:
        """Synthetic ``group:name`` projects from settings.KIMAI (no API call)."""
        projects = []
        kimai_settings = cast(Any, getattr(settings, 'KIMAI', {}))
        for group, data in kimai_settings.items():
            if group == 'global' or not isinstance(data, dict):
                continue
            customer_name = data.get('NAME', group)
            for project_name in data.get('PROJECTS', {}):
                projects.append(Project(
                    pid='{}:{}'.format(group, project_name),
                    title='{} - {}'.format(customer_name, project_name),
                    group=group,
                ))
        return projects

    def _project_id_for(self, entry: Entry) -> int:
        synthetic_to_numeric, _ = self.project_maps()
        pid = _str(entry.project)
        if pid and pid in synthetic_to_numeric:
            return synthetic_to_numeric[pid]
        raise ValueError("No Kimai project for entry project {!r}".format(entry.project))

    def _activity_id_for(self, entry: Entry, project_id: int) -> int:
        """Kimai requires an activity on every timesheet, but TrackLater has no
        such concept. Prefer an explicit setting, else fall back to the project's
        first visible activity (global activities included)."""
        group = entry.group or (
            str(entry.project).split(':', 1)[0] if entry.project and ':' in str(entry.project)
            else 'global'
        )
        configured = get_setting('ACTIVITY', group=group)
        if configured:
            return int(configured)
        if project_id not in self._activity_cache:
            activities = self.provider.paged(
                'activities', params={'project': project_id, 'visible': 1}
            )
            self._activity_cache[project_id] = activities[0]['id'] if activities else None
        activity_id = self._activity_cache[project_id]
        if not activity_id:
            raise ValueError(
                "No Kimai activity for project {}; set KIMAI.<group>.ACTIVITY".format(project_id)
            )
        return activity_id

    def _form_data(self, new_entry: Entry) -> dict:
        if new_entry.end_time is None:
            raise ValueError("No end_time")
        resolve_entry_group_project(new_entry)
        project_id = self._project_id_for(new_entry)
        return {
            'begin': to_kimai_time(new_entry.start_time),
            'end': to_kimai_time(new_entry.end_time),
            'project': project_id,
            'activity': self._activity_id_for(new_entry, project_id),
            'description': new_entry.title or '',
        }

    def _to_entry(self, data: dict) -> Entry:
        # The group is carried in the synthetic pid ("group:name"); without it
        # the timeline has nothing to colour an entry by and every row falls
        # back to the module's global colour.
        _, numeric_to_synthetic = self.project_maps()
        return resolve_entry_group_project(Entry(
            id=_str(data['id']),
            start_time=parse_time(data['begin']),
            end_time=parse_time(data['end']) if data.get('end') else None,
            title=data.get('description') or '',
            project=numeric_to_synthetic.get(_str(data.get('project')) or ''),
        ))

    def create_entry(self, new_entry: Entry, issue: Optional[Issue]) -> Entry:
        if issue and issue.group:
            new_entry.group = issue.group
        data = self.provider.request(
            'timesheets', data=json.dumps(self._form_data(new_entry)), method='POST'
        )
        return self._to_entry(cast(dict, data))

    def update_entry(self, entry_id: str, new_entry: Entry, issue: Optional[Issue]) -> Entry:
        if issue and issue.group:
            new_entry.group = issue.group
        data = self.provider.request(
            'timesheets/{}'.format(entry_id),
            data=json.dumps(self._form_data(new_entry)), method='PATCH'
        )
        return self._to_entry(cast(dict, data))

    def delete_entry(self, entry_id: str) -> None:
        self.provider.request('timesheets/{}'.format(entry_id), method='DELETE')


class Provider(AbstractProvider):
    def __init__(self, api_key, url):
        self.api_key = api_key
        self.url = (url or '').rstrip('/')
        self.id_counter = 100  # only used by the test double

    def paged(self, endpoint: str, params: Optional[dict] = None) -> List[dict]:
        """Walk a paginated GET collection. Kimai answers 404 once the requested
        page is past the end, which the request layer turns into an empty list."""
        results: List[dict] = []
        page = 1
        while True:
            _params = dict(params or {})
            _params.update({'page': page, 'size': PAGE_SIZE})
            data = self.request(endpoint, params=_params, method='GET')
            if not isinstance(data, list) or not data:
                break
            results.extend(data)
            if len(data) < PAGE_SIZE:
                break
            page += 1
        return results

    def request(self, endpoint: str, **kwargs) -> Union[List[dict], dict]:
        url = '{}/api/{}'.format(self.url, endpoint)
        headers = kwargs.pop('headers', {})
        headers.setdefault('Content-Type', 'application/json')
        headers['Authorization'] = 'Bearer {}'.format(self.api_key)
        kwargs['headers'] = headers

        method = kwargs.pop('method', 'POST').lower()
        response = getattr(requests, method)(url, **kwargs)
        # A page past the end of a collection is a 404, not an error.
        if response.status_code == 404 and method == 'get':
            return []
        if not response.ok:
            logger.error("Kimai %s %s -> %s: %s",
                         method.upper(), endpoint, response.status_code, response.content[:500])
            response.raise_for_status()
        if method == 'delete' or not response.content:
            return {}
        try:
            return response.json()
        except Exception as e:
            logger.exception("%s: %s", response.content[:500], e)
            raise

    # --- test doubles -------------------------------------------------------
    # Mirrored on settings.KIMAI so project_maps() resolves the same synthetic
    # ids a real instance would, and writes echo the payload back the way the
    # API does (offset-bearing timestamps included).

    @staticmethod
    def _test_fixtures() -> Tuple[List[dict], List[dict]]:
        kimai_settings = cast(Any, getattr(settings, 'KIMAI', {}))
        customers: List[dict] = []
        projects: List[dict] = []
        for group, data in kimai_settings.items():
            if group == 'global' or not isinstance(data, dict):
                continue
            name = data.get('NAME', group)
            customer = next((c for c in customers if c['name'] == name), None)
            if customer is None:
                customer = {'id': len(customers) + 1, 'name': name}
                customers.append(customer)
            for project_name in data.get('PROJECTS', {}):
                projects.append({
                    'id': len(projects) + 10,
                    'name': project_name,
                    'customer': customer['id'],
                })
        return customers, projects

    def test_paged(self, endpoint: str, params: Optional[dict] = None) -> List[dict]:
        customers, projects = self._test_fixtures()
        if endpoint == 'customers':
            return customers
        if endpoint == 'projects':
            return projects
        if endpoint == 'activities':
            return [{'id': 1, 'name': 'Test activity'}]
        return []

    def test_request(self, endpoint: str, **kwargs) -> Union[List[dict], dict, str]:
        method = kwargs.get('method', 'POST').lower()
        if endpoint == 'timesheets' and method == 'post':
            self.id_counter += 1
            return self._test_timesheet(kwargs, self.id_counter)
        if endpoint.startswith('timesheets/') and method == 'patch':
            return self._test_timesheet(kwargs, endpoint.rsplit('/', 1)[1])
        if endpoint.startswith('timesheets/') and method == 'delete':
            return {}
        return []

    @staticmethod
    def _test_timesheet(kwargs: dict, entry_id) -> dict:
        payload = json.loads(kwargs['data'])
        payload['id'] = entry_id
        # The API answers with offsets; to_kimai_time strips them on the way out.
        for key in ('begin', 'end'):
            if payload.get(key):
                naive = datetime.strptime(payload[key], KIMAI_TIME_FORMAT)
                payload[key] = local_timezone().localize(naive).strftime(
                    KIMAI_TIME_FORMAT + '%z'
                )
        return payload
