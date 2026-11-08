import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from fim_patch.source_summary import summarize
from fim_patch.requirement_conflicts import mark_conflicts
from tests import test_source_summary as fixtures
from fim_patch.paired_repair import PairedRepairGenerator,ARMS
from fim_patch.unified_repair import RepairPolicy


class ConflictTests(unittest.TestCase):
    raw=b'def f(obj):\n    value = obj.old_source\n    if value:\n        return [value]\n    return []\n'

    def report(self,text,raw=None,limit=2200):
        return mark_conflicts(summarize(raw or self.raw,'f',limit),{'description':text})

    def test_explicit_remove_marks_matching_fact_only(self):
        r=self.report('Avoid using old_source.')
        self.assertEqual(r['requirement_conflicts']['explicitly_removed'],['obj.old_source'])
        self.assertIn('REPLACE_REQUESTED',r['rendered'])
        branch=next(f for f in r['facts'] if f['kind']=='branch')
        self.assertEqual(branch['requirement_status'],'observed_only_not_verified_preservation')
        self.assertIsNone(r['requirement_conflicts']['inferred_replacement'])

    def test_negative_and_conditional_requests_are_unknown(self):
        for text in ('Do not avoid using old_source.','Never avoid old_source.','Maybe avoid old_source.','If needed avoid old_source.'):
            self.assertEqual(self.report(text)['requirement_conflicts']['status'],'unknown',text)

    def test_contradictory_requests_do_not_select_replacement(self):
        r=self.report('Avoid old_source. Keep using old_source.')
        self.assertEqual(r['requirement_conflicts']['status'],'ambiguous')
        self.assertNotIn('REPLACE_REQUESTED',r['rendered'].split('# L')[1])
        self.assertTrue(any(f['requirement_status']=='ambiguous_public_requirements' for f in r['facts']))

    def test_plain_overlap_and_gold_fields_are_ignored(self):
        r=mark_conflicts(summarize(self.raw,'f'),{'description':'old_source is used.', 'test_patch':'avoid old_source','patch':'avoid old_source'})
        self.assertEqual(r['requirement_conflicts']['status'],'unknown')

    def test_qualifier_ambiguity_stays_unknown(self):
        raw=b'def f(obj, other):\n    return [obj.old_source, other.old_source]\n'
        self.assertEqual(self.report('avoid old_source',raw)['requirement_conflicts']['status'],'unknown')
        self.assertEqual(self.report('avoid obj.old_source',raw)['requirement_conflicts']['explicitly_removed'],['obj.old_source'])

    def test_nested_function_and_comments_do_not_match(self):
        raw=b'def f(obj):\n    # obj.old_source\n    def nested():\n        return obj.old_source\n    return []\n'
        self.assertEqual(self.report('avoid old_source',raw)['requirement_conflicts']['status'],'unknown')

    def test_renamed_dependency_and_unicode(self):
        raw=self.raw.replace(b'old_source',b'legacy_data')
        self.assertEqual(self.report('Avoid the use of `legacy_data`. 中文',raw)['requirement_conflicts']['explicitly_removed'],['obj.legacy_data'])

    def test_small_budget_keeps_full_audit(self):
        r=self.report('avoid old_source',limit=500)
        self.assertLessEqual(len(r['rendered']),500)
        self.assertTrue(r['truncated'])
        self.assertEqual(len(r['facts']),len(summarize(self.raw,'f')['facts']))

    def test_attribute_in_branch_body_does_not_taint_test(self):
        raw=b'def f(obj, ready):\n    if ready:\n        return obj.old_source\n    return []\n'
        r=self.report('avoid old_source',raw)
        branch=next(f for f in r['facts'] if f['kind']=='branch')
        self.assertEqual(branch['attributes'],[])


class ConflictPairTests(unittest.TestCase):
    setUp=fixtures.SummaryPairTests.setUp
    replace_source=fixtures.SummaryPairTests.replace_source

    def test_same_source_scope_seed_budget_and_patch(self):
        from tests import test_local_fim
        results=[];backends=[]
        self.ticket['description']='Avoid using value.strip; retain delimiter behavior.'
        for arm in ('A3_summary','A3_conflicts'):
            b,_=test_local_fim.LocalFIMTests.generator(self,['    cleaned = value.strip()\n    parsed = cleaned.split(delimiter or ",")\n    return parsed\n'])
            class Counter:
                def count(self,prefix,suffix):return max(1,len(prefix+suffix)//4)
            b._token_counter=Counter()
            g=PairedRepairGenerator(b,arm,RepairPolicy(max_generation_calls=1))
            results.append(g.inner(b,'body').generate(self.ticket,self.loc,self.repo));backends.append(b)
        self.assertTrue(all(r['patch_status']=='generated' for r in results),results)
        self.assertEqual(results[0]['span'],results[1]['span'])
        self.assertEqual(results[0]['patch'],results[1]['patch'])
        self.assertEqual(backends[0].budgets,backends[1].budgets)
        self.assertEqual(backends[0].prompts[0][1],backends[1].prompts[0][1])
        p=[b.prompts[0][0].replace(r['source_body_summary']['rendered'],'') for b,r in zip(backends,results)]
        self.assertEqual(p[0],p[1])
        self.assertIn('REPLACE_REQUESTED',results[1]['source_body_summary']['rendered'])
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)
        self.assertEqual([k for k,v in vars(ARMS['A3_summary']).items() if v!=vars(ARMS['A3_conflicts'])[k]],['requirement_conflicts'])


if __name__=='__main__':unittest.main()
