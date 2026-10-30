"""
Real ActivityWatch + git data exported from 2026-06-01 (TrackLater database).

Source: tracklater/database.db on 2026-06-01. Second commit is stored as metso
in timeline_data.json (converted from outdoor for the split-project test).

Regenerate JSON after changing production data:

    python3 tracklater/tests/integration/export_timeline_data.py
    python3 tracklater/tests/integration/export_timeline_data.py \\
        --from 2026-05-25 --to 2026-06-01 \\
        --out tracklater/tests/integration/last_week_timeline_data.json \\
        --second-commit-group ""
"""
import json
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from tracklater.database import db
from tracklater.models import Entry

_DATA_PATH = os.path.join(os.path.dirname(__file__), 'timeline_data.json')
_LAST_WEEK_PATH = os.path.join(os.path.dirname(__file__), 'last_week_timeline_data.json')

with open(_DATA_PATH, encoding='utf-8') as _handle:
    _DATA: Dict[str, Any] = json.load(_handle)

with open(_LAST_WEEK_PATH, encoding='utf-8') as _handle:
    _LAST_WEEK_DATA: Dict[str, Any] = json.load(_handle)

TIMELINE_DAY = datetime.strptime(_DATA['source_date'], '%Y-%m-%d')

LOCAL_GROUPS = {
    'outdoor': {
        'NAME': 'Scandinavian Outdoor',
        'PROJECTS': {
            'Verkkokauppakehitys': 'default',
            'Vikatilanteet': 'default',
        },
    },
    'metso': {
        'NAME': 'Scandinavian Outdoor',
        'PROJECTS': {
            'Metso-ostojärjestelmäkehitys': 'default',
        },
    },
    'storm': {
        'NAME': 'Storm',
        'PROJECTS': {'Verkkokauppakehitys': 'default'},
    },
}


def project_id(group: str) -> str:
    if group == 'metso':
        return 'metso:Metso-ostojärjestelmäkehitys'
    if group == 'storm':
        return 'storm:Verkkokauppakehitys'
    return 'outdoor:Verkkokauppakehitys'


def _parse_db_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')


def allowed_projects() -> set:
    return {project_id(group) for group in LOCAL_GROUPS}


def _times_from_data(data: Dict[str, Any]) -> List[datetime]:
    times: List[datetime] = []
    for row in data['activitywatch']:
        times.append(_parse_db_time(row['start_time']))  # type: ignore
        end = _parse_db_time(row.get('end_time'))
        if end:
            times.append(end)
    for row in data['git_commits']:
        times.append(_parse_db_time(row['start_time']))  # type: ignore
    return times


def day_range() -> Tuple[datetime, datetime]:
    """Populate range covering all fixture activity (with margin)."""
    times = _times_from_data(_DATA)
    start = min(times).replace(hour=0, minute=0, second=0, microsecond=0)
    end = max(times).replace(hour=23, minute=59, second=59, microsecond=0)
    return start, end


def last_week_range() -> Tuple[datetime, datetime]:
    """2026-05-25 … 2026-06-01 exported from production database."""
    times = _times_from_data(_LAST_WEEK_DATA)
    start = min(times).replace(hour=0, minute=0, second=0, microsecond=0)
    end = max(times).replace(hour=23, minute=59, second=59, microsecond=0)
    return start, end


def _commit_summary(text: str) -> str:
    lines = (text or '').split('\n')
    if len(lines) > 1 and lines[1].strip():
        return lines[1].strip()[:80]
    return (lines[0] if lines else '')[:80]


def _seed_from_data(
    data: Dict[str, Any],
    second_commit_group: Optional[str] = None,
) -> List[Tuple[datetime, str, str]]:
    db.session.query(Entry).delete()

    for row in data['activitywatch']:
        db.session.add(Entry(
            module='activitywatch',
            id=row['id'],
            start_time=_parse_db_time(row['start_time']),
            end_time=_parse_db_time(row.get('end_time')),
            group=row.get('group') or '',
            title=row.get('title') or '',
            text=row.get('text') or '',
        ))

    commit_specs: List[Tuple[datetime, str, str]] = []
    for index, row in enumerate(data['git_commits']):
        group = row['group'] or 'outdoor'
        text = row.get('text') or ''
        if index == 1 and second_commit_group is not None:
            group = second_commit_group
            orig = row.get('text') or ''
            if ' - ' in orig:
                _, msg = orig.split(' - ', 1)
                text = '{} [main] - {}'.format(group, msg)
                if '\n' in orig:
                    text = text + '\n' + orig.split('\n', 1)[1]
            else:
                text = group

        commit_time = _parse_db_time(row['start_time'])
        db.session.add(Entry(
            module='gitmodule',
            id=row['id'],
            start_time=commit_time,
            group=group,
            text=text,
        ))
        commit_specs.append((
            commit_time,  # type: ignore
            group,
            _commit_summary(text),
        ))

    db.session.commit()
    return commit_specs


def seed_timeline(
    second_commit_group: Optional[str] = None,
) -> List[Tuple[datetime, str, str]]:
    """
    Insert exported AW rows and git commits.

    second_commit_group: if set, overrides the second commit's group (e.g. 'outdoor'
    for same-project test; JSON default is 'metso').
    Returns commit specs: (time, group, title_substring).
    """
    return _seed_from_data(_DATA, second_commit_group=second_commit_group)


def seed_last_week() -> List[Tuple[datetime, str, str]]:
    """Insert last-week export (2026-05-25 … 2026-06-01) without overrides."""
    return _seed_from_data(_LAST_WEEK_DATA)


def seed_two_commit_morning(
    second_commit_group: str = 'outdoor',
) -> List[Tuple[datetime, str, str]]:
    """Backward-compatible alias for integration tests."""
    if second_commit_group == 'outdoor':
        return seed_timeline(second_commit_group='outdoor')
    return seed_timeline()
