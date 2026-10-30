#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pins every app's newest release into catalog.json: its file's SHA-256 and size, hashed here from a download.

Most homebrew releases publish no checksum file. Solanum checks every download before anything is unpacked, so for
each app this reads its repository's newest release (GitHub's latest release, never a draft or a pre-release; for an
app whose `channel` is "newest", the newest release that has the app's file, pre-releases included), downloads the
file the catalog's pattern names, and writes what it found into the app's entry:

    "release": {"tag", "asset", "size", "sha256", "content_version", "title_name", "prerelease", "published_at",
                "notes"}   the newest release
    "pins":    [{"tag", "asset", "size", "sha256", "content_version", "title_name"}, ...]   the ones pinned before it
               (five at most)
    "stars":   the repository's stars on GitHub

A release is pinned only when everything agrees: the downloaded file's size is the one GitHub lists; its SHA-256 is
GitHub's digest of the file when GitHub keeps one, and the line the release's checksum file has for it when it has
one; the ZIP holds an app folder (eboot.bin beside sce_sys/param.json) whose titleId is the catalog's; it holds no
game images, ROMs, keys or packages; and, for an app the catalog marks attested, `gh attestation verify` accepts it
for its repository. A release pinned before is never pinned again: when the file GitHub now lists for a pinned tag is
another size or has another digest, or a download of it hashes to anything else, the file was replaced after it was
pinned, and that is an error, not a new pin. content_version is the app's contentVersion in the file's param.json:
Solanum compares it with the installed one, whatever the tag says; a pin made before it was recorded is downloaded
again, checked against its pin, and given it. title_name is the name in that param.json: when two apps share a title ID
(`shares`), Solanum tells which of them is installed by it, so the pins of those apps are given it the same way.

The pins live in this git repository, so every one is in its history. Solanum uses them only for Solanum's own
catalog, never for a custom one.

    python3 tools/pin_catalog.py            pins what is new, writes catalog.json when anything changed
    python3 tools/pin_catalog.py --check    the same, but writes nothing (exit 1 when it would change)
    python3 tools/pin_catalog.py --only a,b only those apps (their ids)

Set GITHUB_TOKEN (GH_TOKEN for gh) for GitHub's higher rate limit. Exit 0: done; 1: an integrity error (a file that
changed after it was pinned, or a release that isn't the app); a release that can't be pinned yet (no file of the
pattern's name) is a warning, and the app keeps its pin.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / 'catalog.json'
API = 'https://api.github.com/repos/'
MAX_FILE = 0x80000000  # 2 GiB, the install helper's own limit
MAX_PINS = 5           # pinned releases kept besides the newest
NOTES_BYTES = 2800     # Solanum keeps 3,072 bytes of notes
FORBIDDEN = re.compile(r'(?i)\.(nsp|nsz|xci|xcz|pkg|ffpkg|ffpfsc|iso|cue|chd|pbp|cso|gcm|rvz|wbfs|wad3?|z64|n64|v64|'
                       r'sfc|smc|gba|gbc|nds|3ds|cia|rom)$|(^|/)(prod|title|dev)\.keys$')


def pattern_name(pattern, tag):
    """The release file a pattern names for a tag: {version} is the tag without a leading v, {content} the tag as a
    content version (v0.7.0 -> 00.007.000). None when the pattern can't name one for this tag."""
    version = tag[1:] if tag.startswith('v') and tag[1:2].isdigit() else tag
    name = pattern.replace('{version}', version)
    if '{content}' in name:
        match = re.fullmatch(r'v?(\d{1,2})\.(\d{1,3})\.(\d{1,3})', tag)
        if not match:
            return None
        name = name.replace('{content}', '%02d.%03d.%03d' % tuple(int(p) for p in match.groups()))
    if not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9._-]*', name) or len(name) > 127:
        return None
    return name


