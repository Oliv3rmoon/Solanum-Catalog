#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""tools/build_site.py and tools/qr.py without a network: the QR codes against Project Nayuki's reference
implementation, the icon pictures, and the pages that come out of a catalog."""
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import build_site  # noqa: E402
import qr  # noqa: E402

# Codes made by Project Nayuki's QR Code generator 1.8.0 (the C version Solanum draws its own QR codes with), byte
# mode, level M, the mask it picks: each module row after row, as hex. The text, its version and its size.
REFERENCE = [
    ('a', 1, 21, 'fe5bfc15906eb4bb7565dba4aec11907faafe0180082b674c0ee4b9ac0ab3ead3dff806417f9d3d04447ba74e5d2fa2e9777047e8fea930'),
    ('https://oliv3rmoon.github.io/Solanum-Catalog/apps/nightshade/', 4, 33, 'fe1f9b3fc103e5106eaca1ebb75a0a55dbaf03daec16ac4107faaaaafe01174a00be57523e56f23edb759deb22dfab97e9ed29242dc2e066db2e8dc499a4c19b7eec6a6f535c9a324e9b669c6116df226793c4fc253d5ce734d936fe762b9d38ca3cde8f0553f980795a457f87596ad05c13b1fbabc52fddd7a5966eeba5acd904d26c14feca63d10'),
    ('https://example.org/' + 'x' * 110, 8, 49, 'fe4446bba8bfc143b5c8afd06e8ec664546bb747a0a88c25dbabcebeeec2ec112211dff107faaaaaaaaafe0012245ddf00aa57b7fbbb09583a7308880b5184f2b11130e53f3b1ddffd2d3262dbbab1c22151588802b67a348111720ea0b455e65fd21e7e73b11b3c2221638dd80866fd9a94c930e8acdb15d5bd267a9d59bdf2c185e5398880743f9faf9107ef91327c5ddf129abab2bbbbac2c5a211888c753f623f9117ee69ec779dfd626e347a9bba94209d2a6848a3e1bda719555af3b162d489d736ae2330efad4bec303222890719e92bbb94af507bf75d95f29751131b9e944e38e22888a351d2a339017aee141375ddd72e280f3fbbafc004bd31888477f82c2a9116af0455fc5ffd12ba89affabbfc5d2f85684877aeb61879736e9045adaa65e22fec1b6711a4d8'),
]


def bits_of(modules):
    flat = ''.join('1' if cell else '0' for row in modules for cell in row)
    flat += '0' * (-len(flat) % 4)
    return ('%x' % int(flat, 2)).zfill(len(flat) // 4)


def svg_modules(svg):
    """The modules an SVG of qr.to_svg draws, quiet zone removed."""
    side = int(re.search(r'viewBox="0 0 (\d+) \d+"', svg).group(1))
    grid = [[False] * side for _ in range(side)]
    for x, y, n in re.findall(r'M(\d+) (\d+)h(\d+)v1h-\d+z', re.search(r'<path d="([^"]+)"', svg).group(1)):
        for i in range(int(n)):
            grid[int(y)][int(x) + i] = True
    return [row[4:side - 4] for row in grid[4:side - 4]]


class QrTest(unittest.TestCase):
    def test_codes_match_the_reference_implementation(self):
        for text, version, size, expected in REFERENCE:
            modules = qr.encode(text)
            self.assertEqual(len(modules), size, f'{text[:30]}: size')
            self.assertEqual(len(modules), version * 4 + 17)
            self.assertEqual(bits_of(modules), expected, f'{text[:30]}: modules differ from the reference')

    def test_every_mask_is_a_valid_code(self):
        # a code made with each mask differs from the others and keeps its finder patterns
        seen = set()
        for mask in range(8):
            modules = qr.encode('https://example.org/', mask)
            seen.add(bits_of(modules))
            self.assertTrue(all(modules[0][i] for i in range(7)) and all(modules[6][i] for i in range(7)))
        self.assertEqual(len(seen), 8)

    def test_capacity(self):
        self.assertEqual(len(qr.encode('x' * 14)), 21)       # version 1 holds 14 bytes at level M
        self.assertEqual(len(qr.encode('x' * 15)), 25)       # version 2
        self.assertEqual(len(qr.encode('x' * 213)), 57)      # version 10 holds 213
        with self.assertRaises(qr.TooLong):
            qr.encode('x' * 214)
        self.assertEqual(len(qr.encode('日本語')), 21)         # UTF-8 bytes, 9 of them

    def test_svg_draws_the_code(self):
        text = 'https://oliv3rmoon.github.io/Solanum-Catalog/apps/mandrake/'
        svg = qr.to_svg(text, 'a "label" <&>')
        self.assertEqual(svg_modules(svg), qr.encode(text))
        self.assertIn('aria-label="a &quot;label&quot; &lt;&amp;&gt;"', svg)
        self.assertNotIn('http', svg.replace('http://www.w3.org/2000/svg', ''))  # nothing external in the picture
        self.assertIn('fill="#fff"', svg)


def chunk(kind, body):
    return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xffffffff)


def png(width, height, depth, ctype, rows, extra=b'', interlace=0, filters=None):
    """A PNG of the given scanlines (bytes), each filtered with the type `filters` says (default 0)."""
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    step = max(1, channels * depth // 8)
    out, previous = b'', bytes(len(rows[0]))
    for y, line in enumerate(rows):
        kind = (filters or [0] * height)[y]
        filtered = bytearray()
        for i, value in enumerate(line):
            a = line[i - step] if i >= step else 0
            b = previous[i]
            c = previous[i - step] if i >= step else 0
            if kind == 0:
                predicted = 0
            elif kind == 1:
                predicted = a
            elif kind == 2:
                predicted = b
            elif kind == 3:
                predicted = (a + b) // 2
            else:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                predicted = a if pa <= pb and pa <= pc else b if pb <= pc else c
            filtered.append((value - predicted) & 255)
        out += bytes([kind]) + bytes(filtered)
        previous = line
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, depth, ctype, 0, 0, interlace)) +
            extra + chunk(b'IDAT', zlib.compress(out)) + chunk(b'IEND', b''))


class PngTest(unittest.TestCase):
    def test_every_filter_decodes(self):
        rows = [bytes((x * 37 + y * 11) % 256 for x in range(5 * 4)) for y in range(5)]
        for kind in range(5):
            width, height, got = build_site.decode_png(png(5, 5, 8, 6, rows, filters=[kind] * 5))
            self.assertEqual((width, height), (5, 5))
            self.assertEqual([bytes(r) for r in got], rows, f'filter {kind}')
        mixed = png(5, 5, 8, 6, rows, filters=[0, 1, 2, 3, 4])
        self.assertEqual([bytes(r) for r in build_site.decode_png(mixed)[2]], rows)

    def test_other_kinds_become_rgba(self):
        rgb = build_site.decode_png(png(2, 1, 8, 2, [bytes([1, 2, 3, 4, 5, 6])]))[2][0]
        self.assertEqual(bytes(rgb), bytes([1, 2, 3, 255, 4, 5, 6, 255]))
        gray = build_site.decode_png(png(2, 1, 8, 0, [bytes([10, 200])]))[2][0]
        self.assertEqual(bytes(gray), bytes([10, 10, 10, 255, 200, 200, 200, 255]))
        gray_alpha = build_site.decode_png(png(1, 1, 8, 4, [bytes([50, 99])]))[2][0]
        self.assertEqual(bytes(gray_alpha), bytes([50, 50, 50, 99]))
        sixteen = build_site.decode_png(png(1, 1, 16, 6, [bytes([1, 9, 2, 9, 3, 9, 4, 9])]))[2][0]
        self.assertEqual(bytes(sixteen), bytes([1, 2, 3, 4]))
        palette = chunk(b'PLTE', bytes([255, 0, 0, 0, 255, 0, 0, 0, 255])) + chunk(b'tRNS', bytes([255, 128]))
        indexed = build_site.decode_png(png(3, 1, 8, 3, [bytes([2, 1, 0])], extra=palette))[2][0]
        self.assertEqual(bytes(indexed), bytes([0, 0, 255, 255, 0, 255, 0, 128, 255, 0, 0, 255]))
        packed = build_site.decode_png(png(3, 1, 2, 3, [bytes([0b10010000])], extra=palette))[2][0]
        self.assertEqual(bytes(packed[:4]), bytes([0, 0, 255, 255]))
        self.assertEqual(bytes(packed[4:8]), bytes([0, 255, 0, 128]))

    def test_what_is_refused(self):
        rows = [bytes(4)] * 2
        for bad in (b'not a png', png(1, 2, 8, 6, rows, interlace=1), png(1, 2, 8, 6, rows)[:40], b''):
            with self.assertRaises((ValueError, zlib.error, struct.error)):
                build_site.decode_png(bad)
        huge = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 60000, 60000, 8, 6, 0, 0, 0)) +
                chunk(b'IDAT', zlib.compress(b'\0' * 100)) + chunk(b'IEND', b''))
        with self.assertRaises(ValueError):
            build_site.decode_png(huge)

    def test_shrinking_averages_blocks(self):
        # 256 x 256: the left half red, the right half blue: 128 x 128 keeps the halves, and a block's colors are averaged
        size = 256
        line = bytes([255, 0, 0, 255]) * (size // 2) + bytes([0, 0, 255, 255]) * (size // 2)
        width, height, rows = build_site.shrink(size, size, [bytearray(line)] * size)
        self.assertEqual((width, height), (128, 128))
        self.assertEqual(bytes(rows[5][:4]), bytes([255, 0, 0, 255]))
        self.assertEqual(bytes(rows[5][-4:]), bytes([0, 0, 255, 255]))
        # a one-pixel checkerboard of black and white is middle grey after
        black, white = bytes([0, 0, 0, 255]), bytes([255, 255, 255, 255])
        board = [bytearray(b''.join(black if (x + y) % 2 == 0 else white for x in range(size))) for y in range(size)]
        _, _, small = build_site.shrink(size, size, board)
        self.assertTrue(all(abs(value - 128) <= 1 for value in small[3][0:3]))

    def test_transparent_pixels_do_not_darken_the_edge(self):
        # an opaque white pixel next to a transparent black one: the color stays white, the alpha is half
        size = 256
        line = bytearray()
        for x in range(size):
            line += bytes([255, 255, 255, 255]) if x % 2 == 0 else bytes([0, 0, 0, 0])
        _, _, rows = build_site.shrink(size, size, [bytearray(line)] * size)
        self.assertEqual(tuple(rows[0][:4]), (255, 255, 255, 128))

    def test_small_and_odd_pictures(self):
        rows = [bytearray(bytes([9, 8, 7, 255]) * 64)] * 64
        self.assertEqual(build_site.shrink(64, 64, rows), (64, 64, rows))               # smaller than 128: left alone
        wide = [bytearray(bytes([1, 2, 3, 255]) * 300)] * 150
        width, height, got = build_site.shrink(300, 150, wide)                          # not a multiple: nearest pixels
        self.assertEqual((width, height), (128, 64))
        self.assertEqual(bytes(got[10][:4]), bytes([1, 2, 3, 255]))

    def test_the_icon_png_round_trips(self):
        source = png(256, 256, 8, 6, [bytes([200, 30, 40, 255]) * 256] * 256)
        small = build_site.small_icon(source)
        width, height, rows = build_site.decode_png(small)
        self.assertEqual((width, height), (128, 128))
        self.assertEqual(bytes(rows[64][64 * 4:64 * 4 + 4]), bytes([200, 30, 40, 255]))
        self.assertLess(len(small), 2000)


def catalog():
    return {
        'schema': 1, 'name': 'Solanum', 'about': 'The open catalog of PS5 homebrew <b>that</b> Solanum installs.',
        'apps': [
            {'id': 'zeta', 'name': 'Zeta Player', 'category': 'media', 'summary': 'Plays things.', 'title_id': 'PPSA00002',
             'repo': 'someone/zeta', 'asset': 'Zeta-{version}.zip', 'folder': 'PPSA00002', 'checksums': 'SHA256SUMS',
             'attested': False, 'license': 'GPL-3.0', 'author': 'someone', 'tags': ['media-player', 'video'],
             'release': {'tag': 'v1.2.3', 'asset': 'Zeta-1.2.3.zip', 'size': 45331110, 'sha256': 'ab' * 32,
                         'published_at': '2026-10-29T12:52:17Z', 'notes': 'ignored'}},
            {'id': 'alpha', 'name': 'Alpha', 'family': True, 'category': 'emulator', 'summary': 'An emulator.',
             'title_id': 'PPSA00001', 'repo': 'Oliv3rmoon/Alpha', 'asset': 'Alpha.zip', 'folder': 'PPSA00001',
             'checksums': 'SHA256SUMS', 'attested': True, 'license': 'MIT', 'author': 'Oliv3rmoon', 'tags': ['emulator'],
             'release': {'tag': 'v0.1.0', 'asset': 'Alpha.zip', 'size': 820000, 'sha256': 'cd' * 32,
                         'published_at': '2026-01-05T00:00:00Z', 'notes': ''}},
            {'id': 'evil', 'name': '<script>alert(1)</script>', 'category': 'tool', 'summary': '"><img src=x onerror=alert(2)>',
             'title_id': 'PPSA00003', 'repo': 'bad/repo" onclick="x', 'asset': 'e.zip', 'folder': '.',
             'checksums': 'SHA256SUMS', 'attested': False, 'license': 'GPL-3.0 <x>', 'tags': ['a"b']},
            {'id': 'zeta-fork', 'name': 'Zeta Fork', 'category': 'game', 'summary': 'Plays your own game.',
             'title_id': 'PPSA00002', 'shares': 'zeta', 'repo': 'other/zeta-fork', 'asset': 'ZF.zip',
             'folder': 'PPSA00002', 'checksums': 'SHA256SUMS', 'attested': False, 'license': 'none stated',
             'needs_game_files': True, 'channel': 'newest',
             'release': {'tag': 'v2.0.0-beta.1', 'asset': 'ZF.zip', 'size': 1000, 'sha256': '12' * 32,
                         'prerelease': True, 'published_at': '2026-10-29T12:52:17Z', 'notes': ''}},
            {'id': 'solanum', 'name': 'Solanum', 'family': True, 'category': 'store', 'summary': 'The store.',
             'title_id': 'PPSA76526', 'repo': 'Oliv3rmoon/Solanum', 'asset': 'PPSA76526.zip', 'folder': 'PPSA76526',
             'checksums': 'SHA256SUMS', 'attested': True, 'license': 'GPL-3.0-or-later',
             'release': {'tag': '0.2.0', 'asset': 'PPSA76526.zip', 'size': 17400000, 'sha256': 'ef' * 32,
                         'published_at': '2026-10-30T05:00:00Z', 'notes': ''}},
        ]}


class SiteTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.out = self.directory / 'site'
        self.icons = self.directory / 'icons'
        self.icons.mkdir()
        (self.icons / 'alpha.png').write_bytes(build_site.small_icon(png(256, 256, 8, 6, [bytes([9, 9, 9, 255]) * 256] * 256)))
        self.base = 'https://example.org/catalog'
        self.site = build_site.Site(catalog(), self.base, self.out, self.icons)
        self.site.write()

    def read(self, *path):
        return (self.out.joinpath(*path)).read_text(encoding='utf-8')

    def test_the_pages_are_there(self):
        for path in ('index.html', 'submit/index.html', 'get-solanum/index.html', '404.html', '.nojekyll',
                     'assets/style.css', 'assets/app.js', 'assets/favicon.svg', 'apps/zeta/index.html',
                     'apps/alpha/index.html', 'apps/evil/index.html', 'apps/solanum/index.html', 'icons/alpha.png'):
            self.assertTrue((self.out / path).is_file(), path)
        self.assertFalse((self.out / 'icons' / 'zeta.png').exists())

    def test_every_local_link_leads_somewhere(self):
        for page in self.out.rglob('*.html'):
            text = page.read_text(encoding='utf-8')
            for link in re.findall(r'(?:href|src)="([^"]+)"', text):
                if re.match(r'^(https?:|mailto:|#|/)', link):
                    continue
                target = (page.parent / link.split('#')[0].split('?')[0]).resolve()
                if target.is_dir():
                    target = target / 'index.html'
                self.assertTrue(target.is_file(), f'{page.relative_to(self.out)}: {link}')
                self.assertTrue(self.out.resolve() in target.parents, f'{page.relative_to(self.out)}: {link} leaves the site')

    def test_nothing_is_loaded_from_elsewhere(self):
        for page in self.out.rglob('*.html'):
            text = page.read_text(encoding='utf-8')
            self.assertNotRegex(text, r'<(?:script|img|iframe)[^>]+src="(?:https?:)?//', page.name)
            self.assertNotRegex(text, r'<link[^>]+rel="(?:stylesheet|icon|preload|modulepreload)"[^>]+href="(?:https?:)?//',
                                page.name)
            self.assertNotIn('@import', text)
        self.assertNotIn('url(http', self.read('assets', 'style.css'))
        self.assertNotRegex(self.read('assets', 'style.css'), r'url\(')

    def test_text_from_the_catalog_is_escaped(self):
        everything = ''.join(p.read_text(encoding='utf-8') for p in self.out.rglob('*.html'))
        self.assertNotIn('<script>alert', everything)
        self.assertNotIn('<img src=x', everything)
        self.assertNotIn('onclick="x', everything)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', self.read('apps', 'evil', 'index.html'))
        self.assertNotIn('github.com/bad/repo', everything)          # a repo that isn't owner/name is not linked
        self.assertNotIn('<b>that</b>', self.read('index.html'))
        self.assertEqual(everything.count('<script>'), len(list(self.out.rglob('*.html'))))  # only the one that sets .js

    def test_home_lists_the_apps_family_first(self):
        home = self.read('index.html')
        order = re.findall(r'<a href="apps/([a-z-]+)/">', home)
        self.assertEqual(order, ['alpha', 'solanum', 'evil', 'zeta-fork', 'zeta'])
        self.assertIn('data-category="media"', home)
        self.assertIn('#media-player', home)                              # a tag is searchable
        self.assertIn('ppsa00002', home)                                  # so is a title ID
        self.assertIn('<img class="icon" src="icons/alpha.png"', home)
        self.assertIn('class="icon tile"', home)                          # no icon: the initial on red
        self.assertIn('Nightshade family', home)
        self.assertIn('<strong>5</strong> apps', home)
        self.assertIn('>Media <span>1</span>', home)
        self.assertNotIn('>Streaming', home)                              # a category with no app has no chip
        self.assertIn('Needs your own game files</span>', home)
        self.assertIn('No license</span>', home)
        self.assertIn('<script src="assets/app.js" defer>', home)

    def test_an_app_page_says_what_the_pin_says(self):
        page = self.read('apps', 'zeta', 'index.html')
        for text in ('v1.2.3', '29 October 2026', '45.3 MB', 'Zeta-1.2.3.zip', 'GPL-3.0', 'someone', 'PPSA00002',
                     'ab' * 32, 'https://github.com/someone/zeta', 'https://github.com/someone/zeta/releases/tag/v1.2.3',
                     'https://spdx.org/licenses/GPL-3.0.html', '/data/homebrew/PPSA00002', '<svg', '#media-player'):
            self.assertIn(text, page)
        self.assertNotIn('ignored', page)                                 # the release's notes are not shown
        self.assertIn('Attested', self.read('apps', 'alpha', 'index.html'))
        self.assertIn('<img class="icon" src="../../icons/alpha.png"', self.read('apps', 'alpha', 'index.html'))
        unpinned = self.read('apps', 'evil', 'index.html')
        self.assertIn('Not pinned yet', unpinned)
        self.assertNotIn('spdx.org', unpinned)                            # an odd license is text, not a link

    def test_an_app_page_says_what_to_know_before_installing(self):
        fork = self.read('apps', 'zeta-fork', 'index.html')
        for text in ('No license: its repository states none', 'Needs your own game files', 'a pre-release',
                     'Pre-release</span>', 'Shares its title ID', '<a href="../zeta/">Zeta Player</a>',
                     'installing one replaces the other'):
            self.assertIn(text, fork)
        self.assertNotIn('spdx.org/licenses/none', fork)
        original = self.read('apps', 'zeta', 'index.html')
        self.assertIn('<a href="../zeta-fork/">Zeta Fork</a>', original)  # the original names its fork too
        self.assertNotIn('Needs your own game files', original)
        self.assertNotIn('Shares its title ID', self.read('apps', 'alpha', 'index.html'))

    def test_the_qr_code_is_the_pages_address(self):
        for app_id in ('zeta', 'alpha'):
            page = self.read('apps', app_id, 'index.html')
            self.assertEqual(svg_modules(re.search(r'<svg.*?</svg>', page, re.S).group(0)),
                             qr.encode(f'{self.base}/apps/{app_id}/'))
            self.assertIn(f'<link rel="canonical" href="{self.base}/apps/{app_id}/">', page)

    def test_submit_and_get_pages(self):
        submit = self.read('submit', 'index.html')
        for text in ('No license', 'title-ID folder', 'No game files', 'No backup tools', 'Needs your own game files',
                     'pull request', 'catalog.json', 'title_id', 'needs_game_files', 'channel', 'shares',
                     'edit/main/catalog.json', 'check_catalog.py'):
            self.assertIn(text, submit)
        get = self.read('get-solanum', 'index.html')
        for text in ('releases/latest/download/PPSA76526.zip', '0.2.0', '30 October 2026', '17.4 MB', 'ef' * 32,
                     '/data/homebrew/', 'gh attestation verify'):
            self.assertIn(text, get)

    def test_get_solanum_without_a_pinned_release_still_downloads(self):
        data = catalog()
        data['apps'] = [a for a in data['apps'] if a['id'] != 'solanum']
        out = self.directory / 'other'
        build_site.Site(data, self.base, out, self.icons).write()
        text = (out / 'get-solanum' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('releases/latest/download/PPSA76526.zip', text)
        self.assertNotIn('Latest release:', text)

    def test_the_404_page_uses_the_sites_own_path(self):
        text = self.read('404.html')
        self.assertIn('href="/catalog/assets/style.css"', text)
        self.assertIn('<a href="/catalog/">The apps are here.</a>', text)

    def test_it_will_not_empty_the_repository(self):
        with self.assertRaises(SystemExit):
            build_site.Site(catalog(), self.base, ROOT, self.icons).write()
        with self.assertRaises(SystemExit):
            build_site.Site(catalog(), self.base, ROOT.parent, self.icons).write()
        (self.directory / 'other').mkdir(exist_ok=True)
        (self.directory / 'other' / 'catalog.json').write_text('{}')
        with self.assertRaises(SystemExit):
            build_site.Site(catalog(), self.base, self.directory / 'other', self.icons).write()
        self.assertTrue((ROOT / 'catalog.json').exists())

    def test_the_real_catalog_builds(self):
        real = json.loads((ROOT / 'catalog.json').read_text(encoding='utf-8'))
        out = self.directory / 'real'
        build_site.Site(real, build_site.DEFAULT_BASE_URL, out, self.directory / 'none').write()
        for app in real['apps']:
            page = (out / 'apps' / app['id'] / 'index.html').read_text(encoding='utf-8')
            self.assertIn(app['title_id'], page)
            if app.get('release'):
                self.assertIn(app['release']['sha256'], page)
        self.assertEqual(len(list((out / 'apps').iterdir())), len(real['apps']))


class HelpersTest(unittest.TestCase):
    def test_dates_and_sizes(self):
        self.assertEqual(build_site.format_date('2026-10-30T11:24:50Z'), '30 October 2026')
        self.assertEqual(build_site.format_date('2026-01-05T00:00:00Z'), '5 January 2026')
        self.assertEqual(build_site.format_date(''), '')
        self.assertEqual(build_site.format_date('2026-13-01T00:00:00Z'), '')
        self.assertEqual(build_site.format_size(38563249), '38.6 MB')
        self.assertEqual(build_site.format_size(1_500_000_000), '1.5 GB')
        self.assertEqual(build_site.format_size(820_000), '820 KB')
        self.assertEqual(build_site.format_size(None), '')

    def test_the_default_address_follows_the_repository(self):
        import os
        old = os.environ.get('GITHUB_REPOSITORY')
        try:
            os.environ['GITHUB_REPOSITORY'] = 'Someone/Else-Catalog'
            self.assertEqual(build_site.default_base_url(), 'https://someone.github.io/Else-Catalog')
            os.environ['GITHUB_REPOSITORY'] = 'not a repository'
            self.assertEqual(build_site.default_base_url(), build_site.DEFAULT_BASE_URL)
        finally:
            if old is None:
                os.environ.pop('GITHUB_REPOSITORY', None)
            else:
                os.environ['GITHUB_REPOSITORY'] = old

    def test_the_icon_choice_prefers_the_ps5s(self):
        tree = {'tree': [{'type': 'blob', 'path': p} for p in (
            'sce_sys/icon0.png', 'ps4/sce_sys/icon0.png', 'ps5/sce_sys/icon0.png', 'art/icon0.png', 'README.md')]}
        calls = []

        def fake(url, token='', limit=0, accept=''):
            calls.append(url)
            return json.dumps(tree).encode()

        original = build_site.fetch
        build_site.fetch = fake
        try:
            self.assertEqual(build_site.icon_path({'repo': 'a/b', 'title_id': 'PPSA00001'}, ''), 'ps5/sce_sys/icon0.png')
        finally:
            build_site.fetch = original
        self.assertEqual(len(calls), 1)                                  # no param.json beside any: one request only

    def test_the_script_is_valid_javascript(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('needs node')
        subprocess.run([node, '--check', str(ROOT / 'web' / 'app.js')], check=True)


if __name__ == '__main__':
    unittest.main()
