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


def app_zip(title, files=None, folder=None, name=None):
    folder = title if folder is None else folder
    prefix = f'{folder}/' if folder else ''
    param = {'titleId': title, 'contentVersion': '01.002.000'}
    if name:
        param['localizedParameters'] = {'defaultLanguage': 'en-US', 'en-US': {'titleName': name}}
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr(prefix + 'eboot.bin', b'program')
        archive.writestr(prefix + 'sce_sys/param.json', json.dumps(param))
        for name, body in (files or {}).items():
            archive.writestr(name, body)
    return data.getvalue()


class FakeGitHub:
    """api.github.com and the release files, from memory."""

    def __init__(self, tag, files, digest=True, sums=None, stars=7, releases=None):
        self.tag, self.files, self.stars = tag, files, stars
        self.digest, self.sums = digest, sums
        self.downloads = 0
        # the list (/releases), newest first: (tag, file names, prerelease, draft); the files' bodies are in `files`
        self.releases = releases or []

    def release(self, tag, names, prerelease=False, draft=False):
        assets = [{'name': n, 'size': len(self.files[n]), 'state': 'uploaded',
                   'digest': 'sha256:' + hashlib.sha256(self.files[n]).hexdigest() if self.digest else None}
                  for n in names]
        if self.sums is not None:
            assets.append({'name': 'SHA256SUMS', 'size': len(self.sums), 'state': 'uploaded'})
        return {'tag_name': tag, 'assets': assets, 'draft': draft, 'prerelease': prerelease,
                'published_at': '2026-10-30T10:00:00Z', 'body': '## New\n- something'}

    def get_json(self, url):
        if url.endswith('/releases/latest'):
            return self.release(self.tag, list(self.files))
        if '/releases?' in url:
            return [self.release(*r) for r in self.releases]
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


    def test_newest_channel_takes_the_newest_release_with_the_file(self):
        body, other = app_zip('ABCD12345', name='Thing'), b'windows build'
        files = {'Thing-1.3.0.zip': body, 'Thing-1.2.0.zip': app_zip('ABCD12345'), 'Thing-Windows-1.4.0.zip': other,
                 'Thing-1.5.0.zip': app_zip('ABCD12345')}
        github = FakeGitHub('v1.4.0', files, releases=[
            ('v1.5.0', ['Thing-1.5.0.zip'], False, True),        # a draft: never
            ('v1.4.0', ['Thing-Windows-1.4.0.zip']),             # another platform's release
            ('v1.3.0', ['Thing-1.3.0.zip'], True),               # a pre-release with the file: this one
            ('v1.2.0', ['Thing-1.2.0.zip'])])
        app = entry(channel='newest')
        changed, _ = self.pin(github, app)
        self.assertTrue(changed)
        self.assertEqual(app['release']['tag'], 'v1.3.0')
        self.assertTrue(app['release']['prerelease'])
        self.assertEqual(app['release']['title_name'], 'Thing')
        # without the channel, GitHub's latest release decides, and it has no file of the pattern
        plain = entry()
        changed, lines = self.pin(github, plain)
        self.assertNotIn('release', plain)
        self.assertTrue(any('keeps its pin' in line for line in lines))

    def test_newest_channel_with_no_release_holding_the_file_keeps_its_pin(self):
        github = FakeGitHub('v1.0.0', {'Other.zip': b'x'}, releases=[('v1.0.0', ['Other.zip'], True)])
        app = entry(channel='newest', release={'tag': 'v0.9.0', 'asset': 'Thing-0.9.0.zip', 'size': 10,
                                               'sha256': 'c' * 64})
        changed, lines = self.pin(github, app)
        self.assertEqual(app['release']['tag'], 'v0.9.0')
        self.assertTrue(any('keeps its pin' in line for line in lines))

    def test_an_app_sharing_its_title_id_is_given_its_name(self):
        body = app_zip('ABCD12345', name='Thing Plus')
        sha = hashlib.sha256(body).hexdigest()
        pinned = {'tag': 'v1.1.0', 'asset': 'Thing-1.1.0.zip', 'size': len(body), 'sha256': sha,
                  'content_version': '01.002.000', 'published_at': '2026-10-30T10:00:00Z',
                  'notes': '## New\n- something'}
        app = entry(stars=7, release=dict(pinned))
        github = FakeGitHub('v1.1.0', {'Thing-1.1.0.zip': body})
        changed, _ = self.pin(github, app)
        self.assertFalse(changed)  # not sharing: not downloaded for a name
        self.assertEqual(github.downloads, 0)
        with mock.patch.object(pin_catalog, 'get_json', github.get_json), \
                mock.patch.object(pin_catalog, 'download', github.download), \
                mock.patch.object(pin_catalog, 'request', github.request), \
                tempfile.TemporaryDirectory() as work:
            changed = pin_catalog.pin_app(app, work, lambda line: None, names=True)
        self.assertTrue(changed)
        self.assertEqual(app['release']['title_name'], 'Thing Plus')
        self.assertEqual(github.downloads, 1)
        self.assertEqual(pin_catalog.sharing([entry(id='a'), entry(id='b', shares='a'), entry(id='c')]), {'a', 'b'})

    def test_title_names(self):
        self.assertEqual(pin_catalog.title_name({'localizedParameters': {'defaultLanguage': 'ja-JP', 'en-US': {
            'titleName': 'Thing'}, 'ja-JP': {'titleName': 'シング'}}}), 'シング')
        self.assertEqual(pin_catalog.title_name({'localizedParameters': {'en-US': {'titleName': ' Thing '}}}), 'Thing')
        self.assertEqual(pin_catalog.title_name({'titleId': 'ABCD12345'}), '')


if __name__ == '__main__':
    unittest.main()
