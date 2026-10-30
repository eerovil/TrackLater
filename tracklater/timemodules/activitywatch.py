from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple
import requests

from tracklater import settings
from tracklater.utils import parse_time
from .interfaces import EntryMixin, AbstractParser, AbstractProvider
from tracklater.models import Entry

import logging
logger = logging.getLogger(__name__)


def get_setting(key, default=None, group='global') -> Any:
    if not hasattr(settings, 'ACTIVITYWATCH'):
        return default
    return settings.helper('ACTIVITYWATCH', key, group=group, default=default)


DEFAULTS = {
    'IDLE': 900,
    'CUTOFF': 300,
    'MERGE_GAP': 30,
}

DEFAULT_API_BASE = 'http://127.0.0.1:5600/api/0'
NOT_AFK_LABEL = 'not-afk'


def api_base_url() -> str:
    """ActivityWatch API root, e.g. http://127.0.0.1:5600/api/0."""
    explicit = get_setting('BASE_URL', default='')
    if explicit:
        return explicit.rstrip('/')
    for key in ('EVENTS_URL', 'EVENTS_URL2', 'EVENTS_URL3', 'EVENTS_URL4'):
        url = get_setting(key, default='')
        if url and '/buckets/' in url:
            return url.split('/buckets/', 1)[0].rstrip('/')
    return DEFAULT_API_BASE


