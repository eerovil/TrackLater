"""
Infer work windows before git commits and build anchored local time entries.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from tracklater.models import Entry
from tracklater.timemodules.activitywatch import discover_buckets, fetch_bucket_events
from tracklater.utils import parse_time

import logging
logger = logging.getLogger(__name__)

MIN_WORK_BEFORE_COMMIT = timedelta(minutes=30)
MAX_WORK_BEFORE_COMMIT = timedelta(hours=1)
MIN_WORK_AFTER_COMMIT = timedelta(minutes=15)
MIN_LOCAL_ENTRY_DURATION = timedelta(minutes=30)
MAX_LOCAL_ENTRY_DURATION = timedelta(hours=10)
# Next project starts a few minutes after its commit (e.g. 09:06 commit → 09:10 block end).
PROJECT_HANDOFF_AFTER_COMMIT = timedelta(minutes=4)
MAX_WORK_AFTER_COMMIT = timedelta(hours=1, minutes=44)
# Same-project sessions only meet when commits are close in time.
CONTIGUOUS_COMMIT_GAP = timedelta(hours=3)

# (start, end, active) — active means not AFK
PresenceInterval = Tuple[datetime, datetime, bool]

# Window titles matching these count as clearly non-work (substring match, lowercased).
LEISURE_WINDOW_PATTERNS = (
    'youtube',
    'netflix',
    'twitch',
    'disney+',
    'hbo',
    'minecraft',
    'steam',
    'epic games',
    'spotify',
    'reddit.com',
    'facebook.com',
    'instagram',
    'tiktok',
)
# Session is non-work when leisure dominates and project work signals are tiny.
NON_WORK_LEISURE_RATIO = 0.75
NON_WORK_MAX_WORK_RATIO = 0.15

_AW_WINDOW_LINE = re.compile(r'^(\d+)s\s*-\s*(.+)$', re.IGNORECASE)


@dataclass
class WorkAnchor:
    kind: str  # 'commit' | 'activitywatch'
    group: str
    start: datetime
    end: datetime
    title: str


def fetch_afk_presence_intervals(
    start_date: datetime, end_date: datetime,
) -> List[PresenceInterval]:
    events: List[dict] = []
    try:
        buckets = discover_buckets()
        for bucket_id, (kind, _meta) in buckets.items():
            if kind != 'afk':
                continue
            try:
                events.extend(fetch_bucket_events(bucket_id, start_date, end_date))
            except Exception:
                logger.exception("Failed to fetch AFK events from bucket %s", bucket_id)
    except Exception:
        logger.exception("Failed to discover ActivityWatch buckets")
        return []

    intervals: List[PresenceInterval] = []
    for event in events:
        status = (event.get('data') or {}).get('status')
        if status not in ('afk', 'not-afk'):
            continue
        start = parse_time(event['timestamp']).replace(tzinfo=None)
        duration = event.get('duration') or 0
        end = start + timedelta(seconds=duration)
        intervals.append((start, end, status == 'not-afk'))

    return _merge_presence_intervals(intervals)


def _work_keywords_for_group(group: Optional[str]) -> List[str]:
    from tracklater import settings

    aw = getattr(settings, 'ACTIVITYWATCH', {}) or {}
    if group and group in aw:
        return list(aw[group].get('KEYWORDS', []) or [])
    keywords: List[str] = []
    for cfg in aw.values():
        if isinstance(cfg, dict):
            keywords.extend(cfg.get('KEYWORDS', []) or [])
    return keywords


def _parse_aw_window_lines(text: str) -> List[Tuple[float, str]]:
    """Parse ``Ns - window title`` lines from an ActivityWatch session summary."""
    windows: List[Tuple[float, str]] = []
    for line in (text or '').split('\n'):
        line = line.strip()
        if not line:
            continue
        match = _AW_WINDOW_LINE.match(line)
        if match:
            windows.append((float(match.group(1)), match.group(2).strip()))
    return windows


def _window_title_is_leisure(title: str) -> bool:
    lower = title.lower()
    return any(pattern in lower for pattern in LEISURE_WINDOW_PATTERNS)


def _window_title_is_work(title: str, group: Optional[str]) -> bool:
    keywords = _work_keywords_for_group(group)
    if not keywords:
        return False
    lower = title.lower()
    return any(keyword.lower() in lower for keyword in keywords)


def _aw_entry_is_clearly_non_work(
    row: Entry,
    group: Optional[str] = None,
) -> bool:
    """
    True when parsed window time is almost all leisure with negligible work signals.

    Rows without a parseable window breakdown are treated as work (legacy rows/tests).
    """
    windows = _parse_aw_window_lines(row.text or '')
    if not windows:
        return False
    total = sum(seconds for seconds, _ in windows)
    if total <= 0:
        return False
    work_seconds = sum(
        seconds for seconds, title in windows
        if _window_title_is_work(title, group or row.group)
    )
    leisure_seconds = sum(
        seconds for seconds, title in windows
        if _window_title_is_leisure(title)
    )
    if leisure_seconds < total * NON_WORK_LEISURE_RATIO:
        return False
    if work_seconds > total * NON_WORK_MAX_WORK_RATIO:
        return False
    return True


def _aw_interval_counts_as_work(row: Entry, group: Optional[str]) -> bool:
    if not row.end_time:
        return False
    return not _aw_entry_is_clearly_non_work(row, group)


def intervals_from_activitywatch_entries(
    rows: List[Entry],
) -> List[PresenceInterval]:
    out: List[PresenceInterval] = []
    for row in rows:
        if not row.end_time:
            continue
        active = _aw_interval_counts_as_work(row, row.group)
        out.append((row.start_time, row.end_time, active))
    return out


def _merge_presence_intervals(
    intervals: List[PresenceInterval],
) -> List[PresenceInterval]:
    if not intervals:
        return []
    intervals = sorted(intervals, key=lambda x: x[0])
    merged: List[PresenceInterval] = [intervals[0]]
    for start, end, active in intervals[1:]:
        last_start, last_end, last_active = merged[-1]
        if active == last_active and start <= last_end:
            merged[-1] = (last_start, max(last_end, end), last_active)
        else:
            merged.append((start, end, active))
    return merged


def infer_work_window_before_commit(
    commit_time: datetime,
    presence: List[PresenceInterval],
) -> Tuple[datetime, datetime]:
    earliest = commit_time - MAX_WORK_BEFORE_COMMIT
    default_start = commit_time - MIN_WORK_BEFORE_COMMIT

    active_near_commit: List[Tuple[datetime, datetime]] = []
    for start, end, active in presence:
        if not active:
            continue
        if end <= earliest or start > commit_time:
            continue
        seg_start = max(start, earliest)
        seg_end = min(end, commit_time)
        if seg_end > seg_start:
            active_near_commit.append((seg_start, seg_end))

    if not active_near_commit:
        return default_start, commit_time

    active_near_commit = _merge_time_ranges(active_near_commit)
    work_start = default_start
    for seg_start, seg_end in reversed(active_near_commit):
        if seg_end >= commit_time - timedelta(minutes=5):
            work_start = min(seg_start, default_start)
            work_start = max(work_start, earliest)
            break

    return work_start, commit_time


def _merge_time_ranges(
    ranges: List[Tuple[datetime, datetime]],
) -> List[Tuple[datetime, datetime]]:
    if not ranges:
        return []
    ranges = sorted(ranges)
    out = [ranges[0]]
    for start, end in ranges[1:]:
        prev_start, prev_end = out[-1]
        if start <= prev_end:
            out[-1] = (prev_start, max(prev_end, end))
        else:
            out.append((start, end))
    return out


def _merge_time_ranges_with_bridge(
    ranges: List[Tuple[datetime, datetime]],
    max_gap: timedelta,
) -> List[Tuple[datetime, datetime]]:
    """Merge overlapping ranges and ranges separated by at most max_gap."""
    merged = _merge_time_ranges(ranges)
    if not merged:
        return merged
    bridged: List[Tuple[datetime, datetime]] = [merged[0]]
    for start, end in merged[1:]:
        if start - bridged[-1][1] <= max_gap:
            bridged[-1] = (bridged[-1][0], max(bridged[-1][1], end))
        else:
            bridged.append((start, end))
    return bridged


def load_presence_intervals(
    start_date: datetime, end_date: datetime,
) -> List[PresenceInterval]:
    presence = fetch_afk_presence_intervals(start_date, end_date)
    aw_rows = Entry.query.filter(
        Entry.module == 'activitywatch',
        Entry.start_time >= start_date - MAX_WORK_BEFORE_COMMIT,
        Entry.start_time <= end_date,
    ).all()
    return _merge_presence_intervals(
        presence + intervals_from_activitywatch_entries(aw_rows)
    )


def build_commit_work_hints(
    start_date: datetime,
    end_date: datetime,
    presence: Optional[List[PresenceInterval]] = None,
) -> List[Dict[str, Any]]:
    git_rows = Entry.query.filter(
        Entry.module == 'gitmodule',
        Entry.start_time >= start_date,
        Entry.start_time <= end_date,
    ).order_by(Entry.start_time).all()

    if not git_rows:
        return []

    if presence is None:
        presence = load_presence_intervals(start_date, end_date)

    hints: List[Dict[str, Any]] = []
    for row in git_rows:
        commit_time = row.start_time
        work_start, work_end = infer_work_window_before_commit(commit_time, presence)
        summary = (row.text or row.title or '').split('\n')[0][:200]
        hints.append({
            'commit_time': commit_time.isoformat() + '+00:00',
            'suggested_work_start': work_start.isoformat() + '+00:00',
            'suggested_work_end': work_end.isoformat() + '+00:00',
            'group': row.group,
            'commit_summary': summary,
        })
    return hints


def _active_interval_near(
    presence: List[PresenceInterval], moment: datetime,
) -> Optional[Tuple[datetime, datetime]]:
    for start, end, active in presence:
        if not active:
            continue
        if start <= moment <= end:
            return (start, end)
    best: Optional[Tuple[datetime, datetime]] = None
    for start, end, active in presence:
        if not active:
            continue
        if end >= moment - timedelta(minutes=10) and start <= moment:
            if best is None or end > best[1]:
                best = (start, end)
    return best


def _active_union(
    t_start: datetime, t_end: datetime, presence: List[PresenceInterval],
) -> List[Tuple[datetime, datetime]]:
    parts: List[Tuple[datetime, datetime]] = []
    for start, end, active in presence:
        if not active:
            continue
        seg_start = max(start, t_start)
        seg_end = min(end, t_end)
        if seg_end > seg_start:
            parts.append((seg_start, seg_end))
    return _merge_time_ranges(parts)


def _window_for_commit(
    commit_time: datetime,
    work_start: datetime,
    presence: List[PresenceInterval],
    not_before: Optional[datetime] = None,
) -> Optional[Tuple[datetime, datetime]]:
    """1h window ending at/before commit, only inside active time."""
    active = _active_interval_near(presence, commit_time)
    if not active:
        entry_start = max(work_start, commit_time - MAX_WORK_BEFORE_COMMIT)
        if not_before:
            entry_start = max(entry_start, not_before)
        entry_end = commit_time
        if entry_end - entry_start < MIN_LOCAL_ENTRY_DURATION:
            entry_end = entry_start + MIN_LOCAL_ENTRY_DURATION
        return entry_start, entry_end

    a_start, a_end = active
    entry_start = max(work_start, a_start, commit_time - MAX_WORK_BEFORE_COMMIT)
    if not_before:
        entry_start = max(entry_start, not_before)
    entry_end = min(commit_time, a_end)

    if entry_end - entry_start < MIN_LOCAL_ENTRY_DURATION:
        need = MIN_LOCAL_ENTRY_DURATION - (entry_end - entry_start)
        back = min(need, entry_start - a_start)
        entry_start -= back
        need -= back
        if need > timedelta(0):
            entry_end = min(a_end, entry_end + need)

    if entry_end - entry_start < MIN_LOCAL_ENTRY_DURATION:
        return None
    return entry_start, entry_end


def _fallback_commit_bounds(
    commit_time: datetime,
    presence: List[PresenceInterval],
) -> Tuple[datetime, datetime]:
    """1h window ending at commit when active-time clipping fails."""
    work_start, _ = infer_work_window_before_commit(commit_time, presence)
    entry_end = commit_time
    entry_start = max(work_start, commit_time - MIN_LOCAL_ENTRY_DURATION)
    if entry_end - entry_start < MIN_LOCAL_ENTRY_DURATION:
        entry_start = commit_time - MIN_LOCAL_ENTRY_DURATION
    return entry_start, entry_end


def _title_from_commit_summary(summary: str) -> str:
    for line in (summary or '').split('\n'):
        line = line.strip()
        if ' - ' in line:
            parts = line.split(' - ', 1)
            if len(parts) > 1 and parts[1].strip():
                return parts[1].strip()[:60]
    first = (summary or '').split('\n')[0].strip()
    return first[:60] or 'Work'


def _collect_anchors(
    start_date: datetime, end_date: datetime, presence: List[PresenceInterval],
) -> List[WorkAnchor]:
    anchors: List[WorkAnchor] = []
    seen_commits = set()

    git_rows = Entry.query.filter(
        Entry.module == 'gitmodule',
        Entry.start_time >= start_date,
        Entry.start_time <= end_date,
    ).order_by(Entry.start_time).all()

    for row in git_rows:
        key = (row.start_time, row.group, (row.text or '')[:120])
        if key in seen_commits:
            continue
        seen_commits.add(key)
        summary = (row.text or row.title or '').split('\n')[0]
        anchors.append(WorkAnchor(
            kind='commit',
            group=row.group or '',
            start=row.start_time,
            end=row.start_time,
            title=_title_from_commit_summary(summary),
        ))

    aw_rows = Entry.query.filter(
        Entry.module == 'activitywatch',
        Entry.start_time >= start_date,
        Entry.start_time <= end_date,
    ).order_by(Entry.start_time).all()

    for row in aw_rows:
        if not row.end_time or not row.group:
            continue
        if row.end_time - row.start_time < MIN_WORK_BEFORE_COMMIT:
            continue
        title = (row.group or '').strip()
        if row.text:
            first = row.text.split('\n')[0].strip()
            if first and first != title:
                title = first[:60]
        anchors.append(WorkAnchor(
            kind='activitywatch',
            group=row.group,
            start=row.start_time,
            end=row.end_time,
            title=(title or 'Work')[:60],
        ))

    anchors.sort(key=lambda a: a.start)
    return anchors


def _anchor_bounds(
    anchor: WorkAnchor,
    presence: List[PresenceInterval],
    not_before: Optional[datetime] = None,
) -> Optional[Tuple[datetime, datetime]]:
    if anchor.kind == 'activitywatch':
        return anchor.start, anchor.end

    work_start, _ = infer_work_window_before_commit(anchor.start, presence)
    return _window_for_commit(anchor.start, work_start, presence, not_before=not_before)


def _anchors_fit_together(
    run: List[WorkAnchor], presence: List[PresenceInterval],
) -> bool:
    """Same project: only one entry if a single 1h window can cover all anchors in active time."""
    if len(run) <= 1:
        return True

    bounds = [_anchor_bounds(a, presence) for a in run]
    if any(b is None for b in bounds):
        return False

    needed_start = min(b[0] for b in bounds)  # type: ignore
    needed_end = max(b[1] for b in bounds)  # type: ignore

    if needed_end - needed_start > MIN_LOCAL_ENTRY_DURATION:
        return False

    span_start = min(a.start for a in run)
    span_end = max(a.end for a in run)
    active_parts = _active_union(span_start, span_end, presence)
    if not active_parts:
        return False

    for a_start, a_end in active_parts:
        if a_end - a_start < MIN_LOCAL_ENTRY_DURATION:
            continue
        slot_end = min(a_end, needed_end + MIN_LOCAL_ENTRY_DURATION)
        slot_start = max(a_start, slot_end - MIN_LOCAL_ENTRY_DURATION)
        if slot_start <= needed_start and slot_end >= needed_end:
            return True

    return False


def _entry_for_anchor_run(
    run: List[WorkAnchor],
    presence: List[PresenceInterval],
    allowed_projects: set,
    not_before: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    from tracklater.billing import default_project_pid

    group = run[0].group
    if not group:
        return None
    project = default_project_pid(group)
    if project not in allowed_projects:
        return None

    aw_anchors = [a for a in run if a.kind == 'activitywatch']
    commit_anchors = [a for a in run if a.kind == 'commit']
    if aw_anchors and (
        len(run) == 1 or all(a.kind == 'activitywatch' for a in run)
    ):
        aw = max(aw_anchors, key=lambda a: (a.end - a.start, a.end))
        if aw.end - aw.start >= MIN_LOCAL_ENTRY_DURATION:
            return {
                'start_time': aw.start,
                'end_time': aw.end,
                'title': run[-1].title,
                'project': project,
            }

    if aw_anchors and commit_anchors and len(run) > 1:
        aw = max(aw_anchors, key=lambda a: (a.end - a.start, a.end))
        if aw.end - aw.start >= MIN_LOCAL_ENTRY_DURATION:
            if all(aw.start <= a.start and aw.end >= a.end for a in commit_anchors):
                return {
                    'start_time': aw.start,
                    'end_time': aw.end,
                    'title': commit_anchors[-1].title,
                    'project': project,
                }

    if len(run) == 1:
        anchor = run[0]
        bounds = _anchor_bounds(anchor, presence, not_before=not_before)
        if not bounds and anchor.kind == 'commit':
            bounds = _fallback_commit_bounds(anchor.start, presence)
        if not bounds:
            return None
        entry_start, entry_end = bounds
        title = anchor.title
    else:
        bounds_list = [_anchor_bounds(a, presence) for a in run]
        if any(b is None for b in bounds_list):
            return None
        needed_start = min(b[0] for b in bounds_list)  # type: ignore
        needed_end = max(b[1] for b in bounds_list)  # type: ignore
        span_start = min(a.start for a in run)
        span_end = max(a.end for a in run)
        active_parts = _active_union(span_start, span_end, presence)
        entry_start, entry_end = None, None
        for a_start, a_end in active_parts:
            if a_end - a_start < MIN_LOCAL_ENTRY_DURATION:
                continue
            slot_end = min(a_end, needed_end + MIN_LOCAL_ENTRY_DURATION)
            slot_start = max(a_start, slot_end - MIN_LOCAL_ENTRY_DURATION)
            if slot_start <= needed_start and slot_end >= needed_end:
                entry_start, entry_end = slot_start, slot_end
                break
        if entry_start is None:
            return None
        title = run[-1].title

    result = {
        'start_time': entry_start,
        'end_time': entry_end,
        'title': title,
        'project': project,
    }
    if run[0].kind == 'commit':
        result['anchor_time'] = run[0].start
    return result


def _trim_overlaps_without_shifting(
    entries: List[Dict[str, Any]],
    presence: List[PresenceInterval],
) -> List[Dict[str, Any]]:
    """Drop invalid overlaps; never shorten entries below 1h or shift into gaps."""
    if not entries:
        return []
    entries = sorted(entries, key=lambda x: x['start_time'])
    result: List[Dict[str, Any]] = []

    for entry in entries:
        current = dict(entry)
        anchor_time = current.pop('anchor_time', None)
        if result and current['start_time'] < result[-1]['end_time']:
            current['start_time'] = max(current['start_time'], result[-1]['end_time'])
            if (current['end_time'] - current['start_time']) < MIN_LOCAL_ENTRY_DURATION:
                if anchor_time is not None:
                    current['end_time'] = anchor_time
                    current['start_time'] = anchor_time - MIN_LOCAL_ENTRY_DURATION
                    if current['start_time'] < result[-1]['end_time']:
                        current['start_time'] = result[-1]['end_time']
                if (current['end_time'] - current['start_time']) < MIN_LOCAL_ENTRY_DURATION:
                    continue

        if (current['end_time'] - current['start_time']) >= MIN_LOCAL_ENTRY_DURATION:
            result.append(current)

    return result


def resolve_overlapping_entries(
    entries: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Legacy name — use trim without time shifting."""
    return entries


