import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'experiments/generalization_v1'))
from existing_test_summary import extract_tests,summarize_file


class ExistingTestSummaryTests(unittest.TestCase):
    def extract(self,files,target='f',chars=3000):
        return extract_tests(files,lambda p:files[p].encode(),[dict(file_path='pkg/core.py',symbol_qualified_name=target)],max_chars=chars)

    def test_alias_and_assert_keep_values(self):
        result=self.extract({'pkg/core.py':'def f(x): return x\n',
            'tests/test_core.py':'from pkg.core import f as renamed\ndef test_f():\n    x = 2\n    assert renamed(x) == 3\n'})
        self.assertEqual(result['status'],'evidence_available')
        self.assertIn('assert renamed(x) == 3',result['summary_text'])
        self.assertIn('x = 2',result['summary_text'])
        self.assertFalse(result['test_execution'])

    def test_reexport_is_resolved(self):
        result=self.extract({'pkg/core.py':'def f(x): return x\n','pkg/__init__.py':'from .core import f\n',
            'tests/test_core.py':'from pkg import f\ndef test_f():\n    assert f(2) == 2\n'})
        self.assertEqual(len(result['selected_tests']),1)

    def test_unknown_factory_does_not_invent_receiver_type(self):
        files={'pkg/core.py':'class C:\n    def f(self): return 1\n',
            'tests/test_core.py':'from pkg.core import factory\ndef test_f():\n    x = factory()\n    assert x.f() == 1\n'}
        self.assertEqual(self.extract(files,'C.f')['selected_tests'],[])

    def test_constructor_receiver_is_resolved(self):
        files={'pkg/core.py':'class C:\n    def f(self): return 1\n',
            'tests/test_core.py':'from pkg.core import C\ndef test_f():\n    x = C()\n    assert x.f() == 1\n'}
        self.assertEqual(len(self.extract(files,'C.f')['selected_tests']),1)

    def test_parameter_shadow_is_not_imported_api(self):
        result=self.extract({'pkg/core.py':'def f(x): return x\n',
            'tests/test_core.py':'from pkg.core import f\ndef test_f(f):\n    assert f(1) == 1\n'})
        self.assertEqual(result['selected_tests'],[])

    def test_annotated_shadow_is_not_imported_api(self):
        result=self.extract({'pkg/core.py':'def f(x): return x\n',
            'tests/test_core.py':'from pkg.core import f\ndef test_f():\n    f: object = helper\n    assert f(1) == 1\n'})
        self.assertEqual(result['selected_tests'],[])

    def test_opposite_branch_does_not_inherit_receiver(self):
        files={'pkg/core.py':'class C:\n    def f(self): return 1\n','tests/test_core.py':
            'from pkg.core import C\ndef test_f(flag):\n    if flag:\n        x = C()\n    else:\n        assert x.f() == 1\n'}
        self.assertEqual(self.extract(files,'C.f')['selected_tests'],[])

    def test_guard_and_parametrize_retained(self):
        files={'pkg/core.py':'def f(x): return x\n','tests/test_core.py':
            'from pkg.core import f\nimport pytest\n@pytest.mark.parametrize("x", [1, 2])\ndef test_f(x):\n    if x > 0:\n        assert f(x) == x\n'}
        selected=self.extract(files)['selected_tests'][0]
        self.assertEqual(selected['assertions'][0]['guards'],['x > 0'])
        self.assertTrue(all(e['guards']==['x > 0'] for e in selected['relevance']))
        self.assertIn('parametrize',selected['decorators'][0]['source'])

    def test_exception_and_oversized_assert_not_clipped(self):
        files={'pkg/core.py':'def f(x): return x\n','tests/test_core.py':
            'from pkg.core import f\nimport pytest\ndef test_f():\n    with pytest.raises(ValueError, match="bad"):\n        f(0)\n    assert f(1) == "'+('a'*450)+'"\n'}
        selected=self.extract(files)['selected_tests'][0]
        self.assertEqual(selected['assertions'],[])
        self.assertEqual(len(selected['exception_expectations']),1)
        self.assertIn('oversized_assertion_omitted',selected['unknown'])

    def test_read_only_requested_base_files_and_no_exec(self):
        source='from pkg.core import f\nraise RuntimeError("must not execute")\ndef test_f():\n    assert f(1) == 1\n'
        result=self.extract({'pkg/core.py':'def f(x): return x\n','tests/test_core.py':source})
        self.assertEqual(result['status'],'evidence_available')
        self.assertFalse(result['benchmark_hidden_fields_used'])


if __name__=='__main__':unittest.main()
