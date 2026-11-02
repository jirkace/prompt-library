#!/usr/bin/env python3
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
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
        description='Sync YouTrack issues where you are Taker to Toggl projects: unresolved '
                    '-> active project (created/restored), resolved -> archived. Also rotates '
                    'the monthly FSWEB service ticket and registers manually created '
                    '"[ISSUE-ID] ..." projects.',
        epilog='Env: YOUTRACK_BASE_URL, YOUTRACK_TOKEN|YOUTRACK_LSDEVTOOLS_TOKEN, TOGGL_TOKEN, '
               'YOUTRACK_TAKER_FIELD (default: Taker). Values may be op:// references. '
               'Mapping: ~/.youtrack-toggl-mapping.json',
    )
    return p.parse_args(argv)


# Parse CLI before resolving secrets so --help works without env/1Password
# and an unknown flag aborts before any API call.
if __name__ == '__main__':
    parse_args()


YT_BASE = os.environ.get('YOUTRACK_BASE_URL', '').rstrip('/')
YT_TOKEN = resolve_secret(os.environ.get('YOUTRACK_TOKEN') or os.environ.get('YOUTRACK_LSDEVTOOLS_TOKEN', ''))
TOGGL_TOKEN = resolve_secret(os.environ.get('TOGGL_TOKEN', ''))
TAKER_FIELD = os.environ.get('YOUTRACK_TAKER_FIELD', 'Taker')
MAPPING_PATH = Path.home() / '.youtrack-toggl-mapping.json'

if not YT_BASE:
    print('YOUTRACK_BASE_URL not set (e.g. https://youtrack.example.com)', file=sys.stderr)
    sys.exit(1)
if not YT_TOKEN:
    print('YOUTRACK_TOKEN or YOUTRACK_LSDEVTOOLS_TOKEN not set (plain value or op:// reference)', file=sys.stderr)
    sys.exit(1)
if not TOGGL_TOKEN:
    print('TOGGL_TOKEN not set (plain value or op:// reference)', file=sys.stderr)
    sys.exit(1)

_toggl_auth = 'Basic ' + base64.b64encode(f'{TOGGL_TOKEN}:api_token'.encode()).decode()


def yt_get(path, params=None):
    url = f'{YT_BASE}/api{path}'
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        'Authorization': f'Bearer {YT_TOKEN}',
        'Accept': 'application/json',
    })
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'YouTrack GET {path}: {e.code} {e.read().decode()}')


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


def toggl_post(path, body):
    req = urllib.request.Request(
        f'https://api.track.toggl.com/api/v9{path}',
        data=json.dumps(body).encode(),
        headers={'Authorization': _toggl_auth, 'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Toggl POST {path}: {e.code} {e.read().decode()}')


def toggl_put(path, body):
    req = urllib.request.Request(
        f'https://api.track.toggl.com/api/v9{path}',
        data=json.dumps(body).encode(),
        headers={'Authorization': _toggl_auth, 'Content-Type': 'application/json'},
        method='PUT',
    )
    try:
        with urllib.request.urlopen(req) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Toggl PUT {path}: {e.code} {e.read().decode()}')


def load_mapping():
    if not MAPPING_PATH.exists():
        return {'workspace_id': None, 'synced_entries': [], 'issues': {}}
    return json.loads(MAPPING_PATH.read_text())


def save_mapping(mapping):
    # Write to a temp file and rename, so an interrupted save never leaves a truncated mapping.
    tmp = MAPPING_PATH.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(mapping, indent=2))
    os.replace(tmp, MAPPING_PATH)


