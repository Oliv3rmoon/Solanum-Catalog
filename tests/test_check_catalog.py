#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""tools/check_catalog.py's offline checks: what a catalog may say, and what it may not."""
import contextlib
import copy
import hashlib
import io
import json
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import check_catalog  # noqa: E402


def app(**more):
    entry = {'id': 'thing', 'name': 'Thing', 'category': 'tool', 'summary': 's', 'title_id': 'ABCD12345',
             'repo': 'someone/thing', 'asset': 'Thing-{version}.zip', 'folder': 'ABCD12345',
             'checksums': 'SHA256SUMS', 'attested': False, 'license': 'MIT'}
    entry.update(more)
    return entry


def png(width, height, extra=b''):
    """A small RGB PNG of width x height, made here (no imaging library on the CI runner)."""
    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body))
    rows = b''.join(b'\0' + b'\x80\x10\x20' * width for _ in range(height))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(rows)) + extra + chunk(b'IEND', b''))


def icon_of(data, path='icons/thing.png'):
    return {'path': path, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}


class OfflineTest(unittest.TestCase):
    def check(self, *apps, root=None):
        catalog = {'schema': 1, 'apps': [copy.deepcopy(a) for a in apps]}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            try:
                check_catalog.check_offline(catalog, **({'root': root} if root else {}))
            except SystemExit:
                return out.getvalue()
        return None

    def test_an_icon_is_optional_and_must_be_the_file_it_names(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'icons').mkdir()
            good = png(256, 256)
            (Path(root) / 'icons/thing.png').write_bytes(good)
            self.assertIsNone(self.check(app(), root=root))
            self.assertIsNone(self.check(app(icon=icon_of(good)), root=root))
            # the file it names: there, a PNG, at most 256x256, under 128 KiB, of the entry's size and SHA-256
            self.assertIn('not in the repository', self.check(app(icon=icon_of(good, 'icons/other.png')), root=root))
            self.assertIn('hashes to', self.check(app(icon=dict(icon_of(good), sha256='0' * 64)), root=root))
            self.assertIn('bytes, the entry says', self.check(app(icon=dict(icon_of(good), size=len(good) - 1)),
                                                              root=root))
            for name, data, why in (('big.png', png(257, 16), 'at most 256x256'),
                                    ('tall.png', png(16, 300), 'at most 256x256'),
                                    ('jpeg.png', b'\xff\xd8\xff\xe0' + bytes(64), 'not a PNG'),
                                    ('cut.png', good[:-20], 'not a PNG'),
                                    ('crc.png', good[:40] + bytes([good[40] ^ 1]) + good[41:], 'not a PNG')):
                (Path(root) / 'icons' / name).write_bytes(data)
                self.assertIn(why, self.check(app(icon=icon_of(data, 'icons/' + name)), root=root), name)
            # under 128 KiB, whatever the entry says
            heavy = png(256, 256, b'\0\x02\0\0zTXt' + bytes(0x20000) + struct.pack('>I', zlib.crc32(
                b'zTXt' + bytes(0x20000))))
            (Path(root) / 'icons/heavy.png').write_bytes(heavy)
            self.assertIn('not a size under', self.check(app(icon=icon_of(heavy, 'icons/heavy.png')), root=root))
            self.assertIn('must be under', self.check(app(icon=dict(icon_of(heavy, 'icons/heavy.png'), size=1000)),
                                                      root=root))
            # the path: icons/<lowercase name>.png, nothing outside icons/
            for path in ('thing.png', 'icons/Thing.png', 'icons/../catalog.json', 'icons/sub/thing.png',
                         'icons/thing.jpg', '/icons/thing.png', 'icons/' + 'a' * 90 + '.png'):
                self.assertIn('icon path', self.check(app(icon=dict(icon_of(good), path=path)), root=root), path)
            # nothing but path, sha256 and size
            self.assertIn('nothing else', self.check(app(icon=dict(icon_of(good), url='https://x')), root=root))
            self.assertIn('nothing else', self.check(app(icon='icons/thing.png'), root=root))
            self.assertIn('size', self.check(app(icon=dict(icon_of(good), size=True)), root=root))

    def test_the_real_catalogs_icons_are_their_files(self):
        catalog = json.loads((ROOT / 'catalog.json').read_text(encoding='utf-8'))
        with_icons = [a for a in catalog['apps'] if 'icon' in a]
        self.assertGreaterEqual(len(with_icons), 6)
        for entry in with_icons:
            data = (ROOT / entry['icon']['path']).read_bytes()
            self.assertEqual(check_catalog.png_size(data), (256, 256), entry['id'])
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry['icon']['sha256'])

    def test_a_plain_app_passes(self):
        self.assertIsNone(self.check(app()))

    def test_the_new_fields(self):
        self.assertIsNone(self.check(app(license='none stated', needs_game_files=True, channel='newest')))
        self.assertIn('needs_game_files', self.check(app(needs_game_files='yes')))
        self.assertIn('channel', self.check(app(channel='beta')))
        pinned = {'tag': 'v1.0.0', 'asset': 'Thing-1.0.0.zip', 'size': 10, 'sha256': 'a' * 64,
                  'title_name': 'Thing', 'prerelease': True}
        self.assertIsNone(self.check(app(release=pinned)))
        self.assertIn('prerelease', self.check(app(release=dict(pinned, prerelease='yes'))))
        self.assertIn('title_name', self.check(app(release=dict(pinned, title_name='x' * 64))))

    def test_a_title_id_is_one_apps_unless_a_later_one_shares_it(self):
        first, fork = app(), app(id='fork', repo='other/fork')
        self.assertIn('already belongs', self.check(first, fork))
        self.assertIsNone(self.check(first, dict(fork, shares='thing')))
        self.assertIsNone(self.check(first, dict(fork, shares='thing'), dict(fork, id='fork2', shares='thing')))
        # it must name the first app of that title ID, listed before it
        self.assertIn('shares', self.check(dict(fork, shares='thing'), first))
        self.assertIn('shares', self.check(first, dict(fork, shares='nobody')))
        self.assertIn('shares', self.check(first, dict(fork, shares='thing', title_id='WXYZ00001')))
        other = app(id='other', title_id='WXYZ00001')
        self.assertIn('shares', self.check(first, other, dict(fork, shares='other')))

    def test_the_real_catalog_passes(self):
        catalog = json.loads((ROOT / 'catalog.json').read_text(encoding='utf-8'))
        with contextlib.redirect_stdout(io.StringIO()):
            apps = check_catalog.check_offline(catalog)
        self.assertEqual(len(apps), len(catalog['apps']))

    def test_the_newest_channel_reads_the_list(self):
        releases = [{'tag_name': 'v2.0.0', 'draft': True, 'assets': [{'name': 'Thing-2.0.0.zip'}]},
                    {'tag_name': 'v1.9.0', 'draft': False, 'prerelease': False, 'assets': [{'name': 'Other.zip'}]},
                    {'tag_name': 'v1.8.0', 'draft': False, 'prerelease': True, 'assets': [{'name': 'Thing-1.8.0.zip'}]}]
        with mock.patch.object(check_catalog, 'get', lambda url: json.dumps(releases)):
            self.assertEqual(check_catalog.newest_release(app(channel='newest'))['tag_name'], 'v1.8.0')
        with mock.patch.object(check_catalog, 'get', lambda url: json.dumps(releases[:2])), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            check_catalog.newest_release(app(channel='newest'))


if __name__ == '__main__':
    unittest.main()
