import sys
from pathlib import Path
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'experiments/generalization_v1'),str(ROOT/'src'),str(ROOT)]
from evaluate_fresh_saved_cloud import IdentityAPI,SavedRunAPI
from fim_patch.cloud_official import GitHubAPI


class FreshCloudIdentityTests(unittest.TestCase):
    def test_binary_download_keyword_forwarded(self):
        api=IdentityAPI('demo/repo',dict(repo='new/lib',base_commit='a'*40),token='fixture')
        with patch.object(GitHubAPI,'request',return_value=b'archive') as request:
            self.assertEqual(api.request('actions/artifacts/1/zip',binary=True),b'archive')
            request.assert_called_once_with('actions/artifacts/1/zip',None,binary=True)

    def test_bound_identity_only_added_to_dispatch(self):
        api=IdentityAPI('demo/repo',dict(repo='new/lib',base_commit='a'*40),token='fixture')
        with patch.object(GitHubAPI,'request',return_value=None) as request:
            api.request('actions/workflows/w/dispatches',dict(ref='branch',inputs=dict(run_id='r')))
            self.assertEqual(request.call_args.args[1]['inputs']['expected_repo'],'new/lib')

    def test_saved_request_recovery_does_not_dispatch(self):
        api=SavedRunAPI('demo/repo',dict(repo='new/lib',base_commit='a'*40),token='fixture')
        with patch.object(GitHubAPI,'request') as request:
            self.assertIsNone(api.request('actions/workflows/w/dispatches',dict(inputs={})))
            request.assert_not_called()


if __name__=='__main__':unittest.main()