def _first_aw_start_before(
    commit_time: datetime,
    group: str,
    aw_rows: List[Entry],
) -> Optional[datetime]:
    lookback = commit_time - MAX_WORK_BEFORE_COMMIT
    earliest: Optional[datetime] = None
    for row in aw_rows:
        if not row.end_time or row.end_time < lookback or row.start_time > commit_time:
            continue
        if group and row.group and row.group != group:
            continue
        if not _aw_interval_counts_as_work(row, group):
            continue
        if earliest is None or row.start_time < earliest:
            earliest = row.start_time
    return earliest


def _last_activity_end_after(
    from_time: datetime,
    presence: List[PresenceInterval],
    aw_rows: List[Entry],
    horizon: datetime,
    group: Optional[str] = None,
) -> datetime:
    end = from_time
    for start, seg_end, active in presence:
        if not active or seg_end <= from_time or start > horizon:
            continue
        end = max(end, min(seg_end, horizon))
    for row in aw_rows:
        if not row.end_time or row.start_time > horizon or row.end_time <= from_time:
            continue
        if group and row.group and row.group != group:
            continue
        if not _aw_interval_counts_as_work(row, group):
            continue
        end = max(end, min(row.end_time, horizon))
    return end


def _activity_intervals_until(
    from_time: datetime,
    presence: List[PresenceInterval],
    aw_rows: List[Entry],
    horizon: datetime,
    group: Optional[str] = None,
) -> List[Tuple[datetime, datetime]]:
    intervals: List[Tuple[datetime, datetime]] = []
    for start, seg_end, active in presence:
        if not active or seg_end <= from_time or start > horizon:
            continue
        intervals.append((max(start, from_time), min(seg_end, horizon)))
    for row in aw_rows:
        if not row.end_time or row.start_time > horizon or row.end_time <= from_time:
            continue
        if group and row.group and row.group != group:
            continue
        if not _aw_interval_counts_as_work(row, group):
            continue
        intervals.append((
            max(row.start_time, from_time),
            min(row.end_time, horizon),
        ))
    return _merge_time_ranges(intervals)


