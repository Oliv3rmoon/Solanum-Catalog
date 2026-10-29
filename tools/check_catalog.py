#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Checks catalog.json, the list of apps Solanum installs.

Offline (always): the schema, unique ids and title IDs, title IDs in the console's form (four
letters, five digits), a repository in owner/name form, and a license for every app.

Online (--online): for every app, the repository's latest release (not a draft or a
pre-release) has the app's ZIP and its checksum file, and the checksum file names that ZIP.
That is what Solanum itself needs before it offers an install. Set GITHUB_TOKEN for a higher
rate limit.
"""
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ('id', 'name', 'category', 'summary', 'title_id', 'repo', 'asset', 'folder', 'checksums',
            'attested', 'license')
CATEGORIES = {'emulator', 'media', 'streaming', 'game', 'tool', 'store'}
TITLE_ID = re.compile(r'^[A-Z]{4}[0-9]{5}$')
REPO = re.compile(r'^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$')


def fail(message):
    print(f'FAIL: {message}')
    sys.exit(1)


def check_offline(catalog):
    if catalog.get('schema') != 1:
        fail('schema must be 1')
    apps = catalog.get('apps')
    if not isinstance(apps, list) or not apps:
        fail('apps must be a non-empty list')
    seen_ids, seen_titles = set(), set()
    for app in apps:
        missing = [key for key in REQUIRED if key not in app]
        if missing:
            fail(f'{app.get("id", "?")}: missing {", ".join(missing)}')
        if app['id'] in seen_ids:
            fail(f'{app["id"]}: id listed twice')
        if app['title_id'] in seen_titles:
            fail(f'{app["id"]}: title ID {app["title_id"]} already belongs to another app')
        seen_ids.add(app['id'])
        seen_titles.add(app['title_id'])
        if not TITLE_ID.match(app['title_id']):
            fail(f'{app["id"]}: title ID {app["title_id"]} is not four letters and five digits')
        if not REPO.match(app['repo']):
            fail(f'{app["id"]}: repo {app["repo"]} is not owner/name')
        if app['category'] not in CATEGORIES:
            fail(f'{app["id"]}: category {app["category"]} is not one of {sorted(CATEGORIES)}')
        if not app['license'].strip():
            fail(f'{app["id"]}: no license')
    for key in ('tips', 'eggs'):
        lines = catalog.get(key, [])
        if not all(isinstance(line, str) and line.strip() for line in lines):
            fail(f'every entry of {key} must be text')
    return apps


def get(url):
    request = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json',
                                                   'User-Agent': 'solanum-catalog-check'})
    token = os.environ.get('GITHUB_TOKEN')
    if token:
        request.add_header('Authorization', f'Bearer {token}')
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def check_online(apps):
    for app in apps:
        release = json.loads(get(f'https://api.github.com/repos/{app["repo"]}/releases/latest'))
        tag = release['tag_name']
        version = tag[1:] if tag.startswith('v') else tag
        asset = app['asset'].replace('{version}', version)
        names = {a['name']: a['browser_download_url'] for a in release['assets']}
        if asset not in names:
            fail(f'{app["id"]}: {tag} has no {asset}')
        if app['checksums'] not in names:
            fail(f'{app["id"]}: {tag} has no {app["checksums"]}')
        sums = get(names[app['checksums']]).decode('utf-8', 'replace')
        if not re.search(rf'^[0-9a-f]{{64}}\s+\*?{re.escape(asset)}$', sums, re.M):
            fail(f'{app["id"]}: {app["checksums"]} of {tag} does not name {asset}')
        print(f'{app["id"]}: {tag} {asset} and {app["checksums"]} found')


def main():
    catalog = json.loads((ROOT / 'catalog.json').read_text())
    apps = check_offline(catalog)
    print(f'catalog: {len(apps)} apps, {len(catalog.get("tips", []))} tips, {len(catalog.get("eggs", []))} hidden')
    if '--online' in sys.argv[1:]:
        check_online(apps)
    print('PASS')


if __name__ == '__main__':
    main()
