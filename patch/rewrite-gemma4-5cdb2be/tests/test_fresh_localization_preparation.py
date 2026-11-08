import sys
from pathlib import Path
import unittest
import tempfile
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'experiments/generalization_v1'),str(ROOT/'src'),str(ROOT)]
from prepare_fresh_localization import cache_path,cached,localization_ticket
from localize_prospective import git,make_thin_snapshot
from utils.fault_localization import build_bug_report_text


class FreshLocalizationPreparationTests(unittest.TestCase):
    def test_public_schema_alias_supplies_existing_fl_query_without_answers(self):
        task=dict(repo='demo/lib',problem_statement='parse should preserve empty fields',patch='SECRET')
        result=localization_ticket(task)
        self.assertEqual(result['bug_report'],task['problem_statement'])
        self.assertEqual(result['problem_statement'],task['problem_statement'])
        self.assertIn(task['problem_statement'],build_bug_report_text(result))
        self.assertNotIn('patch',result)
        self.assertEqual(localization_ticket(dict(task,bug_report='existing'))['bug_report'],'existing')

    def test_identity_paths_and_commit_validation(self):
        ticket=dict(repo='new/library',base_commit='a'*40)
        self.assertEqual(cache_path(ROOT/'runs/demo',ticket).name,'new__library')
        for change in ({'repo':'../outside'},{'repo':'org/lib/extra'},{'base_commit':'main'}):
            with self.assertRaises(ValueError):cache_path(ROOT/'runs/demo',dict(ticket,**change))

    def test_missing_cache_never_fetches(self):
        with patch.object(Path,'is_dir',return_value=False),patch('prepare_fresh_localization.git') as git:
            self.assertFalse(cached(ROOT/'runs/missing','a'*40))
            git.assert_not_called()

    def test_cached_head_must_be_exact_base(self):
        with patch.object(Path,'is_dir',return_value=True),patch('prepare_fresh_localization.git',side_effect=[b'',b'b'*40]):
            self.assertFalse(cached(ROOT/'runs/cache','a'*40))

    def test_depth_one_object_cache_supports_existing_thin_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'fixture';source.mkdir()
            git(source,'init')
            (source/'parser.py').write_text('def parse(value):\n    return value\n',encoding='utf8')
            git(source,'add','.')
            git(source,'-c','user.name=T','-c','user.email=t@example.invalid','commit','-m','fixture')
            base=git(source,'rev-parse','HEAD').decode().strip()
            cache=root/'cache';cache.mkdir();git(cache,'init')
            git(cache,'fetch','--depth=1','--no-tags',str(source),base)
            git(cache,'update-ref','--no-deref','HEAD',base)
            self.assertTrue((cache/'.git/shallow').is_file())
            result=make_thin_snapshot(cache,dict(repo='demo/lib',base_commit=base),root/'snapshot',['parser.py'])
            self.assertEqual(result['source_files'],1)
            self.assertEqual(git(root/'snapshot','rev-parse','HEAD').decode().strip(),base)


if __name__=='__main__':unittest.main()