def _last_activity_end_until_afk(
    from_time: datetime,
    presence: List[PresenceInterval],
    aw_rows: List[Entry],
    horizon: datetime,
    group: Optional[str] = None,
    afk_gap: timedelta = MIN_WORK_BEFORE_COMMIT,
) -> datetime:
    """Last activity end in the contiguous work stretch after from_time (stop at AFK gap)."""
    intervals = _activity_intervals_until(
        from_time, presence, aw_rows, horizon, group=group,
    )
    if not intervals:
        return from_time
    chain_end = intervals[0][1]
    for start, end in intervals[1:]:
        if start - chain_end > afk_gap:
            break
        chain_end = max(chain_end, end)
    return chain_end


def _group_active_intervals(
    aw_rows: List[Entry],
    group: str,
    t_start: datetime,
    t_end: datetime,
) -> List[Tuple[datetime, datetime]]:
    intervals: List[Tuple[datetime, datetime]] = []
    for row in aw_rows:
        if not row.end_time or row.group != group:
            continue
        if not _aw_interval_counts_as_work(row, group):
            continue
        seg_start = max(row.start_time, t_start)
        seg_end = min(row.end_time, t_end)
        if seg_end > seg_start:
            intervals.append((seg_start, seg_end))
    return _merge_time_ranges(intervals)


