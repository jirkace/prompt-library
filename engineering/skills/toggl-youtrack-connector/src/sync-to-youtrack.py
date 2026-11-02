#!/usr/bin/env python3
import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path


def resolve_secret(value: str) -> str:
    """Resolve op:// references via 1Password CLI; return plain values unchanged."""
    if not value or not value.startswith('op://'):
        return value
    result = subprocess.run(['op', 'read', value], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f'1Password lookup failed for {value!r}: {result.stderr.strip()}')
    return result.stdout.strip()


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description='Post Toggl time entries as YouTrack work items on the issues mapped '
                    'by sync-to-toggl.py. Already-synced entry IDs are skipped.',
        epilog='Without options the last 7 days are synced. Dates are UTC (YYYY-MM-DD). '
               'Mapping: ~/.youtrack-toggl-mapping.json',
    )
    p.add_argument('--days', type=int, default=7, metavar='N',
                   help='sync the last N days up to today (default: 7)')
    p.add_argument('--since', type=date.fromisoformat, metavar='YYYY-MM-DD',
                   help='start date (inclusive); overrides --days')
    p.add_argument('--until', type=date.fromisoformat, metavar='YYYY-MM-DD',
                   help='end date (inclusive, default: today); requires --since')
    args = p.parse_args(argv)
    if args.until and not args.since:
        p.error('--until requires --since')
    return args


# Parse CLI before resolving secrets so --help works without env/1Password
# and an unknown flag aborts before any API call.
ARGS = parse_args() if __name__ == '__main__' else None


YT_BASE = os.environ.get('YOUTRACK_BASE_URL', '').rstrip('/')
YT_TOKEN = resolve_secret(os.environ.get('YOUTRACK_TOKEN') or os.environ.get('YOUTRACK_LSDEVTOOLS_TOKEN', ''))
TOGGL_TOKEN = resolve_secret(os.environ.get('TOGGL_TOKEN', ''))
MAPPING_PATH = Path.home() / '.youtrack-toggl-mapping.json'

if not YT_BASE:
    print('YOUTRACK_BASE_URL not set', file=sys.stderr)
    sys.exit(1)
if not YT_TOKEN:
    print('YOUTRACK_TOKEN or YOUTRACK_LSDEVTOOLS_TOKEN not set (plain value or op:// reference)', file=sys.stderr)
    sys.exit(1)
if not TOGGL_TOKEN:
    print('TOGGL_TOKEN not set (plain value or op:// reference)', file=sys.stderr)
    sys.exit(1)

_toggl_auth = 'Basic ' + base64.b64encode(f'{TOGGL_TOKEN}:api_token'.encode()).decode()


def toggl_get(path, params=None):
    url = f'https://api.track.toggl.com/api/v9{path}'
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        'Authorization': _toggl_auth,
        'Content-Type': 'application/json',
    })
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Toggl GET {path}: {e.code} {e.read().decode()}')


def yt_post(path, body):
    req = urllib.request.Request(
        f'{YT_BASE}/api{path}',
        data=json.dumps(body).encode(),
        headers={
            'Authorization': f'Bearer {YT_TOKEN}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'YouTrack POST {path}: {e.code} {e.read().decode()}')


def load_mapping():
    if not MAPPING_PATH.exists():
        raise RuntimeError(
            f'Mapping file not found at {MAPPING_PATH}.\nRun sync-to-toggl first to build the mapping.'
        )
    return json.loads(MAPPING_PATH.read_text())


def save_mapping(mapping):
    # Write to a temp file and rename, so an interrupted save never leaves a truncated mapping.
    tmp = MAPPING_PATH.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(mapping, indent=2))
    os.replace(tmp, MAPPING_PATH)


def parse_date_range(args):
    today = date.today()
    if args.since:
        since, until = args.since, args.until or today
    else:
        since, until = today - timedelta(days=args.days), today
    return (f'{since.isoformat()}T00:00:00+00:00', f'{until.isoformat()}T23:59:59+00:00',
            since.isoformat(), until.isoformat())


def ms_timestamp(iso_str):
    dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
    return int(dt.timestamp() * 1000)


def main(args=None):
    args = args or ARGS or parse_args([])
    mapping = load_mapping()
    start_date, end_date, display_start, display_end = parse_date_range(args)

    project_to_issue = {
        data['toggl_project_id']: issue_id
        for issue_id, data in mapping['issues'].items()
    }

    print(f'Fetching Toggl entries: {display_start} → {display_end}')
    entries = toggl_get('/me/time_entries', {'start_date': start_date, 'end_date': end_date})

    synced_set = set(mapping['synced_entries'])
    to_sync = [
        e for e in entries
        if e['duration'] > 0
        and e.get('project_id') is not None
        and e['project_id'] in project_to_issue
        and e['id'] not in synced_set
    ]

    print(f'  {len(entries)} total, {len(to_sync)} to sync')
    if not to_sync:
        print('Nothing to sync.')
        return

    synced = failed = 0

    for entry in to_sync:
        issue_id = project_to_issue[entry['project_id']]
        minutes = entry['duration'] // 60
        if minutes < 1:
            print(f'  [SKIP] #{entry["id"]}: < 1 minute')
            continue
        description = entry.get('description') or '(no description)'
        print(f'  [SYNC] {issue_id} +{minutes}m "{description[:45]}" ... ', end='', flush=True)
        try:
            yt_post(f'/issues/{issue_id}/timeTracking/workItems', {
                'date': ms_timestamp(entry['start']),
                'duration': {'minutes': minutes},
                'text': f'Toggl #{entry["id"]}: {description}',
            })
            mapping['synced_entries'].append(entry['id'])
            # Persist after every post: an interrupted run must not re-post on the next run.
            save_mapping(mapping)
            print('done')
            synced += 1
        except Exception as e:
            print(f'FAILED: {e}')
            failed += 1

    save_mapping(mapping)
    print(f'\nDone: {synced} synced, {failed} failed')
    if failed > 0:
        sys.exit(1)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
