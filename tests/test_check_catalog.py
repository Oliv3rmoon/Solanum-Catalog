#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""tools/check_catalog.py's offline checks: what a catalog may say, and what it may not."""
import contextlib
import copy
import io
import json
import sys
import unittest
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


class OfflineTest(unittest.TestCase):
    def check(self, *apps):
        catalog = {'schema': 1, 'apps': [copy.deepcopy(a) for a in apps]}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            try:
                check_catalog.check_offline(catalog)
            except SystemExit:
                return out.getvalue()
        return None

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