def _evidence_intervals(
    commit_times: List[datetime],
    group: str,
    aw_rows: List[Entry],
    t_start: datetime,
    t_end: datetime,
    pad: timedelta = MIN_WORK_BEFORE_COMMIT,
) -> List[Tuple[datetime, datetime]]:
    """Group activity and commits, each padded by pad; merged."""
    intervals = _group_active_intervals(aw_rows, group, t_start, t_end)
    for commit_time in commit_times:
        if commit_time < t_start - pad or commit_time > t_end + pad:
            continue
        intervals.append((
            commit_time - pad,
            commit_time + max(pad, MIN_WORK_AFTER_COMMIT),
        ))
    return _merge_time_ranges_with_bridge(intervals, pad)


def _gaps_not_covered(
    range_start: datetime,
    range_end: datetime,
    covered: List[Tuple[datetime, datetime]],
) -> List[Tuple[datetime, datetime]]:
    """Sub-intervals of [range_start, range_end] not overlapping covered ranges."""
    if range_end <= range_start:
        return []
    if not covered:
        return [(range_start, range_end)]
    gaps: List[Tuple[datetime, datetime]] = []
    cursor = range_start
    for start, end in _merge_time_ranges(covered):
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < range_end:
        gaps.append((cursor, range_end))
    return gaps


@dataclass
class InactiveSpanViolation:
    project: str
    entry_start: datetime
    entry_end: datetime
    gap_start: datetime
    gap_end: datetime


CommitTimeGroup = Tuple[datetime, str]


def inactive_span_violations(
    entries: List[Dict[str, Any]],
    aw_rows: List[Entry],
    commits: List[CommitTimeGroup],
    max_inactive_gap: timedelta = MIN_WORK_BEFORE_COMMIT,
) -> List[InactiveSpanViolation]:
    """
    Find entry ranges that extend more than max_inactive_gap beyond activity/commits.

    Only same-group commits count as evidence. The last commit in the dataset may
    have a post-commit tail up to MAX_WORK_AFTER_COMMIT.
    """
    violations: List[InactiveSpanViolation] = []

    for entry in entries:
        start = entry['start_time']
        end = entry['end_time']
        group = entry['project'].split(':', 1)[0]
        commits_in_entry = [
            t for t, g in commits
            if start <= t <= end and g == group
        ]
        for t, g in commits:
            if g == group or not (start <= t <= end):
                continue
            switch_end = t - MIN_WORK_BEFORE_COMMIT
            if abs(end - switch_end) <= timedelta(seconds=2):
                commits_in_entry.append(t)
        evidence = _evidence_intervals(
            commits_in_entry, group, aw_rows, start, end,
        )
        anchor = entry.get('anchor_time')
        for gap_start, gap_end in _gaps_not_covered(start, end, evidence):
            if gap_end - gap_start <= max_inactive_gap:
                continue
            tail_allowed = False
            for commit_time in commits_in_entry:
                if gap_start >= commit_time and gap_end <= (
                    commit_time + MAX_WORK_AFTER_COMMIT + timedelta(seconds=1)
                ):
                    tail_allowed = True
                    break
            if tail_allowed:
                continue
            violations.append(InactiveSpanViolation(
                project=entry['project'],
                entry_start=start,
                entry_end=end,
                gap_start=gap_start,
                gap_end=gap_end,
            ))
    return violations


def assert_no_inactive_span_over(
    entries: List[Dict[str, Any]],
    aw_rows: List[Entry],
    commits: List[CommitTimeGroup],
    max_inactive_gap: timedelta = MIN_WORK_BEFORE_COMMIT,
) -> None:
    violations = inactive_span_violations(
        entries, aw_rows, commits, max_inactive_gap=max_inactive_gap,
    )
    if not violations:
        return
    lines = [
        'Entry extends >{} beyond activity/commits:'.format(max_inactive_gap),
    ]
    for v in violations[:10]:
        lines.append(
            '  {} {}–{} inactive {}–{} ({})'.format(
                v.project,
                v.entry_start,
                v.entry_end,
                v.gap_start,
                v.gap_end,
                v.gap_end - v.gap_start,
            )
        )
    if len(violations) > 10:
        lines.append('  … and {} more'.format(len(violations) - 10))
    raise AssertionError('\n'.join(lines))


# vis.js timeline ranges use an exclusive end; match that in tests.
TIMELINE_EXCLUSIVE_END_PADDING = timedelta(seconds=1)


def commits_outside_entries(
    entries: List[Dict[str, Any]],
    commits: List[CommitTimeGroup],
    tolerance: timedelta = timedelta(seconds=2),
    exclusive_end_padding: timedelta = timedelta(0),
) -> List[Tuple[datetime, str, str]]:
    """Return (commit_time, group, project) for commits not inside any same-project entry."""
    from tracklater.billing import default_project_pid

    uncovered: List[Tuple[datetime, str, str]] = []
    for commit_time, group in commits:
        project = default_project_pid(group)
        if not project:
            continue
        inside = False
        for entry in entries:
            if entry['project'] != project:
                continue
            effective_end = entry['end_time'] + exclusive_end_padding
            if (
                entry['start_time'] <= commit_time + tolerance
                and effective_end >= commit_time - tolerance
            ):
                inside = True
                break
        if not inside:
            uncovered.append((commit_time, group, project))
    return uncovered


def assert_commits_inside_entries(
    entries: List[Dict[str, Any]],
    commits: List[CommitTimeGroup],
    tolerance: timedelta = timedelta(seconds=2),
    exclusive_end_padding: timedelta = TIMELINE_EXCLUSIVE_END_PADDING,
) -> None:
    """Each git commit must fall within a same-project local entry (vis exclusive end)."""
    uncovered = commits_outside_entries(
        entries, commits, tolerance=tolerance,
        exclusive_end_padding=exclusive_end_padding,
    )
    if not uncovered:
        return
    lines = ['Git commit outside all local entries:']
    for commit_time, group, project in uncovered[:10]:
        lines.append('  {} {} ({})'.format(project, commit_time, group))
    if len(uncovered) > 10:
        lines.append('  … and {} more'.format(len(uncovered) - 10))
    raise AssertionError('\n'.join(lines))


