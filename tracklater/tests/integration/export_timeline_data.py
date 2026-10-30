#!/usr/bin/env python3
"""
Export today's ActivityWatch + git rows from TrackLater DB into timeline_data.json.

Usage:
    python3 tracklater/tests/integration/export_timeline_data.py
    python3 tracklater/tests/integration/export_timeline_data.py --date 2026-06-01
    python3 tracklater/tests/integration/export_timeline_data.py --from 2026-05-25 --to 2026-06-01 \\
        --out tracklater/tests/integration/last_week_timeline_data.json
"""
import argparse
import json
import os
import sqlite3
from datetime import date

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..')
)
DEFAULT_DB = os.path.join(REPO_ROOT, 'tracklater', 'database.db')
OUT_PATH = os.path.join(os.path.dirname(__file__), 'timeline_data.json')
TEXT_MAX = 1200


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default=None)
    parser.add_argument('--from', dest='from_date', default=None)
    parser.add_argument('--to', dest='to_date', default=None)
    parser.add_argument('--out', default=OUT_PATH)
    parser.add_argument('--db', default=DEFAULT_DB)
    parser.add_argument('--second-commit-group', default='metso')
    args = parser.parse_args()

    if args.from_date and args.to_date:
        source_from, source_to = args.from_date, args.to_date
        single_date = None
    else:
        single_date = args.date or date.today().isoformat()
        source_from = source_to = single_date

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    def rows(module):
        if single_date:
            return conn.execute(
                'SELECT id, start_time, end_time, "group", title, text FROM entries '
                'WHERE module = ? AND date(start_time) = ? ORDER BY start_time',
                (module, single_date),
            ).fetchall()
        return conn.execute(
            'SELECT id, start_time, end_time, "group", title, text FROM entries '
            'WHERE module = ? AND date(start_time) >= ? AND date(start_time) <= ? '
            'ORDER BY start_time',
            (module, source_from, source_to),
        ).fetchall()

    aw = [dict(r) for r in rows('activitywatch')]
    git = [dict(r) for r in rows('gitmodule')]

    for collection in (aw, git):
        for row in collection:
            for key in ('start_time', 'end_time'):
                if row.get(key):
                    row[key] = str(row[key]).split('.')[0]
            if row.get('text') and len(row['text']) > TEXT_MAX:
                row['text'] = row['text'][:TEXT_MAX] + '\n…'

    for i, row in enumerate(aw):
        row['id'] = row.get('id') or 'integration-aw-{:03d}'.format(i)
    for i, row in enumerate(git):
        row['id'] = row.get('id') or 'integration-git-{:d}'.format(i)

    if len(git) >= 2 and args.second_commit_group:
        git[1]['group'] = args.second_commit_group
        text = git[1].get('text') or ''
        lines = text.split('\n', 1)
        first = lines[0]
        if ' - ' in first:
            _, msg = first.split(' - ', 1)
            first = '{} - {}'.format(args.second_commit_group, msg)
        else:
            first = args.second_commit_group
        git[1]['text'] = first + ('\n' + lines[1] if len(lines) > 1 else '')

    payload = {
        'activitywatch': aw,
        'git_commits': git,
    }
    if single_date:
        payload['source_date'] = single_date
    else:
        payload['source_from'] = source_from
        payload['source_to'] = source_to

    with open(args.out, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)

    print('Wrote {} ({} AW, {} git)'.format(args.out, len(aw), len(git)))


if __name__ == '__main__':
    main()
