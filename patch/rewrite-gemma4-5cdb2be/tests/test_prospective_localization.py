"""Real deterministic FL and local shared-object snapshots on synthetic Git repos."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'experiments/generalization_v1'),str(ROOT/'src'),str(ROOT)]
import localize_prospective as module
from core import dump,write_rows,sha
from fim_patch.pipeline import validate_localization
from fim_patch.generator import source_for

class ProspectiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.cache=self.root/'cache';self.repo=self.cache/'demo__project';self.repo.mkdir(parents=True)
        module.git(self.repo,'init')
        (self.repo/'parser.py').write_text('def parse(value, delimiter=None):\n    parsed = value.split(",")\n    return parsed\n',encoding='utf8')
        (self.repo/'tests').mkdir();(self.repo/'tests/test_secret.py').write_text('SECRET_TEST_ORACLE',encoding='utf8')
        (self.repo/'notes.txt').write_text('Not indexed',encoding='utf8')
        module.git(self.repo,'add','.');module.git(self.repo,'-c','user.name=T','-c','user.email=t@example.invalid','commit','-m','fixture')
        self.base=module.git(self.repo,'rev-parse','HEAD').decode().strip()
        self.ticket=dict(ticket_id='demo__project-777',repo='demo/project',base_commit=self.base,
                         title='parse ignores delimiter argument',description='parse should split input with delimiter when provided.')

    def test_export_reads_base_commit_not_dirty_working_tree_or_tests(self):
        (self.repo/'parser.py').write_text('def parse():\n    return "DIRTY"\n',encoding='utf8')
        target=self.root/'export';target.mkdir()
        result=module.export_index_sources(self.repo,self.base,target)
        self.assertEqual(result['files'],1)
        self.assertIn('value.split',(target/'parser.py').read_text())
        self.assertFalse((target/'tests').exists())
        self.assertFalse((target/'notes.txt').exists())
        self.assertIn('DIRTY',(self.repo/'parser.py').read_text())

    def test_thin_snapshot_is_clean_exact_and_shared_without_rewriting_cache(self):
        destination=self.root/'snapshot'
        info=module.make_thin_snapshot(self.repo,self.ticket,destination,['parser.py'])
        self.assertEqual(module.git(destination,'status','--porcelain'),b'')
        self.assertEqual(module.git(destination,'rev-parse','HEAD').decode().strip(),self.base)
        self.assertTrue((destination/'.git/objects/info/alternates').exists())
        self.assertEqual((destination/'parser.py').read_bytes(),module.git(self.repo,'show',self.base+':parser.py'))
        self.assertFalse((destination/'tests').exists())
        self.assertEqual(info['source_files'],1)
        self.assertEqual(module.git(self.repo,'rev-parse','HEAD').decode().strip(),self.base)

    def test_unsafe_and_windows_reserved_paths_are_refused(self):
        for path in ('../outside.py','/outside.py','C:/outside.py','a\\b.py','con.py'):
            with self.assertRaises(ValueError):module.safe_path(self.root,path)

    def test_snapshot_uses_old_base_without_resetting_newer_cache_head(self):
        original=(self.repo/'parser.py').read_bytes()
        (self.repo/'parser.py').write_text('def parse(value):\n    return "NEW"\n')
        module.git(self.repo,'add','parser.py');module.git(self.repo,'-c','user.name=T','-c','user.email=t@example.invalid','commit','-m','newer')
        newer=module.git(self.repo,'rev-parse','HEAD')
        destination=self.root/'old-base'
        module.make_thin_snapshot(self.repo,self.ticket,destination,['parser.py'])
        self.assertEqual((destination/'parser.py').read_bytes(),original)
        self.assertEqual(module.git(self.repo,'rev-parse','HEAD'),newer)
        self.assertIn('NEW',(self.repo/'parser.py').read_text())

    def test_adapter_preserves_low_confidence_and_ranking(self):
        from types import SimpleNamespace
        original=dict(method=dict(module.FROZEN_RETRIEVAL_METHOD),confidence_level='low',should_manual_review=True,
            recommend_patch_generation=False,stage3_ranked_symbols=[{'file_path':'parser.py','symbol_qualified_name':'parse','start_line':1,'end_line':3}])
        index=SimpleNamespace(settings={'file_fingerprints':{'parser.py':'a'*64}})
        loc=module.attach_location(original,index,self.ticket)
        for field in ('confidence_level','should_manual_review','recommend_patch_generation','stage3_ranked_symbols'):
            self.assertEqual(loc[field],original[field])

    def test_real_existing_fl_produces_stage3_and_source_hash_with_zero_network(self):
        prepared=self.root/'prepared';prepared.mkdir();write_rows(prepared/'prospective_public_tickets.jsonl',[self.ticket])
        dump(prepared/'cohort_manifest.json',{'selected_ids':[self.ticket['ticket_id']]})
        dump(prepared/'pilot_manifest.pending.json',dict(schema_version=1,model={'model':'mock','expected_digest':'pin'},policy={},validation={'kind':'none'}))
        dump(prepared/'freeze.json',dict(implementation_sha256={},prepared_artifact_sha256={}))
        output=self.root/'out'
        with patch.object(sys,'argv',['localize','--prepared',str(prepared),'--repo-cache',str(self.cache),'--output',str(output)]), \
             patch('urllib.request.urlopen',side_effect=AssertionError('network')):
            module.main()
        summary=json.loads((output/'summary.json').read_text(encoding='utf8'))
        self.assertEqual(summary['localized_and_source_validated'],1,summary)
        loc=json.loads((output/'localizations.jsonl').read_text(encoding='utf8'))
        self.assertTrue(loc['stage3_ranked_symbols'])
        snapshot=Path(json.loads((output/'records.json').read_text())[0]['snapshot'])
        self.assertEqual(loc['source_file_sha256']['parser.py'],sha((snapshot/'parser.py').read_bytes()))
        self.assertIn(loc['confidence_level'],('low','medium','high'))
        self.assertNotIn('SECRET_TEST_ORACLE',json.dumps(loc))
        validate_localization(self.ticket,loc,snapshot)
        self.assertEqual(summary['model_calls'],0)

if __name__=='__main__':unittest.main()