def commits_in_same_project_gaps(
    entries: List[Dict[str, Any]],
    commits: List[CommitTimeGroup],
    tolerance: timedelta = timedelta(seconds=2),
) -> List[Tuple[datetime, str, str, datetime, datetime]]:
    """
    Commits that fall strictly between two same-project entries (visible as orphans).

    Returns (commit_time, group, project, gap_start, gap_end).
    """
    from tracklater.billing import default_project_pid

    in_gap: List[Tuple[datetime, str, str, datetime, datetime]] = []
    by_project: Dict[str, List[Dict[str, Any]]] = {}
    for entry in entries:
        by_project.setdefault(entry['project'], []).append(entry)
    for project, project_entries in by_project.items():
        ordered = sorted(project_entries, key=lambda e: e['start_time'])
        for index in range(len(ordered) - 1):
            left = ordered[index]
            right = ordered[index + 1]
            gap_start = left['end_time']
            gap_end = right['start_time']
            if gap_end <= gap_start + tolerance:
                continue
            for commit_time, group in commits:
                if default_project_pid(group) != project:
                    continue
                if gap_start + tolerance < commit_time < gap_end - tolerance:
                    in_gap.append((
                        commit_time, group, project, gap_start, gap_end,
                    ))
    return in_gap


def assert_no_commits_in_same_project_gaps(
    entries: List[Dict[str, Any]],
    commits: List[CommitTimeGroup],
    tolerance: timedelta = timedelta(seconds=2),
) -> None:
    """No commit may sit in empty space between two blocks of the same project."""
    in_gap = commits_in_same_project_gaps(
        entries, commits, tolerance=tolerance,
    )
    if not in_gap:
        return
    lines = ['Git commit in gap between same-project entries:']
    for commit_time, group, project, gap_start, gap_end in in_gap[:10]:
        lines.append(
            '  {} {} ({}) in {}–{} (between blocks)'.format(
                project, commit_time, group, gap_start, gap_end,
            )
        )
    if len(in_gap) > 10:
        lines.append('  … and {} more'.format(len(in_gap) - 10))
    raise AssertionError('\n'.join(lines))


