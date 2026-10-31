#!/usr/bin/env python3
# Release decisions and interrupted publishing must be testable without writing to GitHub.
import argparse
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import release as ci

class ReleaseTests(unittest.TestCase):
    def test_draft_is_not_published(self):
        for draft in (False, True):
            with self.subTest(draft=draft), patch.object(ci, 'run', return_value=Mock(
                    returncode=0, stdout=json.dumps({'draft': draft}))):
                self.assertEqual(ci.is_published('owner/repo', 'v1.0.0-steps.1'), not draft)

    def test_api_failure_is_not_a_missing_release(self):
        with patch.object(ci, 'run', return_value=Mock(returncode=1, stderr='HTTP 503')):
            with self.assertRaises(SystemExit):
                ci.is_published('owner/repo', 'v1.0.0-steps.1')

    def test_highest_version_ignores_draft_and_prerelease(self):
        releases = [{'tag_name': tag, 'draft': draft, 'prerelease': prerelease} for tag, draft, prerelease in (
            ('v1.9.0', False, False), ('v1.10.0', False, False),
            ('v2.0.0', True, False), ('v3.0.0', False, True))]
        with patch.object(ci, 'list_releases', return_value=releases):
            self.assertEqual(ci.select_tag(''), 'v1.10.0')

    def test_moved_upstream_tag_is_rejected(self):
        def git(*args):
            if args[0] == 'tag':
                return 'v1.0.0-steps.1'
            if args[1].endswith('^{tree}'):
                return 'tree'
            return 'original-upstream'
        with patch.dict(os.environ, GITHUB_REPOSITORY='owner/repo'), \
                patch.object(ci, 'select_tag', return_value='v1.0.0'), \
                patch.object(ci, 'resolve_upstream', return_value='moved-upstream'), \
                patch.object(ci, 'make_candidate', return_value='candidate'), \
                patch.object(ci, 'run'), patch.object(ci, 'git', side_effect=git):
            with self.assertRaises(SystemExit):
                ci.plan(argparse.Namespace(tag=''))

    def publish(self, draft, valid_assets=True, newer_release=False):
        with tempfile.TemporaryDirectory() as directory:
            for name in ci.REQUIRED_LIBS:
                Path(directory, name).write_bytes(b'library')
            # A rerun must regenerate this file, not include its own previous hash.
            Path(directory, 'SHA256SUMS').write_text('stale')
            record = {'id': 1, 'tag_name': 'v1.0.0-steps.1', 'draft': draft, 'assets': []}
            releases = [record]
            if newer_release:
                releases.append({'tag_name': 'v2.0.0-steps.1', 'draft': False})
            def api(path, *args):
                if args:
                    return record
                record['assets'] = [dict(name=p.name, size=p.stat().st_size,
                                        digest=f'sha256:{ci.sha256(str(p))}') for p in Path(directory).iterdir()]
                if not valid_assets:
                    record['assets'][0]['digest'] = 'sha256:wrong'
                return record
            # For a public release, list_releases supplies its assets directly.
            api('metadata')
            env = dict(GITHUB_REPOSITORY='owner/repo', GITHUB_SERVER_URL='https://github.com',
                       GITHUB_SHA='control', GITHUB_RUN_ID='1')
            args = argparse.Namespace(dist=directory, upstream_tag='v1.0.0', upstream_sha='upstream',
                                      candidate='candidate', release_tag=record['tag_name'])
            with patch.dict(os.environ, env), patch.object(ci, 'candidate'), patch.object(ci, 'run') as run, \
                    patch.object(ci, 'git', return_value='patch'), \
                    patch.object(ci, 'list_releases', return_value=releases), patch.object(ci, 'gh_api', side_effect=api) as call:
                if not valid_assets:
                    with self.assertRaises(SystemExit):
                        ci.publish(args)
                else:
                    ci.publish(args)
                publish_calls = [c for c in call.call_args_list if 'draft=false' in c.args]
                if draft and valid_assets:
                    self.assertEqual(len(publish_calls), 1)
                    self.assertIn('make_latest=false' if newer_release else 'make_latest=true', publish_calls[0].args)
                    self.assertTrue(any(c.args[:3] == ('gh', 'release', 'upload') for c in run.call_args_list))
                else:
                    self.assertEqual(publish_calls, [])
                sums = Path(directory, 'SHA256SUMS').read_text()
                self.assertNotIn('  SHA256SUMS', sums)

    def test_resume_draft_without_taking_latest_from_newer_version(self):
        self.publish(draft=True, newer_release=True)

    def test_bad_upload_never_publishes(self):
        self.publish(draft=True, valid_assets=False)

    def test_public_release_with_changed_assets_fails_without_overwrite(self):
        self.publish(draft=False, valid_assets=False)

if __name__ == '__main__':
    unittest.main()
