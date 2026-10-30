#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""tools/pin_catalog.py without a network: what it pins, and what it refuses."""
import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import pin_catalog  # noqa: E402


def app_zip(title, files=None, folder=None):
    folder = title if folder is None else folder
    prefix = f'{folder}/' if folder else ''
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr(prefix + 'eboot.bin', b'program')
        archive.writestr(prefix + 'sce_sys/param.json',
                         json.dumps({'titleId': title, 'contentVersion': '01.002.000'}))
        for name, body in (files or {}).items():
            archive.writestr(name, body)
    return data.getvalue()


class FakeGitHub:
    """api.github.com and the release files, from memory."""

    def __init__(self, tag, files, digest=True, sums=None, stars=7):
        self.tag, self.files, self.stars = tag, files, stars
        self.digest, self.sums = digest, sums
        self.downloads = 0

    def get_json(self, url):
        if url.endswith('/releases/latest'):
            assets = [{'name': n, 'size': len(b), 'state': 'uploaded',
                       'digest': 'sha256:' + hashlib.sha256(b).hexdigest() if self.digest else None}
                      for n, b in self.files.items()]
            if self.sums is not None:
                assets.append({'name': 'SHA256SUMS', 'size': len(self.sums), 'state': 'uploaded'})
            return {'tag_name': self.tag, 'assets': assets, 'draft': False, 'prerelease': False,
                    'published_at': '2026-10-30T10:00:00Z', 'body': '## New\n- something'}
        return {'stargazers_count': self.stars}

    def download(self, url, path, limit):
        self.downloads += 1
        body = self.files[url.rsplit('/', 1)[1]]
        Path(path).write_bytes(body)
        return hashlib.sha256(body).hexdigest(), len(body)

    def request(self, url, accept=None):
        return io.BytesIO(self.sums.encode())


def entry(**more):
    app = {'id': 'thing', 'name': 'Thing', 'category': 'tool', 'summary': 's', 'title_id': 'ABCD12345',
           'repo': 'someone/thing', 'asset': 'Thing-{version}.zip', 'folder': 'ABCD12345', 'checksums': 'SHA256SUMS',
           'attested': False, 'license': 'MIT'}
    app.update(more)
    return app