def overlapping_entry_pairs(
    entries: List[Dict[str, Any]],
) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Return consecutive entry pairs whose time ranges overlap."""
    ordered = sorted(entries, key=lambda x: x['start_time'])
    pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for index in range(1, len(ordered)):
        prev_entry = ordered[index - 1]
        entry = ordered[index]
        if entry['start_time'] < prev_entry['end_time']:
            pairs.append((prev_entry, entry))
    return pairs


def assert_pre_commit_windows(
    entries: List[Dict[str, Any]],
    commits: List[CommitTimeGroup],
    tolerance: timedelta = timedelta(seconds=2),
) -> None:
    """Each commit has a same-project entry spanning 30 min before through commit time."""
    from tracklater.billing import default_project_pid

    failures: List[str] = []
    for index, (commit_time, group) in enumerate(commits):
        project = default_project_pid(group)
        if not project:
            continue
        need_start = commit_time - MIN_WORK_BEFORE_COMMIT
        prev_same_commit: Optional[datetime] = None
        for prev_time, prev_group in reversed(commits[:index]):
            if prev_group == group:
                prev_same_commit = prev_time
                break
        covering = []
        for e in entries:
            if e['project'] != project:
                continue
            if e['end_time'] < commit_time - tolerance:
                continue
            if e['start_time'] > commit_time + tolerance:
                continue
            anchor = e.get('anchor_time')
            anchor_matches = (
                not anchor
                or abs((anchor - commit_time).total_seconds())
                <= tolerance.total_seconds()
            )
            ends_at_commit = (
                abs((e['end_time'] - commit_time).total_seconds())
                <= tolerance.total_seconds()
            )
            merged_session = (
                e['start_time'] <= need_start + tolerance
                and e['end_time'] >= commit_time - tolerance
            )
            if (
                anchor
                and not anchor_matches
                and not ends_at_commit
                and not merged_session
            ):
                continue
            span = e['end_time'] - e['start_time']
            if span < MIN_LOCAL_ENTRY_DURATION - tolerance:
                continue
            full_pre = e['start_time'] <= need_start + tolerance
            short_pre = (
                e['start_time'] > need_start + tolerance
                and (commit_time - e['start_time']) >= (
                    MIN_LOCAL_ENTRY_DURATION - tolerance
                )
            )
            handoff_short_pre = (
                anchor_matches
                and e['start_time'] > need_start + tolerance
                and e['end_time'] >= commit_time - tolerance
                and span >= MIN_LOCAL_ENTRY_DURATION - tolerance
            )
            if full_pre or short_pre or handoff_short_pre or merged_session:
                covering.append(e)
        if not covering and prev_same_commit is not None:
            gap = commit_time - prev_same_commit
            if gap <= MIN_WORK_BEFORE_COMMIT * 2:
                for e in entries:
                    if e['project'] != project:
                        continue
                    if e['end_time'] < commit_time - tolerance:
                        continue
                    if e['start_time'] > commit_time + tolerance:
                        continue
                    span = e['end_time'] - e['start_time']
                    if span < MIN_LOCAL_ENTRY_DURATION - tolerance:
                        continue
                    if (
                        e['start_time'] <= prev_same_commit + tolerance
                        and e['end_time'] >= commit_time - tolerance
                    ):
                        covering.append(e)
                        break
        if not covering:
            failures.append(
                '  {} commit {} (need block from {} through commit)'.format(
                    project, commit_time, need_start,
                )
            )
    if failures:
        raise AssertionError(
            'Missing 30-minute pre-commit window:\n' + '\n'.join(failures[:10])
        )


def assert_no_overlapping_entries(entries: List[Dict[str, Any]]) -> None:
    pairs = overlapping_entry_pairs(entries)
    if not pairs:
        return
    lines = ['Local entries must not overlap:']
    for left, right in pairs[:10]:
        overlap_end = min(left['end_time'], right['end_time'])
        lines.append(
            '  {} {}–{} overlaps {} {}–{} ({}–{})'.format(
                left['project'],
                left['start_time'],
                left['end_time'],
                right['project'],
                right['start_time'],
                right['end_time'],
                right['start_time'],
                overlap_end,
            )
        )
    if len(pairs) > 10:
        lines.append('  … and {} more'.format(len(pairs) - 10))
    raise AssertionError('\n'.join(lines))


def _segments_within_evidence(
    entry_start: datetime,
    entry_end: datetime,
    commit_times: List[datetime],
    group: str,
    aw_rows: List[Entry],
) -> List[Tuple[datetime, datetime]]:
    """Parts of [entry_start, entry_end] that stay within pad of activity or commits."""
    evidence = _evidence_intervals(
        commit_times, group, aw_rows, entry_start, entry_end,
    )
    segments: List[Tuple[datetime, datetime]] = []
    for ev_start, ev_end in evidence:
        seg_start = max(entry_start, ev_start)
        seg_end = min(entry_end, ev_end)
        if seg_end - seg_start >= MIN_LOCAL_ENTRY_DURATION:
            segments.append((seg_start, seg_end))
    return segments


def _ensure_pre_commit_window(
    segments: List[Tuple[datetime, datetime]],
    commit_time: datetime,
    entry_start: datetime,
    entry_end: datetime,
) -> List[Tuple[datetime, datetime]]:
    """Guarantee the 30-minute window before commit_time is included when in range."""
    pre_start = max(entry_start, commit_time - MIN_WORK_BEFORE_COMMIT)
    pre_end = min(entry_end, commit_time)
    if pre_end - pre_start < MIN_LOCAL_ENTRY_DURATION:
        pre_start = commit_time - MIN_WORK_BEFORE_COMMIT
        pre_end = max(pre_end, commit_time)
    if pre_end - pre_start < MIN_LOCAL_ENTRY_DURATION:
        return segments
    merged = _merge_time_ranges(segments + [(pre_start, pre_end)])
    result: List[Tuple[datetime, datetime]] = []
    for seg_start, seg_end in merged:
        clip_start = max(seg_start, entry_start)
        clip_end = min(seg_end, entry_end)
        if clip_end - clip_start >= MIN_LOCAL_ENTRY_DURATION:
            result.append((clip_start, clip_end))
    return result


def _cap_entry_start_for_pre_commit(
    entry_start: datetime,
    commit_time: datetime,
    not_before: Optional[datetime] = None,
) -> datetime:
    """Start at or before commit_time - 30min; honor not_before when it is later."""
    pre_start = commit_time - MIN_WORK_BEFORE_COMMIT
    entry_start = min(entry_start, pre_start)
    if not_before is not None:
        entry_start = max(entry_start, not_before)
    return entry_start


def _prev_processed_commit(
    commits: List[WorkAnchor],
    index: int,
    allowed_projects: set,
) -> Optional[WorkAnchor]:
    """Previous commit that actually produces a local entry."""
    from tracklater.billing import default_project_pid

    for j in range(index - 1, -1, -1):
        candidate = commits[j]
        if not candidate.group:
            continue
        if default_project_pid(candidate.group) not in allowed_projects:
            continue
        return candidate
    return None


def _incoming_pre_commit_line(
    commit_index: int,
    commits: List[WorkAnchor],
) -> datetime:
    """Pre-commit start; moves forward when other projects committed in that window."""
    commit = commits[commit_index]
    line = commit.start - MIN_WORK_BEFORE_COMMIT
    for j in range(commit_index - 1, -1, -1):
        other = commits[j]
        if other.group == commit.group:
            continue
        if other.start >= commit.start:
            continue
        if other.start > line:
            line = other.start
    return line


def _incoming_after_handoff(
    previous_entry: Dict[str, Any],
    commit_time: datetime,
    pre_commit_start: datetime,
) -> datetime:
    """
    Hand off to the incoming project without overlapping the outgoing block.

    Trim the outgoing block to the pre-commit line so the incoming project can
    use a full 30-minute window when possible.
    """
    handoff_line = pre_commit_start
    if previous_entry['end_time'] > handoff_line:
        previous_entry['end_time'] = handoff_line
    incoming_start = max(previous_entry['end_time'], handoff_line)
    return _cap_entry_start_for_pre_commit(incoming_start, commit_time)


def _session_start_for_commit(
    commit: WorkAnchor,
    presence: List[PresenceInterval],
    aw_rows: List[Entry],
    not_before: Optional[datetime] = None,
) -> datetime:
    work_start, _ = infer_work_window_before_commit(commit.start, presence)
    aw_start = _first_aw_start_before(commit.start, commit.group, aw_rows)
    entry_start = aw_start if aw_start else work_start
    entry_start = max(entry_start, commit.start - MAX_WORK_BEFORE_COMMIT)
    if not_before:
        entry_start = max(entry_start, not_before)
    return entry_start


def _post_commit_end(
    commit: WorkAnchor,
    presence: List[PresenceInterval],
    aw_rows: List[Entry],
    horizon: datetime,
) -> datetime:
    """
    End of work after a commit: at least MIN_WORK_AFTER_COMMIT, then until activity stops.
    """
    floor = commit.start + MIN_WORK_AFTER_COMMIT
    last_active = _last_activity_end_until_afk(
        commit.start, presence, aw_rows, horizon, group=commit.group,
    )
    if last_active <= commit.start:
        end = min(horizon, floor)
    else:
        end = min(horizon, max(floor, last_active))
    if (
        horizon > commit.start
        and horizon - end <= MIN_WORK_BEFORE_COMMIT
    ):
        end = horizon
    return end


def _session_end_for_commit(
    commit: WorkAnchor,
    next_commit: Optional[WorkAnchor],
    presence: List[PresenceInterval],
    aw_rows: List[Entry],
) -> datetime:
    max_tail = commit.start + MAX_WORK_AFTER_COMMIT
    if next_commit is not None:
        if next_commit.group == commit.group:
            gap = next_commit.start - commit.start
            if gap <= MIN_WORK_BEFORE_COMMIT * 2:
                return next_commit.start
            horizon = min(next_commit.start, max_tail)
            if next_commit.start - max_tail <= MIN_WORK_BEFORE_COMMIT:
                horizon = next_commit.start
            return _post_commit_end(commit, presence, aw_rows, horizon)
        horizon = min(next_commit.start, max_tail)
        return _post_commit_end(commit, presence, aw_rows, horizon)
    return _post_commit_end(commit, presence, aw_rows, max_tail)


def _build_commit_session_entries(
    commit_anchors: List[WorkAnchor],
    presence: List[PresenceInterval],
    aw_rows: List[Entry],
    allowed_projects: set,
) -> List[Dict[str, Any]]:
    """
    One billing block per commit, snapped together.

    - Different project on next commit: end at next commit + handoff (~09:10).
    - Same project on next commit: end when next commit starts (contiguous).
    - After each commit: at least 15 minutes, then until activity stops (up to ~1h45 cap).
    """
    from tracklater.billing import default_project_pid

    commits = sorted(commit_anchors, key=lambda a: a.start)
    entries: List[Dict[str, Any]] = []

    for index, commit in enumerate(commits):
        if not commit.group:
            continue
        project = default_project_pid(commit.group)
        if project not in allowed_projects:
            continue

        next_commit = commits[index + 1] if index + 1 < len(commits) else None
        prev_commit = _prev_processed_commit(commits, index, allowed_projects)
        prev_group = prev_commit.group if prev_commit else None
        pre_commit_start = commit.start - MIN_WORK_BEFORE_COMMIT
        split_after_extended_tail = False
        if prev_group and prev_group != commit.group:
            pre_commit_start = _incoming_pre_commit_line(index, commits)
        if entries and prev_group == commit.group:
            prev_anchor = entries[-1].get('anchor_time', entries[-1]['end_time'])
            if commit.start <= entries[-1]['end_time']:
                if commit.start - prev_anchor < MIN_WORK_BEFORE_COMMIT:
                    entries[-1]['end_time'] = max(
                        entries[-1]['end_time'],
                        _session_end_for_commit(
                            commit, next_commit, presence, aw_rows,
                        ),
                    )
                    entries[-1]['anchor_time'] = commit.start
                    entries[-1]['title'] = commit.title
                    continue
                entries[-1]['end_time'] = min(
                    entries[-1]['end_time'], commit.start,
                )
                split_after_extended_tail = True
            gap = commit.start - prev_anchor
            if timedelta(0) <= gap < MIN_WORK_BEFORE_COMMIT:
                entries[-1]['end_time'] = max(
                    entries[-1]['end_time'],
                    _session_end_for_commit(
                        commit, next_commit, presence, aw_rows,
                    ),
                )
                entries[-1]['anchor_time'] = commit.start
                entries[-1]['title'] = commit.title
                continue
        if prev_group and prev_group != commit.group and entries:
            entry_start = _incoming_after_handoff(
                entries[-1], commit.start, pre_commit_start,
            )
        else:
            not_before = (
                None if split_after_extended_tail
                else (entries[-1]['end_time'] if entries else None)
            )
            entry_start = _session_start_for_commit(
                commit, presence, aw_rows, not_before=not_before,
            )
            prev_anchor = _prev_processed_commit(commits, index, allowed_projects)
            if (
                not split_after_extended_tail
                and entries
                and prev_anchor
                and prev_anchor.group == commit.group
            ):
                gap = commit.start - entries[-1]['end_time']
                if gap <= CONTIGUOUS_COMMIT_GAP:
                    entry_start = max(
                        entry_start,
                        min(
                            entries[-1]['end_time'],
                            commit.start - MIN_WORK_BEFORE_COMMIT,
                        ),
                    )
            entry_start = _cap_entry_start_for_pre_commit(
                entry_start, commit.start, not_before=not_before,
            )
            if split_after_extended_tail:
                entry_start = pre_commit_start
                if entries:
                    entries[-1]['end_time'] = min(
                        entries[-1]['end_time'], entry_start,
                    )
            elif entries:
                pre_start = commit.start - MIN_WORK_BEFORE_COMMIT
                if entry_start > pre_start:
                    entry_start = pre_start
                    entries[-1]['end_time'] = min(
                        entries[-1]['end_time'], entry_start,
                    )
        entry_end = _session_end_for_commit(
            commit, next_commit, presence, aw_rows,
        )
        if entry_end <= entry_start:
            entry_end = entry_start + MIN_LOCAL_ENTRY_DURATION

        commit_times = [
            c.start for c in commits
            if entry_start <= c.start <= entry_end and c.group == commit.group
        ]
        if commit.start not in commit_times:
            commit_times.append(commit.start)
        segment_end = entry_end
        if next_commit is not None and next_commit.group != commit.group:
            segment_end = max(
                commit.start,
                min(
                    segment_end,
                    next_commit.start - MIN_WORK_BEFORE_COMMIT,
                ),
            )
        meta = {
            'title': commit.title,
            'project': project,
            'anchor_time': commit.start,
        }
        if next_commit is None:
            seg_start = pre_commit_start
            if (
                prev_commit
                and prev_commit.group == commit.group
                and commit.start - entries[-1]['end_time'] <= CONTIGUOUS_COMMIT_GAP
            ):
                seg_start = max(entry_start, entries[-1]['end_time'])
            segments = [(seg_start, entry_end)]
        else:
            bridged_to_next = (
                next_commit is not None
                and next_commit.group == commit.group
                and entry_end >= next_commit.start
                and (next_commit.start - entry_end) <= MIN_WORK_BEFORE_COMMIT
            )
            if bridged_to_next:
                segments = [(
                    min(entry_start, pre_commit_start),
                    entry_end,
                )]
            else:
                segments = _segments_within_evidence(
                    entry_start, segment_end, commit_times, commit.group, aw_rows,
                )
        segment_bounds_end = segment_end if next_commit is not None else entry_end
        segments = _ensure_pre_commit_window(
            segments, commit.start, entry_start, segment_bounds_end,
        )
        segments = _merge_time_ranges(segments)
        before_count = len(entries)
        added: List[Dict[str, Any]] = []
        for seg_start, seg_end in segments:
            added.extend(_session_chunks(seg_start, seg_end, meta))
        valid = [
            e for e in added
            if (e['end_time'] - e['start_time']) >= MIN_LOCAL_ENTRY_DURATION
        ]
        if valid:
            entries.extend(valid)
        else:
            fallback_start = min(entry_start, pre_commit_start)
            fallback_end = max(
                commit.start,
                fallback_start + MIN_LOCAL_ENTRY_DURATION,
            )
            if entry_end > fallback_start:
                fallback_end = min(fallback_end, entry_end)
            entries.extend(_session_chunks(
                fallback_start, fallback_end, meta,
            ))

    entries = _merge_overlapping_same_project_entries(entries)
    entries = _resolve_cross_project_overlaps(entries, commits)
    entries = _merge_overlapping_same_project_entries(entries)
    entries = _ensure_commits_inside_entries(entries, commits, allowed_projects)
    return _merge_overlapping_same_project_entries(entries)


def _project_group(project: str) -> str:
    return project.split(':', 1)[0]


def _latest_commit_for_group_before(
    commits: List[WorkAnchor],
    group: str,
    before: datetime,
) -> Optional[datetime]:
    latest: Optional[datetime] = None
    for commit in commits:
        if commit.group == group and commit.start < before:
            if latest is None or commit.start > latest:
                latest = commit.start
    return latest


def _ensure_commits_inside_entries(
    entries: List[Dict[str, Any]],
    commits: List[WorkAnchor],
    allowed_projects: set,
) -> List[Dict[str, Any]]:
    """Extend or add entries so every allowed-project commit lies inside one."""
    from tracklater.billing import default_project_pid

    commit_list: List[CommitTimeGroup] = [
        (c.start, c.group)
        for c in commits
        if c.group and default_project_pid(c.group) in allowed_projects
    ]
    for commit_time, group in commit_list:
        project = default_project_pid(group)
        if not project:
            continue
        if not commits_outside_entries(entries, [(commit_time, group)]):
            continue
        project_entries = sorted(
            [e for e in entries if e['project'] == project],
            key=lambda e: e['start_time'],
        )
        fixed = False
        for index, entry in enumerate(project_entries):
            if entry['start_time'] <= commit_time <= entry['end_time']:
                fixed = True
                break
            if (
                index + 1 < len(project_entries)
                and entry['end_time'] < commit_time < project_entries[index + 1]['start_time']
            ):
                entry['end_time'] = project_entries[index + 1]['start_time']
                fixed = True
                break
            if index == 0 and commit_time < entry['start_time']:
                entry['start_time'] = min(
                    entry['start_time'],
                    commit_time - MIN_WORK_BEFORE_COMMIT,
                )
                fixed = True
                break
            if index == len(project_entries) - 1 and commit_time > entry['end_time']:
                entry['end_time'] = max(entry['end_time'], commit_time)
                fixed = True
                break
        if fixed:
            continue
        start = commit_time - MIN_WORK_BEFORE_COMMIT
        end = max(commit_time, start + MIN_LOCAL_ENTRY_DURATION)
        entries.append({
            'start_time': start,
            'end_time': end,
            'title': 'Work session',
            'project': project,
            'anchor_time': commit_time,
        })
    entries = _bridge_same_project_gaps_with_commits(
        entries, commit_list,
    )
    return [
        e for e in entries
        if (e['end_time'] - e['start_time']) >= MIN_LOCAL_ENTRY_DURATION
    ]


def _bridge_same_project_gaps_with_commits(
    entries: List[Dict[str, Any]],
    commits: List[CommitTimeGroup],
    tolerance: timedelta = timedelta(seconds=2),
) -> List[Dict[str, Any]]:
    """Close gaps between same-project blocks when commits fall in the gap."""
    from tracklater.billing import default_project_pid

    by_project: Dict[str, List[Dict[str, Any]]] = {}
    for entry in entries:
        by_project.setdefault(entry['project'], []).append(entry)
    for project_entries in by_project.values():
        ordered = sorted(project_entries, key=lambda e: e['start_time'])
        for index in range(len(ordered) - 1):
            left = ordered[index]
            right = ordered[index + 1]
            gap_start = left['end_time']
            gap_end = right['start_time']
            if gap_end <= gap_start + tolerance:
                continue
            has_commit_in_gap = False
            for commit_time, group in commits:
                if default_project_pid(group) != left['project']:
                    continue
                if gap_start + tolerance < commit_time < gap_end - tolerance:
                    has_commit_in_gap = True
                    break
            if has_commit_in_gap:
                left['end_time'] = gap_end
    return entries


def _resolve_cross_project_overlaps(
    entries: List[Dict[str, Any]],
    commits: List[WorkAnchor],
) -> List[Dict[str, Any]]:
    """Later commit's pre-commit window wins; trim the other block."""
    if len(entries) < 2:
        return entries
    ordered = sorted(entries, key=lambda e: e['start_time'])
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            earlier = ordered[i]
            later = ordered[j]
            if earlier['project'] == later['project']:
                continue
            if later['start_time'] >= earlier['end_time']:
                continue
            earlier_anchor = earlier.get('anchor_time')
            later_anchor = later.get('anchor_time')
            if earlier_anchor and later_anchor:
                if later_anchor > earlier_anchor:
                    later_index = next(
                        (
                            i for i, c in enumerate(commits)
                            if c.start == later_anchor
                        ),
                        None,
                    )
                    if later_index is not None:
                        handoff = _incoming_pre_commit_line(later_index, commits)
                    else:
                        handoff = later_anchor - MIN_WORK_BEFORE_COMMIT
                    earlier_group = _project_group(earlier['project'])
                    keep_through = _latest_commit_for_group_before(
                        commits, earlier_group, later_anchor,
                    )
                    earlier['end_time'] = min(earlier['end_time'], handoff)
                    if keep_through is not None:
                        earlier['end_time'] = max(
                            earlier['end_time'], keep_through,
                        )
                    later['start_time'] = min(later['start_time'], handoff)
                    if keep_through is not None:
                        later['start_time'] = max(
                            later['start_time'], keep_through,
                        )
                    if earlier['end_time'] < earlier['start_time'] + MIN_LOCAL_ENTRY_DURATION:
                        earlier['start_time'] = (
                            earlier['end_time'] - MIN_LOCAL_ENTRY_DURATION
                        )
                elif earlier_anchor > later_anchor:
                    handoff = earlier_anchor - MIN_WORK_BEFORE_COMMIT
                    later['end_time'] = min(later['end_time'], handoff)
            if later['start_time'] < earlier['end_time']:
                later['start_time'] = earlier['end_time']
    return [
        e for e in ordered
        if (e['end_time'] - e['start_time']) >= MIN_LOCAL_ENTRY_DURATION
    ]