def main():
    mapping = load_mapping()

    print('Fetching Toggl workspace...')
    me = toggl_get('/me')
    workspace_id = me['default_workspace_id']
    mapping['workspace_id'] = workspace_id
    print(f'  Workspace ID: {workspace_id}')

    print('Fetching Toggl projects...')
    # The endpoint is paginated; without per_page/page it silently truncates the list.
    toggl_projects = []
    page = 1
    while True:
        batch = toggl_get(f'/workspaces/{workspace_id}/projects',
                          {'active': 'both', 'per_page': 200, 'page': page})
        toggl_projects.extend(batch)
        if len(batch) < 200:
            break
        page += 1
    projects_by_name = {p['name']: p for p in toggl_projects}
    projects_by_id = {p['id']: p for p in toggl_projects}
    print(f'  Found {len(toggl_projects)} projects')

    print(f'Fetching YouTrack issues ({TAKER_FIELD}: me)...')
    # Paginate with $skip/$top; a single $top request silently drops older issues.
    issues = []
    page_size = 500
    while True:
        batch = yt_get('/issues', {
            'query': f'{TAKER_FIELD}: me',
            'fields': 'id,idReadable,summary,resolved',
            '$top': str(page_size),
            '$skip': str(len(issues)),
        })
        issues.extend(batch)
        if len(batch) < page_size:
            break
    print(f'  Found {len(issues)} issues')

    created = archived = restored = skipped = 0

    for issue in issues:
        project_name = f'[{issue["idReadable"]}] {issue["summary"]}'[:255]
        is_resolved = issue['resolved'] is not None
        existing_mapping = mapping['issues'].get(issue['idReadable'])
        toggl_project = projects_by_id.get(existing_mapping['toggl_project_id']) if existing_mapping else None

        if toggl_project is None:
            toggl_project = projects_by_name.get(project_name)
        if toggl_project is None:
            prefix = f'[{issue["idReadable"]}]'
            toggl_project = next((p for p in toggl_projects if p['name'].startswith(prefix)), None)

        if not is_resolved:
            if toggl_project is None:
                print(f'  [CREATE] {issue["idReadable"]}: {issue["summary"][:50]} ... ', end='', flush=True)
                toggl_project = toggl_post(f'/workspaces/{workspace_id}/projects', {
                    'name': project_name, 'active': True, 'is_private': False,
                })
                print(f'#{toggl_project["id"]}')
                created += 1
            elif not toggl_project['active']:
                print(f'  [RESTORE] {issue["idReadable"]} → #{toggl_project["id"]} ... ', end='', flush=True)
                toggl_put(f'/workspaces/{workspace_id}/projects/{toggl_project["id"]}', {'active': True})
                print('done')
                toggl_project['active'] = True
                restored += 1
            else:
                print(f'  [OK] {issue["idReadable"]} → #{toggl_project["id"]}')
                skipped += 1
            mapping['issues'][issue['idReadable']] = {
                'toggl_project_id': toggl_project['id'],
                'name': project_name,
            }
        else:
            if toggl_project and toggl_project['active']:
                print(f'  [ARCHIVE] {issue["idReadable"]} → #{toggl_project["id"]} ... ', end='', flush=True)
                toggl_put(f'/workspaces/{workspace_id}/projects/{toggl_project["id"]}', {'active': False})
                print('done')
                archived += 1
                if existing_mapping:
                    mapping['issues'][issue['idReadable']] = {
                        **existing_mapping,
                        'toggl_project_id': toggl_project['id'],
                    }
            elif toggl_project:
                print(f'  [ARCHIVED] {issue["idReadable"]} → #{toggl_project["id"]}')
                skipped += 1
            else:
                print(f'  [SKIP] {issue["idReadable"]}: resolved, no Toggl project')
                skipped += 1

    # Sync rotating monthly service ticket (FSWEB project, tag: service-ticket)
    print('Fetching FSWEB service ticket (tag: service-ticket, unresolved)...')
    service_issues = yt_get('/issues', {
        'query': 'project: FSWEB tag: service-ticket #Unresolved',
        'fields': 'id,idReadable,summary,resolved',
        '$top': '1',
    })
    special = mapping.setdefault('special_tickets', {})
    cached = special.get('service_ticket')  # {'yt_id': '...', 'toggl_project_id': ...}

    current_service = service_issues[0] if service_issues else None

    if cached and (current_service is None or current_service['idReadable'] != cached['yt_id']):
        # Old ticket rotated out — archive its Toggl project if still active
        old_project = projects_by_id.get(cached.get('toggl_project_id'))
        if old_project and old_project['active']:
            print(f'  [ARCHIVE service] {cached["yt_id"]} → #{old_project["id"]} ... ', end='', flush=True)
            toggl_put(f'/workspaces/{workspace_id}/projects/{old_project["id"]}', {'active': False})
            print('done')
            archived += 1

    if current_service:
        project_name = f'[{current_service["idReadable"]}] {current_service["summary"]}'[:255]
        existing_id = cached.get('toggl_project_id') if cached and cached['yt_id'] == current_service['idReadable'] else None
        toggl_project = projects_by_id.get(existing_id) if existing_id else None
        if toggl_project is None:
            toggl_project = projects_by_name.get(project_name)
        if toggl_project is None:
            prefix = f'[{current_service["idReadable"]}]'
            toggl_project = next((p for p in toggl_projects if p['name'].startswith(prefix)), None)

        if toggl_project is None:
            print(f'  [CREATE service] {current_service["idReadable"]}: {current_service["summary"][:50]} ... ', end='', flush=True)
            toggl_project = toggl_post(f'/workspaces/{workspace_id}/projects', {
                'name': project_name, 'active': True, 'is_private': False,
            })
            print(f'#{toggl_project["id"]}')
            created += 1
        elif not toggl_project['active']:
            print(f'  [RESTORE service] {current_service["idReadable"]} → #{toggl_project["id"]} ... ', end='', flush=True)
            toggl_put(f'/workspaces/{workspace_id}/projects/{toggl_project["id"]}', {'active': True})
            print('done')
            restored += 1
        else:
            print(f'  [OK service] {current_service["idReadable"]} → #{toggl_project["id"]}')
            skipped += 1

        special['service_ticket'] = {'yt_id': current_service['idReadable'], 'toggl_project_id': toggl_project['id']}
    else:
        print('  No open FSWEB service ticket found.')

    # Register manually-created Toggl projects (code reviews, consultations, etc.)
    # that have [ISSUE-ID] in the name but aren't in the mapping yet.
    registered = 0
    issue_id_re = re.compile(r'^\[([A-Z]+-\d+)\]')
    for project in toggl_projects:
        m = issue_id_re.match(project['name'])
        if not m:
            continue
        issue_id = m.group(1)
        if issue_id in mapping['issues']:
            continue
        mapping['issues'][issue_id] = {
            'toggl_project_id': project['id'],
            'name': project['name'],
        }
        print(f'  [REGISTER] {issue_id} → #{project["id"]} (manually created)')
        registered += 1

    save_mapping(mapping)
    print(f'\nDone: {created} created, {archived} archived, {restored} restored, {skipped} unchanged, {registered} registered')
    print(f'Mapping saved → {MAPPING_PATH}')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