class PinTest(unittest.TestCase):
    def pin(self, github, app):
        lines = []
        with mock.patch.object(pin_catalog, 'get_json', github.get_json), \
                mock.patch.object(pin_catalog, 'download', github.download), \
                mock.patch.object(pin_catalog, 'request', github.request), \
                tempfile.TemporaryDirectory() as work:
            changed = pin_catalog.pin_app(app, work, lines.append)
        return changed, lines

    def test_names(self):
        self.assertEqual(pin_catalog.pattern_name('Thing-{version}.zip', 'v1.2.3'), 'Thing-1.2.3.zip')
        self.assertEqual(pin_catalog.pattern_name('Jelly5-{content}.zip', 'v0.7.0'), 'Jelly5-00.007.000.zip')
        self.assertEqual(pin_catalog.pattern_name('PPSA99003.zip', '01.002.001'), 'PPSA99003.zip')
        self.assertIsNone(pin_catalog.pattern_name('Jelly5-{content}.zip', 'nightly'))
        self.assertIsNone(pin_catalog.pattern_name('../{version}.zip', '1.0'))

    def test_notes_are_cut_to_what_solanum_keeps(self):
        long = '\n'.join('- line %d with some words' % i for i in range(400))
        cut = pin_catalog.cut_notes(long)
        self.assertLessEqual(len(cut.encode()), pin_catalog.NOTES_BYTES)
        self.assertTrue(cut.endswith('words'))
        self.assertLessEqual(len(pin_catalog.cut_notes('日本語' * 2000).encode()), pin_catalog.NOTES_BYTES)

    def test_sums(self):
        sha = 'a' * 64
        self.assertEqual(pin_catalog.sums_line(f'{sha}  Thing.zip\n{"b" * 64} *Other.zip\n', 'Thing.zip'), sha)
        self.assertIsNone(pin_catalog.sums_line(f'{sha}  Other.zip\n', 'Thing.zip'))
        with self.assertRaises(ValueError):
            pin_catalog.sums_line(f'{sha}  Thing.zip\n{sha}  Thing.zip\n', 'Thing.zip')

    def test_the_zip_must_hold_the_app_and_no_games(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'a.zip'
            path.write_bytes(app_zip('ABCD12345', folder='Thing-v1/ABCD12345'))
            self.assertEqual(pin_catalog.inspect_zip(path, 'ABCD12345'), '01.002.000')
            path.write_bytes(app_zip('ABCD12345', folder=''))
            self.assertEqual(pin_catalog.inspect_zip(path, 'ABCD12345'), '01.002.000')
            path.write_bytes(app_zip('ABCD12345', {'ABCD12345.ffpfsc': b'its own image'}))
            self.assertEqual(pin_catalog.inspect_zip(path, 'ABCD12345'), '01.002.000')
            for files, why in (({'ABCD12345/roms/game.sfc': b'x'}, 'ROMs'), ({'keys/prod.keys': b'x'}, 'keys'),
                               ({'Other.pkg': b'x'}, 'packages'), ({'game.iso': b'x'}, 'images')):
                path.write_bytes(app_zip('ABCD12345', files))
                with self.assertRaises(ValueError, msg=why):
                    pin_catalog.inspect_zip(path, 'ABCD12345')
            path.write_bytes(app_zip('WXYZ00001'))
            with self.assertRaisesRegex(ValueError, 'not ABCD12345'):
                pin_catalog.inspect_zip(path, 'ABCD12345')

    def test_a_new_release_is_pinned_and_the_old_one_kept(self):
        body = app_zip('ABCD12345')
        app = entry(release={'tag': 'v1.0.0', 'asset': 'Thing-1.0.0.zip', 'size': 10, 'sha256': 'c' * 64,
                             'published_at': 'x', 'notes': ''})
        github = FakeGitHub('v1.1.0', {'Thing-1.1.0.zip': body},
                            sums=f'{hashlib.sha256(body).hexdigest()}  Thing-1.1.0.zip\n')
        changed, _ = self.pin(github, app)
        self.assertTrue(changed)
        self.assertEqual(app['release']['tag'], 'v1.1.0')
        self.assertEqual(app['release']['sha256'], hashlib.sha256(body).hexdigest())
        self.assertEqual(app['release']['size'], len(body))
        self.assertEqual(app['pins'][0]['tag'], 'v1.0.0')
        self.assertEqual(app['release']['content_version'], '01.002.000')
        self.assertEqual(app['stars'], 7)
        self.assertNotIn('published_at', app['pins'][0])

    def test_a_pin_without_its_content_version_is_checked_again_and_given_it(self):
        body = app_zip('ABCD12345')
        sha = hashlib.sha256(body).hexdigest()
        app = entry(stars=7, release={'tag': 'v1.1.0', 'asset': 'Thing-1.1.0.zip', 'size': len(body), 'sha256': sha,
                                      'published_at': '2026-10-30T10:00:00Z', 'notes': '## New\n- something'})
        github = FakeGitHub('v1.1.0', {'Thing-1.1.0.zip': body}, digest=False)
        changed, _ = self.pin(github, app)
        self.assertTrue(changed)
        self.assertEqual(app['release']['content_version'], '01.002.000')
        self.assertEqual(github.downloads, 1)
        replaced = entry(release={'tag': 'v1.1.0', 'asset': 'Thing-1.1.0.zip', 'size': len(body), 'sha256': 'c' * 64})
        with self.assertRaisesRegex(pin_catalog.Problem, 'replaced'):
            self.pin(FakeGitHub('v1.1.0', {'Thing-1.1.0.zip': body}, digest=False), replaced)

    def test_an_older_pinned_release_that_comes_back_must_be_the_same_file(self):
        body = app_zip('ABCD12345')
        app = entry(release={'tag': 'v1.2.0', 'asset': 'Thing-1.2.0.zip', 'size': 10, 'sha256': 'c' * 64,
                             'content_version': '01.002.000'},
                    pins=[{'tag': 'v1.1.0', 'asset': 'Thing-1.1.0.zip', 'size': len(body), 'sha256': 'd' * 64}])
        with self.assertRaisesRegex(pin_catalog.Problem, 'replaced'):
            self.pin(FakeGitHub('v1.1.0', {'Thing-1.1.0.zip': body}, digest=False), app)
        self.assertEqual(app['pins'][0]['sha256'], 'd' * 64)
        self.assertEqual(app['release']['tag'], 'v1.2.0')

    def test_a_pinned_release_is_not_downloaded_again(self):
        body = app_zip('ABCD12345')
        sha = hashlib.sha256(body).hexdigest()
        app = entry(stars=7, release={'tag': 'v1.1.0', 'asset': 'Thing-1.1.0.zip', 'size': len(body), 'sha256': sha,
                                      'content_version': '01.002.000',
                                      'published_at': '2026-10-30T10:00:00Z', 'notes': '## New\n- something'})
        github = FakeGitHub('v1.1.0', {'Thing-1.1.0.zip': body})
        changed, _ = self.pin(github, app)
        self.assertFalse(changed)
        self.assertEqual(github.downloads, 0)

    def test_a_file_replaced_after_it_was_pinned_is_an_error(self):
        body = app_zip('ABCD12345')
        app = entry(release={'tag': 'v1.1.0', 'asset': 'Thing-1.1.0.zip', 'size': len(body), 'sha256': 'c' * 64,
                             'published_at': 'x', 'notes': ''})
        with self.assertRaisesRegex(pin_catalog.Problem, 'replaced'):
            self.pin(FakeGitHub('v1.1.0', {'Thing-1.1.0.zip': body}), app)
        self.assertEqual(app['release']['sha256'], 'c' * 64)

    def test_a_checksum_file_that_disagrees_is_an_error(self):
        body = app_zip('ABCD12345')
        app = entry()
        with self.assertRaisesRegex(pin_catalog.Problem, 'SHA256SUMS gives'):
            self.pin(FakeGitHub('v1.1.0', {'Thing-1.1.0.zip': body}, digest=False, sums=f'{"d" * 64}  Thing-1.1.0.zip\n'),
                     app)
        self.assertNotIn('release', app)

    def test_a_release_of_another_app_is_an_error(self):
        app = entry()
        with self.assertRaisesRegex(pin_catalog.Problem, "isn't the app"):
            self.pin(FakeGitHub('v1.1.0', {'Thing-1.1.0.zip': app_zip('WXYZ00001')}), app)
        self.assertNotIn('release', app)

    def test_a_release_without_the_file_keeps_its_pin(self):
        app = entry(release={'tag': 'v1.0.0', 'asset': 'Thing-1.0.0.zip', 'size': 10, 'sha256': 'c' * 64})
        changed, lines = self.pin(FakeGitHub('v1.1.0', {'Other.zip': b'x'}), app)
        self.assertTrue(any('keeps its pin' in line for line in lines))
        self.assertEqual(app['release']['tag'], 'v1.0.0')


if __name__ == '__main__':
    unittest.main()
