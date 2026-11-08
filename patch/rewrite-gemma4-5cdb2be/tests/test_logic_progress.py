import copy
import sys
import unittest
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parents[1]/'src'), str(Path(__file__).resolve().parents[1])]
from fim_patch.generation_records import logic_fingerprint, annotate_generation, repeated_failed_logic, LOGIC_VERSION
from fim_patch.generator import FIMPatchGenerator
from fim_patch.iterative import IterativeRepairGenerator
from scripts.run_stage4_batch import build_record, summarize
from tests import test_fim


class LogicTests(unittest.TestCase):
    def test_whitespace_and_comments_deduplicate_without_erasing_strings(self):
        source='def f(x):\n    return x + 1\n'
        fingerprint=logic_fingerprint([('app.py',source)])
        self.assertEqual(fingerprint,logic_fingerprint([('app.py','def f(x):\n    # note\n    return (x+1)\n\n')]))
        for other in ('def f(x):\n    return x + 2\n', 'def f(y):\n    return y + 1\n',
                      'def f(x):\n    """doc"""\n    return x + 1\n'):
            self.assertNotEqual(fingerprint,logic_fingerprint([('app.py',other)]))
        self.assertNotEqual(fingerprint,logic_fingerprint([('other.py',source)]))

    def test_all_files_and_order_are_accounted_for(self):
        a=('a.py',b'x=1\n');b=('b.py',b'y=2\n')
        self.assertEqual(logic_fingerprint([a,b]),logic_fingerprint([b,a]))
        self.assertNotEqual(logic_fingerprint([a,b]),logic_fingerprint([a,('b.py',b'y=3\n')]))
        for sources in ([],[a,a],[('x.txt','x=1')],[('x.py','def broken(')],[('x.py',b'\xff')]):
            self.assertIsNone(logic_fingerprint(sources))

    def attempt(self, patch='one', digest='checked', reason='source_none_regression'):
        return dict(patch_status='no_valid_candidate',patch='',apply_status='passed',
                    candidates=[dict(candidate_id=1,status='rejected',reason=reason,patch=patch,
                       apply_check_status='passed',logic_sha256=digest,logic_fingerprint_version=LOGIC_VERSION,
                       validation=dict(status='failed',official_resolved=False))])

    def test_additive_accounting_keeps_raw_patches_and_attempts(self):
        result=self.attempt();result['repair_history']=[self.attempt(),self.attempt('two')]
        annotate_generation(result)
        self.assertEqual(result['static_candidate_count'],2)
        self.assertEqual(result['logical_static_candidate_count'],1)
        self.assertEqual(len(result['logical_candidate_groups'][0]['attempts']),2)
        self.assertEqual([c['patch'] for c in result['generated_candidates']],['one','two'])
        record=build_record({'ticket_id':'x'},result=result,output_dir='unused')
        stats=summarize([record])
        self.assertEqual(stats['static_candidates_total'],2)
        self.assertEqual(stats['logical_static_candidates_total'],1)
        self.assertEqual(stats['logic_unclassified_candidates_total'],0)

    def test_historical_missing_fingerprints_stay_unknown(self):
        result=self.attempt();result['candidates'][0].pop('logic_fingerprint_version')
        annotate_generation(result)
        self.assertEqual(result['logical_static_candidate_count'],0)
        self.assertEqual(result['logic_unclassified_candidate_count'],1)
        self.assertFalse(result['logic_accounting_complete'])
        self.assertIsNone(repeated_failed_logic([result,result]))

    def test_multi_function_composition_is_not_inferred_from_component_ast(self):
        result=self.attempt()
        result.update(multi_function_repair='v1',combined_patch='combined',combined_apply_status='passed')
        annotate_generation(result)
        self.assertEqual(result['static_candidate_count'],1)
        self.assertEqual(result['logical_static_candidate_count'],0)
        self.assertEqual(result['logic_unclassified_candidate_count'],1)

    def test_resume_rejects_previous_progress_rules(self):
        import json,tempfile
        from scripts.run_stage4_batch import validate_resume_config
        current=dict(logic_accounting_version=LOGIC_VERSION,repair_progress_version=LOGIC_VERSION)
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'run_config.json';path.write_text(json.dumps({}),encoding='utf8')
            with self.assertRaisesRegex(ValueError,'repair_progress_version'):
                validate_resume_config(path,current)

    def test_stop_requires_same_target_same_failure_and_checked_structure(self):
        one=self.attempt();two=self.attempt('two')
        self.assertEqual(repeated_failed_logic([one,two])['status'],'no_logic_progress')
        variants=[]
        different=copy.deepcopy(two);different['candidates'][0]['validation']['feedback']='new assertion';variants.append(different)
        different=copy.deepcopy(two);different['candidates'][0]['logic_sha256']='new';variants.append(different)
        different=copy.deepcopy(two);different['span']={'symbol':'other'};variants.append(different)
        different=copy.deepcopy(two);different['candidates'][0].pop('apply_check_status');variants.append(different)
        different=copy.deepcopy(two);different['patch_status']='generated';variants.append(different)
        for different in variants:
            self.assertIsNone(repeated_failed_logic([one,different]))


class PipelineProgressTests(unittest.TestCase):
    def setUp(self):
        test_fim.PatchTests.setUp(self)

    def run_repair(self,outputs,validator):
        backend=test_fim.FakeBackend(outputs)
        generator=FIMPatchGenerator(backend,candidates=1,prompt_layout='compact_fim',validator=validator)
        result=IterativeRepairGenerator(generator,3).generate(self.ticket,self.loc,self.repo)
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)
        return backend,result

    def test_same_failed_ast_stops_before_third_call(self):
        backend,result=self.run_repair(['    return x.upper()\n','    return x.upper()\n\n',
                                       '    return "third must not run"\n'],
                                      lambda *args:dict(status='failed',feedback='AssertionError: still wrong'))
        self.assertEqual(backend.calls,2)
        self.assertEqual(result['repair_stop_reason'],'no_logic_progress')
        self.assertEqual(result['repair_progress']['repeated_round'],1)
        self.assertEqual(result['logical_static_candidate_count'],1)
        self.assertEqual(result['static_candidate_count'],2)

    def test_real_structure_change_gets_third_round(self):
        backend,result=self.run_repair(['    return x.upper()\n','    return x.lower()\n','    return "fixed"\n'],
                                      lambda *args:dict(status='failed',feedback='same assertion'))
        self.assertEqual(backend.calls,3)
        self.assertEqual(result['repair_stop_reason'],'round_limit')
        self.assertEqual(result['logical_static_candidate_count'],3)

    def test_new_test_outcome_and_success_are_not_stopped(self):
        outcomes=iter([dict(status='failed',feedback='first assertion'),
                       dict(status='failed',feedback='another assertion'),dict(status='plausible')])
        backend,result=self.run_repair(['    return x.upper()\n']*3,lambda *args:next(outcomes))
        self.assertEqual(backend.calls,3)
        self.assertEqual(result['patch_status'],'generated')
        self.assertEqual(result['repair_stop_reason'],'tests_passed')


if __name__=='__main__': unittest.main()