def aw_request(path: str, params: Optional[dict] = None) -> Any:
    url = '{}{}'.format(api_base_url().rstrip('/'), path)
    resp = requests.get(
        url,
        params=params,
        headers={'HOST': '127.0.0.1'},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def classify_bucket(bucket_id: str, meta: dict) -> Optional[str]:
    """Return 'window', 'afk', or None (skip bucket)."""
    bucket_type = (meta.get('type') or '').lower()
    client = (meta.get('client') or '').lower()
    bid = bucket_id.lower()
    if bucket_type in ('currentwindow',) or 'window' in bid or 'window' in client:
        return 'window'
    if bucket_type in ('afkstatus',) or 'afk' in bid or 'afk' in client:
        return 'afk'
    return None


def discover_buckets() -> Dict[str, Tuple[str, dict]]:
    """Map bucket id -> (kind, metadata) for window and afk buckets."""
    raw = aw_request('/buckets/')
    found: Dict[str, Tuple[str, dict]] = {}
    for bucket_id, meta in raw.items():
        kind = classify_bucket(bucket_id, meta)
        if kind:
            found[bucket_id] = (kind, meta)
    return found


def fetch_bucket_events(
    bucket_id: str, start_date, end_date,
) -> List[dict]:
    path = '/buckets/{}/events'.format(requests.utils.quote(bucket_id, safe=''))
    return aw_request(path, params={
        'start': start_date.isoformat(),
        'end': end_date.isoformat(),
    })


def get_window(entry) -> Optional[str]:
    data = entry.get('data') or {}
    if data.get('app'):
        parts = [data['app']]
        if data.get('title'):
            parts.append(data['title'])
        if data.get('url'):
            parts.append(data['url'])
        return ' - '.join(parts)
    return None


def _intervals_overlap(
    a_start, a_end, b_start, b_end,
) -> bool:
    return a_start < b_end and b_start < a_end


def _entry_duration(entry: dict) -> float:
    return max(0.0, (entry['end_time'] - entry['time']).total_seconds())


def merge_overlapping_entries(entries: List[dict]) -> List[dict]:
    """Collapse duplicate intervals from multiple window buckets."""
    if not entries:
        return []
    entries = sorted(entries, key=lambda e: e['time'])
    merged: List[dict] = []
    for entry in entries:
        row = dict(entry)
        if not merged:
            merged.append(row)
            continue
        last = merged[-1]
        if not _intervals_overlap(
            last['time'], last['end_time'], row['time'], row['end_time'],
        ):
            merged.append(row)
            continue
        end = max(last['end_time'], row['end_time'])
        row_label = row['active_window']
        last_label = last['active_window']
        if len(row_label) > len(last_label):
            merged[-1] = {**row, 'end_time': end}
        elif len(row_label) < len(last_label):
            merged[-1] = {**last, 'end_time': end}
        elif _entry_duration(row) > _entry_duration(last):
            merged[-1] = {**row, 'end_time': end}
        else:
            merged[-1] = {**last, 'end_time': end}
    return merged


def coalesce_adjacent_entries(
    entries: List[dict], gap_seconds: float,
) -> List[dict]:
    """Merge consecutive segments with the same label when the gap is small."""
    if not entries:
        return []
    entries = sorted(entries, key=lambda e: e['time'])
    merged: List[dict] = [dict(entries[0])]
    for entry in entries[1:]:
        prev = merged[-1]
        gap = (entry['time'] - prev['end_time']).total_seconds()
        if entry['active_window'] == prev['active_window'] and gap <= gap_seconds:
            prev['end_time'] = max(prev['end_time'], entry['end_time'])
        else:
            merged.append(dict(entry))
    return merged


class Parser(EntryMixin, AbstractParser):
    """
    Only implements "get".
    """

    def get_entries(self) -> List[Entry]:
        raw_events: List[dict] = self._fetch_events()
        parsed = self._parse_events(raw_events)
        return self._generate_sessions(parsed)

    def _fetch_events(self) -> List[dict]:
        provider = Provider()
        return provider.fetch_events(self.start_date, self.end_date)

    def _parse_events(self, raw_events: List[dict]) -> List[dict]:
        window_raw: List[dict] = []
        afk_raw: List[dict] = []
        for event in raw_events:
            kind = event.get('_aw_bucket_kind')
            if kind == 'window':
                window_raw.append(event)
            elif kind == 'afk':
                afk_raw.append(event)

        afk_intervals = self._afk_intervals(afk_raw)
        window_entries = []
        for event in window_raw:
            parsed = self._parse_window_event(event)
            if parsed and not self._overlaps_any_interval(parsed, afk_intervals):
                window_entries.append(parsed)

        fallback_entries = []
        for event in afk_raw:
            parsed = self._parse_not_afk_event(event)
            if parsed and not self._covered_by_window(parsed, window_entries):
                fallback_entries.append(parsed)

        entries = window_entries + fallback_entries
        merge_gap = get_setting('MERGE_GAP', DEFAULTS['MERGE_GAP'])
        entries = merge_overlapping_entries(entries)
        entries = coalesce_adjacent_entries(entries, merge_gap)
        return entries

    def _afk_intervals(self, afk_raw: List[dict]) -> List[Tuple]:
        intervals = []
        for event in afk_raw:
            data = event.get('data') or {}
            if data.get('status') != 'afk':
                continue
            start = parse_time(event['timestamp'])
            end = start + timedelta(seconds=(event.get('duration') or 0))
            intervals.append((start, end))
        return intervals

    def _overlaps_any_interval(self, entry: dict, intervals: List[Tuple]) -> bool:
        for start, end in intervals:
            if _intervals_overlap(
                entry['time'], entry['end_time'], start, end,
            ):
                return True
        return False

    def _covered_by_window(self, entry: dict, window_entries: List[dict]) -> bool:
        for window in window_entries:
            if _intervals_overlap(
                entry['time'], entry['end_time'],
                window['time'], window['end_time'],
            ):
                return True
        return False

    def _parse_window_event(self, entry: dict) -> Optional[dict]:
        data = entry.get('data') or {}
        if data.get('status') == 'afk':
            return None
        try:
            active_window = get_window(entry)
            if active_window:
                active_window = active_window[:100]
        except Exception:
            logger.exception('Failed to parse window event: %s', entry)
            active_window = None
        if not active_window:
            return None
        time = parse_time(entry['timestamp'])
        end_time = time + timedelta(seconds=(entry.get('duration') or 1))
        return {
            'active_window': active_window,
            'time': time,
            'end_time': end_time,
            'category': 'work',
        }

    def _parse_not_afk_event(self, entry: dict) -> Optional[dict]:
        data = entry.get('data') or {}
        if data.get('status') != 'not-afk':
            return None
        time = parse_time(entry['timestamp'])
        duration = entry.get('duration') or 0
        if duration <= 0:
            return None
        end_time = time + timedelta(seconds=duration)
        return {
            'active_window': NOT_AFK_LABEL,
            'time': time,
            'end_time': end_time,
            'category': 'work',
        }

    def _generate_sessions(self, entries):
        def _add_window_time(windows: dict, groups: dict, window_name: str, seconds: float):
            if seconds <= 0:
                return
            if window_name not in windows:
                windows[window_name] = 0
            windows[window_name] += seconds

            aw_settings = getattr(settings, 'ACTIVITYWATCH', {}) or {}
            for key in aw_settings:
                for keyword in aw_settings[key].get('KEYWORDS', []):
                    if keyword in window_name:
                        if key not in groups:
                            groups[key] = 0
                        groups[key] += seconds

        def _finalize_session(start, end, windows, groups) -> Entry:
            session = Entry(
                start_time=start,
                end_time=end,
                extra_data={'windows': {}, 'group': {}},
            )
            window_list = [
                {'name': name, 'time': windows[name]}
                for name in windows
            ]
            session.text = "\n".join([
                "{}s - {}".format(int(data['time']), data['name'])
                for data in sorted(window_list, key=lambda x: x["time"], reverse=True)
            ])
            sorted_groups = sorted(groups.items(), key=lambda val: val[1], reverse=True)
            session.extra_data['groups'] = sorted_groups
            session.extra_data['windows'] = window_list
            if sorted_groups:
                session.group = sorted_groups[0][0]
                session.text = session.group + "\n" + session.text
            if session.end_time.date() != session.start_time.date():
                session.end_time = session.start_time + timedelta(hours=1)
            return session

        if not entries:
            return []

        entries = sorted(entries, key=lambda e: e['time'])
        idle = get_setting('IDLE', DEFAULTS['IDLE'])
        cutoff = get_setting('CUTOFF', DEFAULTS['CUTOFF'])

        sessions: List[Entry] = []
        session_start = None
        session_end = None
        windows: dict = {}
        groups: dict = {}
        last_end = None

        def flush():
            nonlocal session_start, session_end, windows, groups, last_end
            if session_start is None or session_end is None:
                return
            if session_end > session_start:
                sessions.append(_finalize_session(session_start, session_end, windows, groups))
            session_start = None
            session_end = None
            windows = {}
            groups = {}
            last_end = None

        for entry in entries:
            gap = 0.0 if last_end is None else (entry['time'] - last_end).total_seconds()
            if session_start is not None and gap >= idle:
                flush()

            if session_start is not None:
                projected_end = max(session_end, entry['end_time'])
                if (projected_end - session_start).total_seconds() > cutoff:
                    flush()

            if session_start is None:
                session_start = entry['time']
                session_end = entry['end_time']
                windows = {}
                groups = {}
            else:
                session_end = max(session_end, entry['end_time'])

            _add_window_time(windows, groups, entry['active_window'], _entry_duration(entry))
            last_end = entry['end_time']

        flush()
        return sessions


class Provider(AbstractProvider):
    def fetch_events(self, start_date, end_date) -> List[dict]:
        if self._use_legacy_urls():
            return self._fetch_legacy_urls(start_date, end_date)
        try:
            return self._fetch_discovered_buckets(start_date, end_date)
        except Exception:
            logger.exception('Bucket discovery failed, trying legacy EVENTS_URL settings')
            return self._fetch_legacy_urls(start_date, end_date)

    def _use_legacy_urls(self) -> bool:
        return bool(get_setting('USE_LEGACY_URLS', default=False))

    def _fetch_discovered_buckets(self, start_date, end_date) -> List[dict]:
        buckets = discover_buckets()
        if not buckets:
            logger.warning('No window or afk buckets found at %s', api_base_url())
            return []

        parsed_events: List[dict] = []
        for bucket_id, (kind, _meta) in buckets.items():
            events = fetch_bucket_events(bucket_id, start_date, end_date)
            logger.info(
                'Fetched %s events from %s bucket %s',
                len(events), kind, bucket_id,
            )
            for event in events:
                event['_aw_bucket_kind'] = kind
                parsed_events.append(event)
        return parsed_events

    def _fetch_legacy_urls(self, start_date, end_date) -> List[dict]:
        parsed_events: List[dict] = []
        urls: List[str] = []
        for key in ('EVENTS_URL', 'EVENTS_URL2', 'EVENTS_URL3', 'EVENTS_URL4'):
            url = get_setting(key, default='')
            if url:
                urls.append(url)
        if not urls:
            return self._fetch_discovered_buckets(start_date, end_date)

        for url in urls:
            if '?' not in url:
                url += '?'
            url += 'start={}&end={}'.format(
                start_date.isoformat(), end_date.isoformat(),
            )
            logger.info('Fetching events from %s', url)
            resp = requests.get(url, headers={'HOST': '127.0.0.1'}, timeout=30)
            resp.raise_for_status()
            kind = 'afk' if 'afk' in url.lower() else 'window'
            for event in resp.json():
                event['_aw_bucket_kind'] = kind
                parsed_events.append(event)
        return parsed_events

    def test_fetch_events(self, start_date=None, end_date=None):
        return [
            {
                "id": 46,
                "timestamp": "2022-09-04T04:41:31.063000+00:00",
                "duration": 10.464,
                "data": {
                    "app": "Google Chrome",
                    "url": "http://localhost:5600/api/0/buckets/",
                    "title": "",
                    "incognito": False
                },
                "_aw_bucket_kind": "window",
            },
            {
                "id": 45,
                "timestamp": "2022-09-04T04:41:29.999000+00:00",
                "duration": 0.0,
                "data": {
                    "app": "Google Chrome",
                    "url": "chrome://downloads/",
                    "title": "Downloads",
                    "incognito": False
                },
                "_aw_bucket_kind": "window",
            },
        ]
