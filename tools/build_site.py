#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Builds the catalog's website from catalog.json: plain HTML, one stylesheet and one small script, no framework,
no external fonts, scripts or services (the QR codes are inline SVGs made here).

    python3 tools/build_site.py                          writes site/ (index.html, apps/<id>/, submit/, get-solanum/)
    python3 tools/build_site.py --fetch-icons            first looks for each app's icon in its repository
    python3 tools/build_site.py --base-url https://example.org/catalog --out public

Pages: the home page (every app, a search and a category filter that work in plain JavaScript), a page for each app
(icon, summary, license, the newest pinned version and its date, the SHA-256, the GitHub link, a QR code to the page
for installing through the Solanum app), "Submit your app" and "Get Solanum". Everything comes from catalog.json, so
the site shows what the pin workflow pinned. Notes say what to know before installing an app: "No license" when its
repository states none, "Needs your own game files" for a port, "Pre-release" when its newest release is one, and the
other app of its title ID when two share one.

Icons: an app's icon is its sce_sys/icon0.png in its own repository. --fetch-icons finds it through GitHub's API (the
one beside a param.json naming the app's title ID when a repository has several), downloads it, shrinks it to 128
pixels and keeps it in .cache/icons (a week), which is where the build reads them from; an app with none gets its
initial on Nightshade's red, as in the app. Set GITHUB_TOKEN for GitHub's higher rate limit.
"""
import argparse
import html
import json
import os
import re
import shutil
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import qr  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'web'
DEFAULT_BASE_URL = 'https://oliv3rmoon.github.io/Solanum-Catalog'
CATALOG_REPO = 'Oliv3rmoon/Solanum-Catalog'
APP_REPO = 'Oliv3rmoon/Solanum'
APP_TITLE_ID = 'PPSA76526'
CATEGORIES = (('emulator', 'Emulators'), ('media', 'Media'), ('streaming', 'Streaming'), ('game', 'Games'),
              ('tool', 'Tools'), ('store', 'Stores'))
CATEGORY_NAME = dict(CATEGORIES)
CATEGORY_ONE = {'emulator': 'Emulator', 'media': 'Media', 'streaming': 'Streaming', 'game': 'Game', 'tool': 'Tool',
                'store': 'Store'}
REPO = re.compile(r'^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$')
SPDX = re.compile(r'^[A-Za-z0-9.+-]+$')
NO_LICENSE = 'none stated'  # an app whose repository states no license
MONTHS = ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October',
          'November', 'December')
ICON_SIZE = 128
ICON_MAX_BYTES = 6 << 20
ICON_MAX_PIXELS = 2048 * 2048
ICON_MAX_AGE = 7 * 24 * 3600
CACHE = ROOT / '.cache' / 'icons'


# ---------------------------------------------------------------- small helpers

def esc(text):
    return html.escape(str(text), quote=True)


def format_date(stamp):
    """'2026-10-30T11:24:50Z' -> '30 October 2026'; '' when it isn't a date."""
    match = re.match(r'^(\d{4})-(\d{2})-(\d{2})', stamp or '')
    if not match or not 1 <= int(match.group(2)) <= 12:
        return ''
    return f'{int(match.group(3))} {MONTHS[int(match.group(2)) - 1]} {match.group(1)}'


def format_size(size):
    """38563249 -> '38.6 MB' (decimal, as Solanum shows it)."""
    if not isinstance(size, int) or size <= 0:
        return ''
    if size >= 1_000_000_000:
        return f'{size / 1e9:.1f} GB'
    if size >= 1_000_000:
        return f'{size / 1e6:.1f} MB'
    return f'{max(1, round(size / 1e3))} KB'


def app_url(app):
    return f'apps/{app["id"]}/'


def github(repo):
    return f'https://github.com/{repo}' if REPO.match(repo or '') else ''


def sorted_apps(apps):
    """The Nightshade family first, then by name, as Solanum lists them."""
    return sorted(apps, key=lambda a: (not a.get('family', False), a['name'].lower(), a['id']))


# ---------------------------------------------------------------- PNG: read, shrink, write (no libraries)

def decode_png(data, max_pixels=ICON_MAX_PIXELS):
    """(width, height, rows) of a PNG as rows of RGBA bytes. ValueError for what it doesn't read: a file that isn't
    a PNG, an interlaced one, one with more than max_pixels, or a damaged one."""
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('not a PNG')
    pos, idat, palette, trns, header = 8, [], b'', b'', None
    while pos + 8 <= len(data):
        length, kind = struct.unpack('>I4s', data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b'IHDR' and length == 13:
            header = struct.unpack('>IIBBBBB', body)
        elif kind == b'PLTE':
            palette = body
        elif kind == b'tRNS':
            trns = body
        elif kind == b'IDAT':
            idat.append(body)
        elif kind == b'IEND':
            break
    if not header or not idat:
        raise ValueError('damaged PNG')
    width, height, depth, ctype, compression, filtering, interlace = header
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if (not channels or compression or filtering or interlace or depth not in (1, 2, 4, 8, 16) or
            (ctype in (2, 4, 6) and depth < 8) or (ctype == 3 and depth > 8)):
        raise ValueError('a kind of PNG this does not read')
    if not 0 < width * height <= max_pixels or width > 16384 or height > 16384:
        raise ValueError('a picture too large')
    bits = channels * depth
    step = max(1, bits // 8)
    stride = (width * bits + 7) // 8
    expected = height * (stride + 1)
    raw = zlib.decompressobj().decompress(b''.join(idat), expected + 1)
    if len(raw) < expected:
        raise ValueError('damaged PNG')
    rows, previous = [], bytearray(stride)
    for y in range(height):
        base = y * (stride + 1)
        kind, line = raw[base], bytearray(raw[base + 1:base + 1 + stride])
        if kind == 1:
            for i in range(step, stride):
                line[i] = (line[i] + line[i - step]) & 255
        elif kind == 2:
            line = bytearray((a + b) & 255 for a, b in zip(line, previous))
        elif kind == 3:
            for i in range(stride):
                line[i] = (line[i] + (((line[i - step] if i >= step else 0) + previous[i]) >> 1)) & 255
        elif kind == 4:
            for i in range(stride):
                a = line[i - step] if i >= step else 0
                b = previous[i]
                c = previous[i - step] if i >= step else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        elif kind != 0:
            raise ValueError('damaged PNG')
        rows.append(line)
        previous = line
    entries = [palette[i * 3:i * 3 + 3].ljust(3, b'\0') for i in range(len(palette) // 3)] or [b'\0\0\0']
    return width, height, [to_rgba(line, width, depth, ctype, entries, trns) for line in rows]


def to_rgba(line, width, depth, ctype, entries, trns):
    out = bytearray(width * 4)
    if depth == 16:
        line = line[0::2]
        depth = 8
    if ctype == 6:
        return bytearray(line)
    if ctype == 2:
        out[0::4], out[1::4], out[2::4], out[3::4] = line[0::3], line[1::3], line[2::3], b'\xff' * width
    elif ctype == 4:
        out[0::4] = out[1::4] = out[2::4] = line[0::2]
        out[3::4] = line[1::2]
    else:
        if depth < 8:  # several samples to a byte, the leftmost in the high bits
            mask = (1 << depth) - 1
            samples = bytearray(width)
            for i in range(width):
                samples[i] = (line[i * depth // 8] >> (8 - depth - (i * depth) % 8)) & mask
        else:
            samples = line[:width]
        if ctype == 0:
            scale = 255 // ((1 << depth) - 1)
            gray = bytes(min(255, s * scale) for s in samples)
            out[0::4] = out[1::4] = out[2::4] = gray
            out[3::4] = b'\xff' * width
        else:
            for i, s in enumerate(samples):
                rgb = entries[s] if s < len(entries) else entries[0]
                out[i * 4:i * 4 + 3] = rgb
                out[i * 4 + 3] = trns[s] if s < len(trns) else 255
    return out


def shrink(width, height, rows, size=ICON_SIZE):
    """The picture at most `size` pixels on a side: an average of whole blocks (colors weighted by their alpha) when
    that divides it, the nearest pixels otherwise; a smaller picture is left as it is."""
    if width <= size and height <= size:
        return width, height, rows
    if width == height and width % size == 0:
        f = width // size
        out_rows = []
        for top in range(0, height, f):
            sums = [[0] * size for _ in range(4)]  # premultiplied r, g, b, and alpha, per output column
            for line in rows[top:top + f]:
                a = line[3::4]
                for c in range(3):
                    premult = [x * y for x, y in zip(line[c::4], a)]
                    horizontal = [sum(t) for t in zip(*[premult[k::f] for k in range(f)])]
                    sums[c] = [p + q for p, q in zip(sums[c], horizontal)]
                sums[3] = [p + q for p, q in zip(sums[3], [sum(t) for t in zip(*[a[k::f] for k in range(f)])])]
            out = bytearray(size * 4)
            for x in range(size):
                alpha = sums[3][x]
                for c in range(3):
                    out[x * 4 + c] = min(255, (sums[c][x] + alpha // 2) // alpha) if alpha else 0
                out[x * 4 + 3] = min(255, (alpha + f * f // 2) // (f * f))
            out_rows.append(out)
        return size, size, out_rows
    scale = size / max(width, height)
    new_w, new_h = max(1, round(width * scale)), max(1, round(height * scale))
    out_rows = []
    for y in range(new_h):
        line = rows[min(height - 1, int((y + 0.5) / scale))]
        out = bytearray(new_w * 4)
        for x in range(new_w):
            s = min(width - 1, int((x + 0.5) / scale)) * 4
            out[x * 4:x * 4 + 4] = line[s:s + 4]
        out_rows.append(out)
    return new_w, new_h, out_rows


def encode_png(width, height, rows):
    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xffffffff)
    raw = b''.join(b'\0' + bytes(line) for line in rows)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


def small_icon(data):
    """A repository's icon as the PNG the site serves; ValueError when it can't be read."""
    return encode_png(*shrink(*decode_png(data)))


# ---------------------------------------------------------------- icons from the apps' repositories

def fetch(url, token='', limit=ICON_MAX_BYTES, accept='application/vnd.github+json'):
    headers = {'User-Agent': 'Solanum-Catalog-site', 'Accept': accept}
    if token and url.startswith('https://api.github.com/'):
        headers['Authorization'] = f'Bearer {token}'
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
                body = response.read(limit + 1)
            if len(body) > limit:
                raise ValueError('too large')
            return body
        except urllib.error.HTTPError as error:
            if error.code in (403, 404, 409, 451):  # not there, or not for us: another try changes nothing
                raise
            last = error
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            last = error
        time.sleep(1 + attempt)
    raise last


def icon_path(app, token):
    """The path of the icon in the app's repository, or None. Several icons: the one beside a param.json that names
    the app's title ID, else the PS5's (sce_sys, ps5 in the path), else the shallowest."""
    tree = json.loads(fetch(f'https://api.github.com/repos/{app["repo"]}/git/trees/HEAD?recursive=1', token))
    paths = [e['path'] for e in tree.get('tree', []) if e.get('type') == 'blob']
    names = set(paths)
    candidates = [p for p in paths if p.lower().endswith('icon0.png')]
    if not candidates:
        return None

    def score(path):
        parts = path.lower().split('/')
        return ((10 if 'sce_sys' in parts else 0) + (5 if 'ps5' in parts else 0) -
                (5 if {'ps4', 'psv', 'vita', 'ps3'} & set(parts) else 0) - 0.1 * len(parts), -len(parts))

    candidates.sort(key=score, reverse=True)
    if len(candidates) > 1:
        for path in candidates[:6]:
            param = path.rsplit('/', 1)[0] + '/param.json' if '/' in path else 'param.json'
            if param in names:
                try:
                    text = fetch('https://raw.githubusercontent.com/' + app['repo'] + '/HEAD/' +
                                 urllib.parse.quote(param), limit=1 << 20).decode('utf-8', 'replace')
                except (urllib.error.URLError, ValueError, TimeoutError):
                    continue
                if app['title_id'] in text:
                    return path
    return candidates[0]


def fetch_icons(apps, cache=CACHE, token=''):
    """Fills the cache with each app's icon (a week's copy is kept); a repository without one is remembered as that."""
    cache.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for app in apps:
        if not REPO.match(app.get('repo', '')):
            continue
        png, none = cache / f'{app["id"]}.png', cache / f'{app["id"]}.none'
        if any(p.exists() and now - p.stat().st_mtime < ICON_MAX_AGE for p in (png, none)):
            continue
        try:
            path = icon_path(app, token)
            if path:
                data = fetch('https://raw.githubusercontent.com/' + app['repo'] + '/HEAD/' + urllib.parse.quote(path))
                png.write_bytes(small_icon(data))
                none.unlink(missing_ok=True)
                print(f'icon: {app["id"]} <- {path}')
            else:
                none.write_text('no icon0.png\n')
                png.unlink(missing_ok=True)
                print(f'icon: {app["id"]} has none')
        except (urllib.error.URLError, ValueError, OSError, KeyError, TimeoutError, zlib.error) as error:
            print(f'icon: {app["id"]} skipped ({error})')  # keep what the cache has; the page works without it


# ---------------------------------------------------------------- pages

NAV = (('', 'Apps'), ('submit/', 'Submit your app'), ('get-solanum/', 'Get Solanum'))


def no_license(app):
    return app.get('license', '').strip().lower() == NO_LICENSE


def badges(app):
    found = ''
    if app.get('family'):
        found += '<span class="badge family">Nightshade family</span>'
    if app.get('attested'):
        found += '<span class="badge good">Attested</span>'
    if app.get('needs_game_files'):
        found += '<span class="badge warn">Needs your own game files</span>'
    if no_license(app):
        found += '<span class="badge warn">No license</span>'
    if (app.get('release') or {}).get('prerelease'):
        found += '<span class="badge note">Pre-release</span>'
    return found


class Site:
    def __init__(self, catalog, base_url, out, icons=CACHE):
        self.catalog = catalog
        self.apps = sorted_apps(catalog['apps'])
        self.base_url = base_url.rstrip('/')
        self.base_path = urllib.parse.urlparse(self.base_url).path.rstrip('/')
        self.out = Path(out)
        self.icons = Path(icons)
        self.have_icon = {a['id'] for a in self.apps if (self.icons / f'{a["id"]}.png').is_file()}
        self.solanum = next((a for a in self.apps if a.get('repo') == APP_REPO), None)
        self.by_id = {a['id']: a for a in self.apps}

    def sharing(self, app):
        """The other apps of an app's title ID (`shares`, either side): a console holds one of them."""
        return [a for a in self.apps if a['id'] != app['id'] and a['title_id'] == app['title_id'] and
                (a.get('shares') == app['id'] or app.get('shares') == a['id'] or
                 (app.get('shares') and a.get('shares') == app.get('shares')))]

    # --- the parts every page shares

    def page(self, path, title, description, body, depth, script=False, root=None):
        """A whole page. `path` is the page's place in the site ('submit/'), `depth` how many folders deep it is;
        `root` (default: '../' for each) is what the links to the site's own files start with."""
        root = '../' * depth if root is None else root
        url = f'{self.base_url}/{path}'
        links = []
        for href, label in NAV:
            current = ' aria-current="page"' if href == path else ''
            links.append(f'<a href="{root + href or "./"}"{current}>{label}</a>')
        links.append(f'<a href="https://github.com/{CATALOG_REPO}" rel="noopener">GitHub</a>')
        brand = root or './'
        script_tag = f'<script src="{root}assets/app.js" defer></script>' if script else ''
        return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<meta name="theme-color" content="#09090b">
<meta property="og:site_name" content="Solanum Catalog">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:type" content="website">
<meta property="og:url" content="{esc(url)}">
<link rel="canonical" href="{esc(url)}">
<link rel="icon" href="{root}assets/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="{root}assets/style.css">
<script>document.documentElement.classList.add('js')</script>
{script_tag}
</head>
<body>
<a class="skip" href="#main">Skip to the content</a>
<header class="top">
<div class="wrap bar">
<a class="brand" href="{brand}"><span class="mark" aria-hidden="true"></span>Solanum <span class="dim">Catalog</span></a>
<nav aria-label="Main">{''.join(links)}</nav>
</div>
</header>
<main id="main" class="wrap">
{body}
</main>
<footer class="foot">
<div class="wrap">
<p>The open catalog of PS5 homebrew that <a href="{root}get-solanum/">Solanum</a> installs. Every app is its authors', under its own license; the catalog is GPL-3.0-or-later.</p>
<p><a href="https://github.com/{CATALOG_REPO}" rel="noopener">Solanum-Catalog on GitHub</a> · <a href="https://github.com/{APP_REPO}" rel="noopener">Solanum on GitHub</a> · <a href="{root}submit/">Submit your app</a></p>
</div>
</footer>
</body>
</html>
'''

    def icon(self, app, size, root, lazy=True):
        if app['id'] in self.have_icon:
            loading = ' loading="lazy"' if lazy else ''
            return (f'<img class="icon" src="{root}icons/{esc(app["id"])}.png" width="{size}" height="{size}" alt="" '
                    f'decoding="async"{loading}>')
        initial = esc((app['name'].strip() or '?')[0].upper())
        return f'<span class="icon tile" style="--size:{size}px" aria-hidden="true">{initial}</span>'

    # --- home

    def home(self):
        apps = self.apps
        counts = {key: sum(1 for a in apps if a['category'] == key) for key, _ in CATEGORIES}
        chips = ''.join(f'<button type="button" class="chip" data-category="{key}" aria-pressed="false">{label} '
                        f'<span>{counts[key]}</span></button>' for key, label in CATEGORIES if counts[key])
        cards = []
        for app in apps:
            release = app.get('release') or {}
            tags = ' '.join('#' + t for t in app.get('tags', []))
            search = ' '.join([app['name'], app['summary'], app.get('author', ''), app['title_id'], app['category'],
                               tags]).lower()
            meta = [CATEGORY_ONE.get(app['category'], app['category'])]
            if release.get('tag'):
                meta.append(release['tag'])
            cards.append(f'''<li class="card" data-category="{esc(app["category"])}" data-search="{esc(search)}">
<a href="{app_url(app)}">{self.icon(app, 64, '')}
<span class="card-body"><span class="card-title">{esc(app["name"])}</span>
<span class="card-meta">{esc(" · ".join(meta))}</span>
<span class="card-summary">{esc(app["summary"])}</span>
<span class="badges">{badges(app)}</span></span></a>
</li>''')
        about = self.catalog.get('about') or 'The open catalog of PS5 homebrew that Solanum installs.'
        categories = sum(1 for key in counts if counts[key])
        grid = '\n'.join(cards)
        body = f'''<section class="hero">
<h1>PS5 homebrew, <span class="accent">one open catalog</span></h1>
<p class="lead">{esc(about)}</p>
<p class="actions"><a class="button" href="get-solanum/">Get Solanum</a> <a class="button ghost" href="submit/">Submit your app</a></p>
<p class="stats"><strong>{len(apps)}</strong> apps · <strong>{categories}</strong> categories · every release pinned by its SHA-256</p>
</section>
<section aria-labelledby="apps-title">
<h2 id="apps-title" class="sr">Apps</h2>
<div class="controls">
<label class="sr" for="q">Search the apps</label>
<input id="q" type="search" placeholder="Search by name, author, title ID or #tag" autocomplete="off" spellcheck="false">
<div class="chips" role="group" aria-label="Category"><button type="button" class="chip" data-category="" aria-pressed="true">All <span>{len(apps)}</span></button>{chips}</div>
<p id="count" class="count" aria-live="polite"></p>
</div>
<ul id="grid" class="grid">
{grid}
</ul>
<p id="none" class="empty" hidden>No app matches. Try a shorter search, or <a href="submit/">submit yours</a>.</p>
</section>'''
        return self.page('', 'Solanum Catalog: PS5 homebrew, one open catalog',
                         f'{len(apps)} PS5 homebrew apps, each release pinned by its SHA-256. Browse them '
                         'and install them with Solanum.', body, 0, script=True)

    # --- an app

    def app_page(self, app):
        release = app.get('release') or {}
        title_id, name = app['title_id'], app['name']
        repo_url = github(app['repo'])
        facts = []

        def fact(label, value):
            facts.append(f'<div><dt>{label}</dt><dd>{value}</dd></div>')

        if release.get('tag'):
            version = esc(release['tag'])
            if repo_url:
                tag_url = f'{repo_url}/releases/tag/{urllib.parse.quote(release["tag"])}'
                version = f'<a href="{esc(tag_url)}" rel="noopener">{version}</a>'
            if release.get('prerelease'):
                version += ' <span class="dim">(a pre-release: its author hasn\'t called it finished)</span>'
            fact('Latest version', version)
            if format_date(release.get('published_at')):
                fact('Released', esc(format_date(release['published_at'])))
            if format_size(release.get('size')):
                fact('Download', f'{esc(release.get("asset", ""))} · {esc(format_size(release["size"]))}')
        else:
            fact('Latest version', '<span class="dim">Not pinned yet: the catalog workflow pins it on its next run.</span>')
        license_id = app.get('license', '')
        if no_license(app):
            fact('License', 'No license: its repository states none, so its author keeps every right to it. Solanum '
                            'installs it from the author\'s own release, as published.')
        elif SPDX.match(license_id):
            fact('License', f'<a href="https://spdx.org/licenses/{esc(license_id)}.html" rel="noopener">{esc(license_id)}</a>')
        else:
            fact('License', esc(license_id))
        if app.get('needs_game_files'):
            fact('Needs your own game files', 'It ships none: it runs the files of a game you own, which you copy to '
                                              'the console yourself. Its README says which.')
        others = self.sharing(app)
        if others:
            names = ', '.join(f'<a href="../{esc(o["id"])}/">{esc(o["name"])}</a>' for o in others)
            fact('Shares its title ID', f'with {names}. A console holds one of them: installing one replaces the other '
                                        '(saved data stays).')
        fact('Category', esc(CATEGORY_NAME.get(app['category'], app['category'])))
        if app.get('author'):
            fact('Author', esc(app['author']))
        fact('Title ID', f'<code>{esc(title_id)}</code>')
        if app.get('attested'):
            fact('Attested', 'Its own CI attests every build; <code>gh attestation verify</code> checks them.')
        if repo_url:
            fact('Source', f'<a href="{esc(repo_url)}" rel="noopener">github.com/{esc(app["repo"])}</a>')
        tags = ''.join(f'<a class="tag" href="../../?q=%23{urllib.parse.quote(t)}">#{esc(t)}</a>'
                       for t in app.get('tags', []))
        tags_html = f'<p class="tags">{tags}</p>' if tags else ''
        sha_html = ''
        if release.get('sha256'):
            sha_html = ('<h2>SHA-256</h2><p>Solanum checks every download of this release against it before anything is '
                        f'unpacked.</p><p class="hash"><code>{esc(release["sha256"])}</code></p>')
        button = f'<a class="button" href="{esc(repo_url)}" rel="noopener">View on GitHub</a>' if repo_url else ''
        page_url = f'{self.base_url}/{app_url(app)}'
        code = qr.to_svg(page_url, f'QR code for {page_url}')
        facts_html = ''.join(facts)
        body = f'''<nav class="crumbs" aria-label="Breadcrumb"><a href="../../">Apps</a> / <span>{esc(name)}</span></nav>
<article class="app">
<header class="app-head">
{self.icon(app, 96, '../../', lazy=False)}
<div>
<h1>{esc(name)}</h1>
<p class="lead">{esc(app["summary"])}</p>
<p class="badges">{badges(app)}</p>
</div>
</header>
<div class="cols">
<div class="col-main">
<dl class="facts">{facts_html}</dl>
{tags_html}
{sha_html}
<h2>Links</h2>
<p>{button}</p>
</div>
<aside class="col-side">
<section class="install">
<h2>Install it with Solanum</h2>
<ol>
<li><a href="../../get-solanum/">Get Solanum</a> on your PS5, if you haven't yet.</li>
<li>Open it, find <strong>{esc(name)}</strong> in Browse (or press Triangle and search for it, or for <code>{esc(title_id)}</code>) and press Cross.</li>
<li>Choose <strong>Install</strong>. Solanum downloads the release, checks it against the SHA-256 on this page, and puts it in <code>/data/homebrew/{esc(title_id)}</code>.</li>
</ol>
<figure class="qr">
<div class="qr-code">{code}</div>
<figcaption>Scan to open this page on your phone.</figcaption>
</figure>
</section>
</aside>
</div>
</article>'''
        return self.page(app_url(app), f'{name}: Solanum Catalog',
                         f'{app["summary"]} Install it on your PS5 with Solanum.', body, 2)

    # --- the two fixed pages

    def submit(self):
        fields = (
            ('id', 'short lowercase name (letters, digits and -)'),
            ('name', 'the name on the home screen'),
            ('category', '<code>emulator</code>, <code>media</code>, <code>streaming</code>, <code>game</code>, '
                         '<code>tool</code> or <code>store</code>'),
            ('summary', 'one line about what it does'),
            ('title_id', "the app's title ID: four capital letters and five digits, used by no other app (its "
                         '<code>sce_sys/param.json</code> says it)'),
            ('repo', '<code>owner/name</code> on GitHub'),
            ('asset', "the release's ZIP; <code>{version}</code> stands for the tag without a leading <code>v</code>"),
            ('folder', 'the app folder inside the ZIP, named after the title ID'),
            ('checksums', "the release's checksum file, usually <code>SHA256SUMS</code> (leave that when it has none)"),
            ('attested', '<code>true</code> only if your CI attests the build (<code>gh attestation verify</code> '
                         'must accept it)'),
            ('license', 'an SPDX identifier, such as <code>GPL-3.0-or-later</code> or <code>MIT</code>; the '
                        "license's own name when it has none; <code>none stated</code> when the repository states "
                        'none (Solanum and this site say "No license")'),
            ('author', 'optional: who makes it'),
            ('tags', "optional: up to eight lowercase words, for Solanum's search"),
            ('needs_game_files', 'optional: <code>true</code> for a port or launcher that runs the files of a game '
                                 'the player owns (it ships none)'),
            ('channel', 'optional: <code>newest</code> when the app\'s release is the newest one that has its file, '
                        'pre-releases included; leave it out for GitHub\'s latest release'),
            ('shares', "optional: another app's <code>id</code>, listed before it, whose title ID this one has too "
                       '(a fork that kept it). A console holds one of them'),
        )
        rows = ''.join(f'<tr><th scope="row"><code>{k}</code></th><td>{v}</td></tr>' for k, v in fields)
        entry = json.dumps({
            'id': 'my-app', 'name': 'My App', 'category': 'tool', 'summary': 'One line about what it does.',
            'title_id': 'PPSA12345', 'repo': 'your-name/my-app', 'asset': 'MyApp-v{version}.zip',
            'folder': 'PPSA12345', 'checksums': 'SHA256SUMS', 'attested': False, 'license': 'GPL-3.0-or-later',
            'author': 'your-name', 'tags': ['tool', 'example']}, indent=2)
        edit = f'https://github.com/{CATALOG_REPO}/edit/main/catalog.json'
        readme = f'https://github.com/{CATALOG_REPO}#readme'
        body = f'''<section class="page-head">
<h1>Submit your app</h1>
<p class="lead">Anyone can add an app, and there is no gatekeeper: the gate is a pull request.</p>
</section>
<div class="prose">
<h2>The rules</h2>
<ul class="rules">
<li><strong>A public repository and release.</strong> Releases come from your app's own GitHub repository. Its license is listed as it is: an OSI license, another license under its own name, or "No license" when the repository states none.</li>
<li><strong>A release ZIP with a title-ID folder.</strong> The ZIP holds your app's folder, named after its title ID, with <code>eboot.bin</code> beside <code>sce_sys/param.json</code> (Solanum finds it up to three folders deep, or at the top of the ZIP). The <code>titleId</code> in that <code>param.json</code> is the one you list.</li>
<li><strong>No game files.</strong> No game data, ROMs, BIOS files, keys or packages in a release. Ports that run the player's own game files (decompilations, recompilations, engine re-implementations, PC launchers) and emulators are welcome as long as they ship none; they are marked "Needs your own game files".</li>
<li><strong>No backup tools.</strong> Nothing whose main purpose is installing or loading commercial game dumps or backups: PKG and fPKG installers, backup loaders, dump-to-package converters.</li>
<li><strong>Authors decide.</strong> If you made an app and want it off the list, open an issue and it comes off.</li>
</ul>
<h2>How to submit it</h2>
<ol class="steps">
<li><a href="{edit}" rel="noopener">Edit <code>catalog.json</code> on GitHub</a> (it forks the repository for you), or fork the repository and clone it.</li>
<li>Add an entry like the one below to the <code>apps</code> list. Leave out <code>release</code>, <code>pins</code> and <code>stars</code>: the catalog's workflow writes those.</li>
<li>Open a pull request. CI checks the entry (<code>tools/check_catalog.py --online</code>): the title ID's shape and that no other app has it (unless the entry says it <code>shares</code> it), that your repository's latest release has the file you named and that Solanum can check it, and that every pinned release is still the file that was pinned.</li>
<li>Once it is merged, the workflow pins your newest release within six hours, and Solanum and this site show it.</li>
</ol>
<h2>The entry</h2>
<pre><code>{esc(entry)}</code></pre>
<table class="fields"><tbody>{rows}</tbody></table>
<p>The full rules and the pinning workflow are in the <a href="{readme}" rel="noopener">catalog's README</a>.</p>
<p class="actions"><a class="button" href="{edit}" rel="noopener">Start a pull request</a> <a class="button ghost" href="{readme}" rel="noopener">Read the README</a></p>
</div>'''
        return self.page('submit/', 'Submit your app: Solanum Catalog',
                         'How to add your PS5 homebrew to the Solanum catalog with a pull request, and the '
                         'rules.', body, 1)

    def get_solanum(self):
        entry = self.solanum or {}
        release = entry.get('release') or {}
        title_id = entry.get('title_id') or APP_TITLE_ID
        asset = release.get('asset') or f'{title_id}.zip'
        download = f'https://github.com/{APP_REPO}/releases/latest/download/{urllib.parse.quote(asset)}'
        info = ''
        if release.get('tag'):
            parts = [f'Latest release: <strong>{esc(release["tag"])}</strong>']
            if format_date(release.get('published_at')):
                parts.append(esc(format_date(release['published_at'])))
            if format_size(release.get('size')):
                parts.append(esc(format_size(release['size'])))
            info = '<p class="release-line">' + ' · '.join(parts) + '</p>'
            if release.get('sha256'):
                info += f'<p class="hash"><span class="dim">SHA-256</span> <code>{esc(release["sha256"])}</code></p>'
        body = f'''<section class="page-head">
<h1>Get Solanum</h1>
<p class="lead">A store for PS5 homebrew. It installs, updates, rolls back and uninstalls the apps of this catalog from their own GitHub releases, each checked against the SHA-256 the catalog pinned.</p>
<p class="actions"><a class="button" href="{esc(download)}" rel="noopener">Download {esc(asset)}</a> <a class="button ghost" href="https://github.com/{APP_REPO}/releases/latest" rel="noopener">Release notes</a></p>
{info}
</section>
<div class="prose">
<h2>Install it</h2>
<ol class="steps">
<li>Download <code>{esc(asset)}</code> and unzip it.</li>
<li>Copy its <code>{esc(title_id)}</code> folder to <code>/data/homebrew/</code> on the PS5 over FTP, and set its permissions to <code>777</code> (in FileZilla: right-click the folder, <em>File permissions</em>, <code>777</code>, recurse into subdirectories). Without them the PS5 says <em>Can't start the game or app</em>.</li>
<li>Restart ShadowMountPlus (or the console). <strong>Solanum</strong> appears on the home screen.</li>
</ol>
<p>It needs what the other Nightshade apps need: a HEN with the payload loader on port 9021 (etaHEN, or another with an ELF loader there), which takes Solanum's install helper and its launch payload, and <code>/data</code> unlocked so it can see the apps already installed. Its own data lives in <code>/data/solanum/</code>.</p>
<h2>Check the download</h2>
<p>Every release carries a <code>SHA256SUMS</code> file and is built and attested by its own workflow: <code>gh attestation verify {esc(asset)} -R {APP_REPO}</code>.</p>
<p>Once it is installed, Solanum updates itself from the same releases.</p>
</div>'''
        return self.page('get-solanum/', 'Get Solanum: Solanum Catalog',
                         'Download Solanum, the PS5 homebrew store that installs the apps of this open catalog.', body, 1)

    def not_found(self):
        root = self.base_path + '/'
        body = ('<section class="page-head"><h1>Page not found</h1><p class="lead">There is nothing at this address. '
                f'<a href="{root}">The apps are here.</a></p></section>')
        return self.page('404.html', 'Page not found: Solanum Catalog', 'This page does not exist.', body, 0, root=root)

    # --- writing it

    def write(self):
        out = self.out.resolve()
        if out == ROOT or out in ROOT.parents or (out / 'catalog.json').exists() or (out / '.git').exists():
            raise SystemExit(f'refusing to empty {out}: it is not a build folder')
        if out.exists():
            shutil.rmtree(out)
        (out / 'assets').mkdir(parents=True)
        for name in sorted(p.name for p in WEB.iterdir() if p.is_file()):
            shutil.copy(WEB / name, out / 'assets' / name)
        (out / 'index.html').write_text(self.home(), encoding='utf-8')
        for folder, text in (('submit', self.submit()), ('get-solanum', self.get_solanum())):
            (out / folder).mkdir()
            (out / folder / 'index.html').write_text(text, encoding='utf-8')
        for app in self.apps:
            (out / 'apps' / app['id']).mkdir(parents=True)
            (out / 'apps' / app['id'] / 'index.html').write_text(self.app_page(app), encoding='utf-8')
        (out / '404.html').write_text(self.not_found(), encoding='utf-8')
        (out / '.nojekyll').write_text('')
        if self.have_icon:
            (out / 'icons').mkdir()
            for app_id in sorted(self.have_icon):
                shutil.copy(self.icons / f'{app_id}.png', out / 'icons' / f'{app_id}.png')


def default_base_url():
    owner_repo = os.environ.get('GITHUB_REPOSITORY', '')
    if REPO.match(owner_repo):
        owner, name = owner_repo.split('/')
        return f'https://{owner.lower()}.github.io/{name}'
    return DEFAULT_BASE_URL


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--catalog', default=str(ROOT / 'catalog.json'))
    parser.add_argument('--out', default=str(ROOT / 'site'))
    parser.add_argument('--base-url', default=default_base_url(), help='where the site is published')
    parser.add_argument('--fetch-icons', action='store_true', help="look for the apps' icons in their repositories")
    parser.add_argument('--icons', default=str(CACHE), help='where the icons are kept')
    args = parser.parse_args(argv)
    catalog = json.loads(Path(args.catalog).read_text(encoding='utf-8'))
    if args.fetch_icons:
        fetch_icons(catalog['apps'], Path(args.icons), os.environ.get('GITHUB_TOKEN', ''))
    site = Site(catalog, args.base_url, args.out, args.icons)
    site.write()
    print(f'{len(site.apps)} apps, {len(site.have_icon)} icons -> {site.out}')


if __name__ == '__main__':
    main()
