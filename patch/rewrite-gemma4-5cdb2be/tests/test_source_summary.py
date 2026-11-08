import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from fim_patch.source_summary import summarize
from tests import test_local_fim as fixtures
from fim_patch.paired_repair import PairedRepairGenerator, ARMS
from fim_patch.unified_repair import RepairPolicy


class SummaryTests(unittest.TestCase):
    def test_literals_only_have_known_shape(self):
        raw=b'def f(x):\n    if x:\n        return [x]\n    return callback(x)\n'
        facts=summarize(raw,'f')['facts']
        returns=[f for f in facts if f['kind']=='return']
        self.assertIn('list literal',returns[0]['detail'])
        self.assertIn('unknown',returns[1]['detail'])

    def test_names_stay_unknown_even_after_literal_binding(self):
        report=summarize(b'def f():\n    args = []\n    args += [1]\n    return args\n','f')
        self.assertIn('shape=unknown',next(f['detail'] for f in report['facts'] if f['kind']=='return'))
        self.assertTrue(any(f['kind']=='mutation' for f in report['facts']))

    def test_no_execution_or_nested_docstring_claims(self):
        raw=b'def f():\n    """Return a string."""\n    def helper():\n        return "SECRET"\n    return dangerous()\n'
        report=summarize(raw,'f')
        self.assertNotIn('SECRET',str(report))
        self.assertNotIn('Return a string',report['rendered'])
        self.assertFalse(report['semantic_contract_verified'])
        self.assertEqual(report['model_calls'],0)

    def test_bounded_render_preserves_complete_audit(self):
        raw=('def f():\n'+''.join('    item'+str(i)+' = call'+str(i)+'()\n' for i in range(100))+'    return []\n').encode()
        report=summarize(raw,'f',500)
        self.assertLessEqual(len(report['rendered']),500)
        self.assertTrue(report['truncated'])
        self.assertEqual(len(report['facts']),201)
        self.assertIn('return []',report['rendered'])

    def test_controls_raises_and_unicode(self):
        raw='async def f(x):\n    try:\n        for y in x:\n            if y:\n                raise ValueError("中文")\n    except ValueError:\n        return\n'.encode()
        kinds={f['kind'] for f in summarize(raw,'f')['facts']}
        self.assertTrue({'loop','branch','raise','handler','return'}<=kinds)

    def test_rejects_bad_limits_and_unknown_symbol(self):
        for limit in (True,100):
            with self.assertRaises(ValueError):summarize(b'def f():\n    pass\n','f',limit)
        with self.assertRaises(ValueError):summarize(b'def f():\n    pass\n','missing')


class SummaryPairTests(unittest.TestCase):
    setUp=fixtures.LocalFIMTests.setUp
    replace_source=fixtures.LocalFIMTests.replace_source

    def test_only_summary_changes_with_identical_body_budget_seed_and_patch(self):
        result=[];backends=[]
        for arm in ('A3','A3_summary'):
            backend,_=fixtures.LocalFIMTests.generator(self,['    cleaned = value.strip()\n    parsed = cleaned.split(delimiter or ",")\n    return parsed\n'])
            class Counter:
                def count(self,prefix,suffix):return max(1,len(prefix+suffix)//4)
            backend._token_counter=Counter()
            generator=PairedRepairGenerator(backend,arm,RepairPolicy(max_generation_calls=1))
            inner=generator.inner(backend,'body',ceiling=256)
            result.append(inner.generate(self.ticket,self.loc,self.repo));backends.append(backend)
        self.assertTrue(all(r['patch_status']=='generated' for r in result),result)
        self.assertEqual(result[0]['span'],result[1]['span'])
        self.assertEqual(result[0]['patch'],result[1]['patch'])
        self.assertEqual(backends[0].budgets,backends[1].budgets)
        self.assertEqual(backends[0].prompts[0][1],backends[1].prompts[0][1])
        extra=result[1]['source_body_summary']['rendered']
        self.assertEqual(backends[1].prompts[0][0].replace(extra,''),backends[0].prompts[0][0])
        self.assertNotIn('source_body_summary',result[0])
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)
        a=vars(ARMS['A3']);b=vars(ARMS['A3_summary'])
        self.assertEqual([k for k in a if a[k]!=b[k]],['source_summary'])


if __name__=='__main__':unittest.main()
