#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Checks catalog.json, the list of apps Solanum installs.

Offline (always): the schema; unique ids and title IDs; title IDs in the console's form (four letters, five
digits); a repository in owner/name form; a known category; a license; a file pattern that names a release's file
({version} is the tag without a leading v, {content} the tag as a content version); and what the pin workflow and the
authors add: tags (lowercase), an author, a data folder (one folder of its own directly in /data, none the console,
the HEN, ShadowMountPlus or Solanum keeps), stars, and pinned releases (a tag, the file the pattern names for it, its
size and a lowercase SHA-256, and notes Solanum can hold).

Online (--online): for every app, the repository's latest release (not a draft or a pre-release) has the app's file,
and Solanum can check it: the release's checksum file names it, GitHub keeps a digest of it, or the catalog pins it.
When the catalog pins that very release, the file GitHub lists is the pinned one (its size, and its digest when GitHub
keeps one): a file replaced after it was pinned fails. A newer release that has neither a checksum nor a pin yet is a
warning (the pin workflow pins it on its next run; Solanum offers the pinned one meanwhile). Set GITHUB_TOKEN for
GitHub's higher rate limit.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pin_catalog import NOTES_BYTES, pattern_name  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ('id', 'name', 'category', 'summary', 'title_id', 'repo', 'asset', 'folder', 'checksums',
            'attested', 'license')
CATEGORIES = {'emulator', 'media', 'streaming', 'game', 'tool', 'store'}
TITLE_ID = re.compile(r'^[A-Z]{4}[0-9]{5}$')
REPO = re.compile(r'^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$')
ID = re.compile(r'^[a-z0-9-]+$')
NAME = re.compile(r'^[A-Za-z0-9_-][A-Za-z0-9._-]*$')
SHA256 = re.compile(r'^[0-9a-f]{64}$')
# The same folders as Solanum's catalog.c and its install helper's data_folder_allowed.
KEPT = {'homebrew', 'etahen', 'solanum', 'self-update', 'system', 'shadowmountplus', 'shadowmount', 'payloads', 'lapy',
        'user', 'app', 'apps', 'savedata', 'pkg', 'games', 'tmp', 'temp', 'data', 'mnt', 'usb', 'ext', 'ftp', 'elfldr',
        'ps5_autoloader', 'itemzflow', 'prosperostore', 'sandbox'}
warnings = []


def fail(message):
    print(f'FAIL: {message}')
    sys.exit(1)


def check_pin(app, pin, what):
    for key in ('tag', 'asset', 'size', 'sha256'):
        if key not in pin:
            fail(f'{app["id"]}: {what} has no {key}')
    if not SHA256.match(pin['sha256']):
        fail(f'{app["id"]}: {what}\'s sha256 is not 64 lowercase hex digits')
    if not isinstance(pin['size'], int) or not 0 < pin['size'] <= 0x80000000:
        fail(f'{app["id"]}: {what}\'s size is not a size')
    if pattern_name(app['asset'], pin['tag']) != pin['asset']:
        fail(f'{app["id"]}: {what} pins {pin["asset"]}, but the pattern names {pattern_name(app["asset"], pin["tag"])} '
             f'for {pin["tag"]}')


