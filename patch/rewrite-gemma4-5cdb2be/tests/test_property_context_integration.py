import sys
from pathlib import Path
import unittest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).resolve().parents[1])]
from fim_patch.property_context_integration import first_context
from fim_patch.source_api_integration import first_context as api_context
from fim_patch.unified_repair import RepairPolicy
from tests.test_existing_test_context import Backend


def evidence():return dict(version='source-property-dependencies-v1',rendered='# Facts; types unknown.\n# Observed self.name -> getter pkg.Owner.name\n# L1 return: value\n# Observed self.other -> getter pkg.Owner.other\n# L4 return: other\n')


class PropertyIntegrationTests(unittest.TestCase):
    def test_first_request_only_preserves_suffix_seed_and_output(self):
        b=Backend();report={}
        with first_context(b,evidence(),report):
            b.infill('p','s',seed=9,max_new_tokens=20);b.infill('again','s',seed=10,max_new_tokens=20)
        self.assertTrue(report['injected']);self.assertEqual(report['included_properties'],2)
        self.assertTrue(b.calls[0][0].endswith('p'));self.assertEqual(b.calls[0][1],'s')
        self.assertEqual(b.calls[0][2],dict(seed=9,max_new_tokens=20));self.assertEqual(b.calls[1][0],'again')
        self.assertNotIn('infill',b.__dict__)

    def test_unknown_leaves_request_exact(self):
        b=Backend();r={}
        with first_context(b,dict(rendered='',unknowns=['dynamic']),r):b.infill('p','s',seed=7,max_new_tokens=20)
        self.assertEqual(b.calls,[('p','s',dict(seed=7,max_new_tokens=20))]);self.assertFalse(r['injected'])

    def test_whole_properties_only_when_token_space_limited(self):
        b=Backend();r={};b.num_ctx=130
        with first_context(b,evidence(),r):b.infill('p','s',seed=9,max_new_tokens=20)
        self.assertEqual(r['included_properties'],1);self.assertNotIn('self.other',b.calls[0][0])
        self.assertIn('# L1 return: value',b.calls[0][0])

    def test_no_space_omits_all_and_base_overflow_never_dispatches(self):
        b=Backend();b.num_ctx=40;r={}
        with first_context(b,evidence(),r):b.infill('p','s',max_new_tokens=20)
        self.assertEqual(b.calls[0][0],'p');self.assertFalse(r['injected'])
        b=Backend();b.num_ctx=10
        with first_context(b,evidence(),{}):
            with self.assertRaisesRegex(ValueError,'base_budget'):b.infill('p','s',max_new_tokens=20)
        self.assertEqual(b.calls,[])

    def test_nested_contexts_final_actual_request_fits(self):
        b=Backend();b.num_ctx=190;r={};api={}
        with first_context(b,evidence(),r):
            with api_context(b,dict(compact_context='# f(x)\n'),api):b.infill('p','s',seed=4,max_new_tokens=20)
        prefix,suffix,kwargs=b.calls[0]
        self.assertLessEqual(b._token_counter.count(prefix,suffix)+20+8,b.num_ctx)

    def test_markers_sanitized_and_non_comment_refused(self):
        b=Backend();ctx=evidence();ctx['rendered']=ctx['rendered'].replace('value','<FILL_ME> <MID>')
        with first_context(b,ctx,{}):b.infill('p','s',max_new_tokens=20)
        self.assertNotIn('<MID>',b.calls[0][0]);self.assertNotIn('<FILL_ME>',b.calls[0][0])
        with self.assertRaisesRegex(ValueError,'comment'):
            with first_context(b,dict(rendered='execute_code()'),{}):pass

    def test_exception_restores_binding_and_policy_defaults_off(self):
        b=Backend()
        with self.assertRaises(RuntimeError):
            with first_context(b,evidence(),{}):raise RuntimeError('test')
        self.assertNotIn('infill',b.__dict__);self.assertFalse(RepairPolicy().property_context)
        with self.assertRaisesRegex(ValueError,'must be bool'):RepairPolicy(property_context='true')


if __name__=='__main__':unittest.main()