def cut_notes(text):
    """A release's notes as Solanum keeps them: at most NOTES_BYTES of UTF-8, cut at a line's end when one is near."""
    text = (text or '').replace('\r\n', '\n').strip()
    data = text.encode('utf-8')
    if len(data) <= NOTES_BYTES:
        return text
    cut = data[:NOTES_BYTES].decode('utf-8', 'ignore')
    line = cut.rfind('\n')
    return (cut[:line] if line > NOTES_BYTES // 2 else cut).rstrip()


def request(url, accept='application/vnd.github+json'):
    req = urllib.request.Request(url, headers={'Accept': accept, 'User-Agent': 'solanum-catalog-pin'})
    token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
    if token and url.startswith('https://api.github.com/'):
        req.add_header('Authorization', f'Bearer {token}')
    return urllib.request.urlopen(req, timeout=60)


def get_json(url):
    with request(url) as response:
        return json.loads(response.read())


def download(url, path, limit):
    """The file into `path`, hashed as it arrives. (sha256, size)."""
    digest = hashlib.sha256()
    size = 0
    with request(url, accept='application/octet-stream') as response, open(path, 'wb') as out:
        while True:
            piece = response.read(1 << 20)
            if not piece:
                break
            size += len(piece)
            if size > limit:
                raise ValueError(f'larger than {limit} bytes')
            digest.update(piece)
            out.write(piece)
    return digest.hexdigest(), size


def sums_line(text, name):
    """The SHA-256 a checksum file gives `name`: None when it has no line for it; ValueError when two."""
    found = None
    for line in text.splitlines():
        match = re.fullmatch(r'([0-9a-fA-F]{64}) [ *](.+?)\s*', line)
        if match and match.group(2) == name:
            if found:
                raise ValueError(f'two lines for {name}')
            found = match.group(1).lower()
    return found


def title_name(param):
    """The app's name in a param.json: its default language's titleName, else the first one it has; '' for none."""
    localized = param.get('localizedParameters')
    if not isinstance(localized, dict):
        return ''
    names = [localized.get(localized.get('defaultLanguage'))] + [v for v in localized.values() if isinstance(v, dict)]
    for entry in names:
        if isinstance(entry, dict) and isinstance(entry.get('titleName'), str) and entry['titleName'].strip():
            return entry['titleName'].strip()[:63]
    return ''


def inspect_app(path, title_id):
    """The ZIP's app folder as the install helper finds it: the one folder (at the top, or up to three folders deep)
    holding sce_sys/param.json and eboot.bin. Returns its param.json's contentVersion and name
    ({'content_version', 'title_name'}); raises ValueError otherwise."""
    with zipfile.ZipFile(path) as archive:
        names = [info.filename for info in archive.infolist()]
        # The app's own image beside its folder (<title ID>.ffpfsc, for ShadowMountPlus to mount instead) is the app,
        # not a game, and the install helper never unpacks it; any other image or package is refused.
        own = re.compile(rf'(^|/){re.escape(title_id)}\.(ffpfsc|ffpkg)$')
        bad = [n for n in names if FORBIDDEN.search(n) and not own.search(n)]
        if bad:
            raise ValueError(f'game images, ROMs, keys or packages in the ZIP: {bad[:3]}')
        params = [n for n in names if n == 'sce_sys/param.json' or n.endswith('/sce_sys/param.json')]
        params = [n for n in params if n.count('/') <= 4]
        if len(params) != 1:
            raise ValueError(f'{len(params)} sce_sys/param.json in the ZIP, not one')
        prefix = params[0][:-len('sce_sys/param.json')]
        if prefix + 'eboot.bin' not in names:
            raise ValueError('no eboot.bin beside sce_sys')
        param = json.loads(archive.read(params[0]).decode('utf-8-sig'))
        if param.get('titleId') != title_id:
            raise ValueError(f'its param.json is {param.get("titleId")}, not {title_id}')
        return {'content_version': param.get('contentVersion', ''), 'title_name': title_name(param)}


def inspect_zip(path, title_id):
    """inspect_app's contentVersion alone."""
    return inspect_app(path, title_id)['content_version']


def attested(path, repo):
    """Whether gh attestation verify accepts the file for its repository; None when gh can't be run."""
    if not shutil.which('gh'):
        return None
    result = subprocess.run(['gh', 'attestation', 'verify', str(path), '-R', repo], capture_output=True, text=True,
                            timeout=300, check=False)
    return result.returncode == 0


class Problem(Exception):
    pass


LIST = 30  # releases read for an app whose channel is "newest"
PIN_KEYS = ('tag', 'asset', 'size', 'sha256', 'content_version', 'title_name')


def uploaded(release):
    return {a['name']: a for a in release.get('assets', []) if a.get('state', 'uploaded') == 'uploaded'}


def find_release(app, log):
    """The release to pin, or None (said in the log). GitHub's latest release (never a draft or a pre-release); for an
    app whose channel is "newest", the newest release (pre-releases included, drafts never) that has the file the
    pattern names for its tag, so that an app publishing only pre-releases, or other platforms' releases between its
    own, is pinned all the same."""
    repo = app['repo']
    if app.get('channel') == 'newest':
        try:
            releases = get_json(f'{API}{repo}/releases?per_page={LIST}')
        except urllib.error.HTTPError as error:
            log(f'{app["id"]}: its releases can\'t be read ({error.code}); left as it is')
            return None
        for release in releases if isinstance(releases, list) else []:
            name = pattern_name(app['asset'], release.get('tag_name', ''))
            if not release.get('draft') and name and name in uploaded(release):
                return release
        log(f'WARNING {app["id"]}: none of its newest {LIST} releases has {app["asset"]}; it keeps its pin')
        return None
    try:
        release = get_json(f'{API}{repo}/releases/latest')
    except urllib.error.HTTPError as error:
        log(f'{app["id"]}: no release ({error.code}); left as it is')
        return None
    if release.get('draft') or release.get('prerelease'):
        log(f'{app["id"]}: its newest release is a draft or a pre-release; left as it is')
        return None
    return release


def pin_app(app, work, log, names=False):
    """Pins one app's newest release. True when its entry changed. `names`: the app shares its title ID with another
    (`shares`), so its pinned release must say its param.json's name, and one pinned before it was recorded is
    downloaded again for it."""
    repo = app['repo']
    changed = False
    try:
        info = get_json(API + repo)
    except urllib.error.HTTPError as error:
        log(f'{app["id"]}: {repo} answered {error.code}; left as it is')
        return False
    stars = int(info.get('stargazers_count', 0))
    if app.get('stars') != stars:
        app['stars'] = stars
        changed = True
    release = find_release(app, log)
    if release is None:
        return changed
    tag = release['tag_name']
    name = pattern_name(app['asset'], tag)
    assets = uploaded(release)
    if not name or name not in assets:
        log(f'WARNING {app["id"]}: {tag} has no {name or app["asset"]}; it keeps its pin')
        return changed
    asset = assets[name]
    digest = (asset.get('digest') or '')
    digest = digest[7:].lower() if digest.startswith('sha256:') else None
    prerelease = bool(release.get('prerelease'))
    pinned = app.get('release') or {}
    if pinned.get('tag') == tag and pinned.get('asset') == name:
        # Pinned already: the file must still be the one that was hashed.
        if asset['size'] != pinned['size'] or (digest and digest != pinned['sha256']):
            raise Problem(f'{app["id"]}: {tag}/{name} is not the file that was pinned (size {asset["size"]} '
                          f'for {pinned["size"]}, digest {digest} for {pinned["sha256"]}): it was replaced')
        if not pinned.get('content_version') or (names and 'title_name' not in pinned):
            # Pinned before content versions (or names) were recorded: downloaded again, it must hash to the pin.
            path = Path(work) / f'{app["id"]}.zip'
            sha256, size = download(f'https://github.com/{repo}/releases/download/{tag}/{name}', path, MAX_FILE)
            if sha256 != pinned['sha256'] or size != pinned['size']:
                raise Problem(f'{app["id"]}: {tag}/{name} hashes to {sha256}, not its pin {pinned["sha256"]}: '
                              f'it was replaced')
            try:
                found = inspect_app(path, app['title_id'])
            except (ValueError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as error:
                raise Problem(f'{app["id"]}: {tag}/{name} isn\'t the app: {error}') from error
            pinned['content_version'] = found['content_version']
            pinned['title_name'] = found['title_name']
            path.unlink()
            changed = True
        notes = cut_notes(release.get('body'))
        if pinned.get('notes', '') != notes or pinned.get('published_at') != release.get('published_at'):
            pinned['notes'] = notes
            pinned['published_at'] = release.get('published_at')
            changed = True
        if bool(pinned.get('prerelease')) != prerelease:
            if prerelease:
                pinned['prerelease'] = True
            else:
                pinned.pop('prerelease', None)
            changed = True
        log(f'{app["id"]}: {tag} pinned already')
        return changed
    for old in app.get('pins', []):
        if old.get('tag') == tag and old.get('asset') == name and (asset['size'] != old['size'] or
                                                                     (digest and digest != old['sha256'])):
            raise Problem(f'{app["id"]}: {tag}/{name} was pinned before as another file: it was replaced')
    if asset['size'] <= 0 or asset['size'] > MAX_FILE:
        log(f'WARNING {app["id"]}: {name} is {asset["size"]} bytes; not pinned')
        return changed

    # A new release: downloaded, hashed, and checked every way it can be.
    path = Path(work) / f'{app["id"]}.zip'
    url = f'https://github.com/{repo}/releases/download/{tag}/{name}'
    sha256, size = download(url, path, MAX_FILE)
    if size != asset['size']:
        raise Problem(f'{app["id"]}: {name} downloaded as {size} bytes, GitHub lists {asset["size"]}')
    if digest and digest != sha256:
        raise Problem(f'{app["id"]}: {name} hashes to {sha256}, GitHub\'s digest is {digest}')
    sums = app.get('checksums') or 'SHA256SUMS'
    if sums in assets:
        text = request(f'https://github.com/{repo}/releases/download/{tag}/{sums}',
                       accept='application/octet-stream').read(65536).decode('utf-8', 'replace')
        line = sums_line(text, name)
        if line and line != sha256:
            raise Problem(f'{app["id"]}: {sums} gives {name} {line}, it hashes to {sha256}')
    # A tag pinned before (an older release that is the newest again) must be the very file pinned then.
    for old in app.get('pins', []):
        if old.get('tag') == tag and old.get('asset') == name and (old['sha256'] != sha256 or old['size'] != size):
            raise Problem(f'{app["id"]}: {tag}/{name} hashes to {sha256}, but it was pinned as {old["sha256"]}: '
                          f'it was replaced')
    try:
        found = inspect_app(path, app['title_id'])
    except (ValueError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as error:
        raise Problem(f'{app["id"]}: {tag}/{name} isn\'t the app: {error}') from error
    if app.get('attested'):
        verdict = attested(path, repo)
        if verdict is False:
            raise Problem(f'{app["id"]}: the catalog says it is attested, but gh attestation verify refuses {name}')
        if verdict is None:
            log(f'WARNING {app["id"]}: gh isn\'t here to check its attestation')
    path.unlink()

    if pinned.get('tag'):
        older = [{k: pinned[k] for k in PIN_KEYS if k in pinned}]
        older += [p for p in app.get('pins', []) if p.get('tag') != pinned['tag'] and p.get('tag') != tag]
        app['pins'] = older[:MAX_PINS]
    app['release'] = {'tag': tag, 'asset': name, 'size': size, 'sha256': sha256,
                      'content_version': found['content_version'], 'title_name': found['title_name']}
    if prerelease:
        app['release']['prerelease'] = True
    app['release'].update({'published_at': release.get('published_at'), 'notes': cut_notes(release.get('body'))})
    log(f'{app["id"]}: pinned {tag} {name} {size} bytes sha256 {sha256} (contentVersion {found["content_version"]}'
        f'{", a pre-release" if prerelease else ""}{", digest agrees" if digest else ""}'
        f'{", " + sums + " agrees" if sums in assets else ""})')
    return True


def sharing(apps):
    """The ids of the apps that share a title ID with another (`shares`, either side of it)."""
    found = set()
    for app in apps:
        if app.get('shares'):
            found.update((app['id'], app['shares']))
    return found


def main():
    arguments = sys.argv[1:]
    check = '--check' in arguments
    only = None
    if '--only' in arguments:
        only = set(arguments[arguments.index('--only') + 1].split(','))
    text = CATALOG.read_text(encoding='utf-8')
    catalog = json.loads(text)
    named = sharing(catalog['apps'])
    problems = []
    changed = False
    with tempfile.TemporaryDirectory() as work:
        for app in catalog['apps']:
            if only and app['id'] not in only:
                continue
            try:
                changed = pin_app(app, work, print, names=app['id'] in named) or changed
            except Problem as problem:
                problems.append(str(problem))
                print(f'ERROR {problem}')
            except (urllib.error.URLError, OSError, ValueError) as error:
                print(f'WARNING {app["id"]}: {error}; it keeps its pin')
    if changed and not check:
        CATALOG.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        print('catalog.json written')
    elif changed:
        print('catalog.json would change')
    for problem in problems:
        print(f'ERROR {problem}')
    sys.exit(1 if problems or (check and changed) else 0)


if __name__ == '__main__':
    main()