def _merge_overlapping_same_project_entries(
    entries: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge duplicate chunks; trim starts when different commits overlap."""
    if not entries:
        return entries
    ordered = sorted(entries, key=lambda x: x['start_time'])
    merged: List[Dict[str, Any]] = [dict(ordered[0])]
    for entry in ordered[1:]:
        prev = merged[-1]
        current = dict(entry)
        if (
            current['project'] == prev['project']
            and current['start_time'] == prev['start_time']
            and current['end_time'] == prev['end_time']
        ):
            continue
        if (
            current['project'] == prev['project']
            and current['start_time'] < prev['end_time']
        ):
            if current.get('anchor_time') == prev.get('anchor_time'):
                prev['end_time'] = max(prev['end_time'], current['end_time'])
                if current.get('anchor_time'):
                    prev['anchor_time'] = current['anchor_time']
                prev['title'] = current['title']
            else:
                anchor = current.get('anchor_time') or current['end_time']
                ideal_start = anchor - MIN_WORK_BEFORE_COMMIT
                if ideal_start >= prev['end_time']:
                    current['start_time'] = ideal_start
                else:
                    current['start_time'] = prev['end_time']
            if current['end_time'] - current['start_time'] >= MIN_LOCAL_ENTRY_DURATION:
                merged.append(current)
        else:
            if current['end_time'] - current['start_time'] >= MIN_LOCAL_ENTRY_DURATION:
                merged.append(current)
    return merged


def _session_chunks(
    entry_start: datetime,
    entry_end: datetime,
    meta: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Split a session into blocks of at most MAX_LOCAL_ENTRY_DURATION (min 30 min each)."""
    chunks: List[Dict[str, Any]] = []
    cursor = entry_start
    while cursor < entry_end:
        chunk_end = min(cursor + MAX_LOCAL_ENTRY_DURATION, entry_end)
        if chunk_end - cursor >= MIN_LOCAL_ENTRY_DURATION:
            chunks.append({
                'start_time': cursor,
                'end_time': chunk_end,
                'title': meta['title'],
                'project': meta['project'],
                'anchor_time': meta.get('anchor_time'),
            })
        cursor = chunk_end
    return chunks


def _merge_commit_entries_when_fit(
    entries: List[Dict[str, Any]],
    presence: List[PresenceInterval],
) -> List[Dict[str, Any]]:
    """Same project only: merge consecutive commit entries when one 1h window fits."""
    if len(entries) <= 1:
        return entries
    merged: List[Dict[str, Any]] = [dict(entries[0])]
    for entry in entries[1:]:
        if entry['project'] != merged[-1]['project']:
            merged.append(dict(entry))
            continue
        run = [
            WorkAnchor('commit', '', entry['anchor_time'], entry['anchor_time'], ''),
            WorkAnchor('commit', '', merged[-1]['anchor_time'], merged[-1]['anchor_time'], ''),
        ]
        if _anchors_fit_together(run, presence):
            bounds = [
                (merged[-1]['start_time'], merged[-1]['end_time']),
                (entry['start_time'], entry['end_time']),
            ]
            merged[-1]['start_time'] = min(b[0] for b in bounds)
            merged[-1]['end_time'] = max(b[1] for b in bounds)
            merged[-1]['title'] = entry['title']
            merged[-1]['anchor_time'] = entry['anchor_time']
        else:
            merged.append(dict(entry))
    return merged


def build_entries_from_commits(
    start_date: datetime,
    end_date: datetime,
    allowed_projects: set,
) -> List[Dict[str, Any]]:
    """
    Build local entries anchored to git commits and ActivityWatch sessions.

    - One session block per commit; times span real work until the next commit.
    - Different project on next commit: outdoor ends ~handoff after next commit (e.g. 09:10).
    - Same project on next commit: blocks meet at the next commit time.
    - After each commit: at least 15 minutes, then until AFK (up to ~1h45).
    """
    presence = load_presence_intervals(start_date, end_date)
    anchors = _collect_anchors(start_date, end_date, presence)

    if not anchors:
        return []

    commit_anchors = [a for a in anchors if a.kind == 'commit']
    aw_anchors = [a for a in anchors if a.kind == 'activitywatch']
    aw_rows = Entry.query.filter(
        Entry.module == 'activitywatch',
        Entry.start_time >= start_date - MAX_WORK_BEFORE_COMMIT,
        Entry.start_time <= end_date,
    ).all()

    if commit_anchors:
        entries = _build_commit_session_entries(
            commit_anchors, presence, aw_rows, allowed_projects,
        )
        return entries

    entries: List[Dict[str, Any]] = []
    for aw in aw_anchors:
        entry = _entry_for_anchor_run(
            [aw], presence, allowed_projects, not_before=None
        )
        if entry:
            entries.append(entry)

    entries.sort(key=lambda x: x['start_time'])
    return _trim_overlaps_without_shifting(entries, presence)