def check_offline(catalog):
    if catalog.get('schema') != 1:
        fail('schema must be 1')
    apps = catalog.get('apps')
    if not isinstance(apps, list) or not apps:
        fail('apps must be a non-empty list')
    if len(apps) > 256:
        fail('Solanum reads 256 apps at most')
    seen_ids, seen_titles = set(), set()
    for app in apps:
        missing = [key for key in REQUIRED if key not in app]
        if missing:
            fail(f'{app.get("id", "?")}: missing {", ".join(missing)}')
        if not ID.match(app['id']):
            fail(f'{app["id"]}: id is not lowercase letters, digits and -')
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
        if not isinstance(app['attested'], bool) or not isinstance(app.get('family', False), bool):
            fail(f'{app["id"]}: attested and family are true or false')
        if not pattern_name(app['asset'], 'v1.0.0'):
            fail(f'{app["id"]}: asset {app["asset"]} can\'t name a release\'s file')
        if app['folder'] != '.' and not NAME.match(app['folder']):
            fail(f'{app["id"]}: folder {app["folder"]} can\'t be a folder\'s name')
        if not NAME.match(app['checksums']):
            fail(f'{app["id"]}: checksums {app["checksums"]} can\'t name a release\'s file')
        for key, size in (('name', 63), ('summary', 255), ('author', 47), ('license', 63)):
            if key in app and (not isinstance(app[key], str) or len(app[key].encode('utf-8')) > size):
                fail(f'{app["id"]}: {key} is not text of at most {size} bytes')
        tags = app.get('tags', [])
        if not isinstance(tags, list) or len(tags) > 8 or not all(isinstance(t, str) and ID.match(t) and len(t) < 24
                                                                   for t in tags):
            fail(f'{app["id"]}: tags are up to eight lowercase words (letters, digits, -)')
        if 'data' in app:
            name = app['data'][6:] if app['data'].startswith('/data/') else ''
            if not NAME.match(name) or name.lower() in KEPT or len(app['data']) > 70:
                fail(f'{app["id"]}: data {app["data"]} is not a folder of its own directly in /data')
        if 'stars' in app and (not isinstance(app['stars'], int) or app['stars'] < 0):
            fail(f'{app["id"]}: stars is a count')
        if 'release' in app:
            check_pin(app, app['release'], 'its release')
            if len(app['release'].get('notes', '').encode('utf-8')) > NOTES_BYTES:
                fail(f'{app["id"]}: its release notes are longer than {NOTES_BYTES} bytes')
        pins = app.get('pins', [])
        if pins and 'release' not in app:
            fail(f'{app["id"]}: older pins without a pinned release')
        for i, pin in enumerate(pins):
            check_pin(app, pin, f'pin {i}')
    for key in ('tips', 'eggs'):
        lines = catalog.get(key, [])
        if not all(isinstance(line, str) and line.strip() for line in lines):
            fail(f'every entry of {key} must be text')
    return apps


def get(url):
    request = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json',
                                                   'User-Agent': 'solanum-catalog-check'})
    token = os.environ.get('GITHUB_TOKEN')
    if token and url.startswith('https://api.github.com/'):
        request.add_header('Authorization', f'Bearer {token}')
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def check_online(apps):
    for app in apps:
        release = json.loads(get(f'https://api.github.com/repos/{app["repo"]}/releases/latest'))
        tag = release['tag_name']
        asset = pattern_name(app['asset'], tag)
        files = {a['name']: a for a in release['assets'] if a.get('state', 'uploaded') == 'uploaded'}
        if not asset or asset not in files:
            fail(f'{app["id"]}: {tag} has no {asset or app["asset"]}')
        digest = files[asset].get('digest') or ''
        digest = digest[7:] if digest.startswith('sha256:') else ''
        pinned = app.get('release', {})
        how = []
        if pinned.get('tag') == tag:
            if files[asset]['size'] != pinned['size'] or (digest and digest != pinned['sha256']):
                fail(f'{app["id"]}: {tag}/{asset} is not the file the catalog pinned: it was replaced')
            how.append('pinned')
        if digest:
            how.append('GitHub\'s digest')
        if app['checksums'] in files:
            sums = urllib.request.urlopen(files[app['checksums']]['browser_download_url'], timeout=30).read()
            sums = sums.decode('utf-8', 'replace')
            if re.search(rf'^[0-9a-fA-F]{{64}}\s+\*?{re.escape(asset)}\s*$', sums, re.M):
                how.append(app['checksums'])
        if not how:
            warnings.append(f'{app["id"]}: {tag} has no checksum and isn\'t pinned yet; Solanum offers '
                            f'{pinned.get("tag", "nothing")} until the pin workflow pins it')
            print(f'WARNING {warnings[-1]}')
            continue
        print(f'{app["id"]}: {tag} {asset}, checked against {", ".join(how)}')


def main():
    catalog = json.loads((ROOT / 'catalog.json').read_text(encoding='utf-8'))
    apps = check_offline(catalog)
    pinned = sum(1 for app in apps if 'release' in app)
    print(f'catalog: {len(apps)} apps ({pinned} pinned), {len(catalog.get("tips", []))} tips, '
          f'{len(catalog.get("eggs", []))} hidden')
    if '--online' in sys.argv[1:]:
        check_online(apps)
    print(f'PASS{f" ({len(warnings)} warnings)" if warnings else ""}')


if __name__ == '__main__':
    main()
