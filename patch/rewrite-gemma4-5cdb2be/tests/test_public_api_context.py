import sys
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'experiments/generalization_v1')]
from public_api_context import SourceIndex,extract,public_references


def index(files):return SourceIndex(files,lambda p:files[p].encode(),{p:'blob-'+p for p in files})


class PublicAPIContextTests(unittest.TestCase):
    def test_star_reexport_base_and_keyword_setter(self):
        files={'pkg/__init__.py':'from .entry import *\n','pkg/entry.py':
            '__all__=["Item"]\nclass Base:\n    def __init__(self, data=None):\n        self.data=data\n    @property\n    def data(self):\n        return self._data\n    @data.setter\n    def data(self, value):\n        self._data=value\nclass Item(Base):\n    pass\n'}
        r=extract(index(files),'```python\nimport pkg as renamed\nx=renamed.Item(data=1)\n```')
        self.assertEqual(r['status'],'source_context_available')
        roles={c['role_hint'] for c in r['entries'][0]['candidate_locations']}
        self.assertIn('input_keyword_setter',roles);self.assertIn('constructor',roles)
        setter=next(c for c in r['entries'][0]['candidate_locations'] if c['role_hint']=='input_keyword_setter')
        self.assertIn('Base.data',setter['symbol']);self.assertFalse(setter['repair_correctness_verified'])

    def test_ambiguous_star_providers_not_auto_selected(self):
        files={'pkg/__init__.py':'from .a import *\nfrom .b import *\n','pkg/a.py':'class Item: pass\n','pkg/b.py':'class Item: pass\n'}
        r=extract(index(files),'```python\nfrom pkg import Item\nx=Item()\n```')
        self.assertEqual(r['entries'][0]['status'],'unknown');self.assertEqual(len(r['entries'][0]['roots']),2)

    def test_src_layout_and_named_reexport(self):
        files={'src/pkg/__init__.py':'from .core import entry as API\n','src/pkg/core.py':'def entry(x): return x\n'}
        self.assertEqual(index(files).resolve('pkg.API'),(('pkg.core','entry',1),))

    def test_dynamic_all_cycle_and_external_unknown(self):
        files={'pkg/__init__.py':'from .core import *\n','pkg/core.py':'__all__ = build_names()\nclass Item: pass\n'}
        self.assertEqual(index(files).resolve('pkg.Item'),())
        self.assertEqual(index({'pkg/__init__.py':'from . import Item\n'}).resolve('pkg.Item'),())
        self.assertEqual(index(files).resolve('external.Item'),())

    def test_shadowed_public_import_does_not_resolve(self):
        refs,_=public_references('```python\nfrom pkg import Item\ndef f(Item):\n    return Item()\n```')
        self.assertEqual(refs,[])

    def test_source_assignment_shadow_not_original_definition(self):
        self.assertEqual(index({'pkg/core.py':'def entry(): return 1\nentry = build_other()\n'}).resolve('pkg.core.entry'),())

    def test_never_executes_source_or_example(self):
        files={'pkg/__init__.py':'raise RuntimeError("no execute")\nclass Item: pass\n'}
        r=extract(index(files),'```python\nfrom pkg import Item\nraise RuntimeError("no execute")\nx=Item()\n```')
        self.assertEqual(r['status'],'source_context_available');self.assertEqual(r['model_calls'],0)

    def test_local_callback_not_reported_as_global_callee(self):
        files={'pkg/__init__.py':'def helper(): return 1\ndef entry(helper):\n    return helper()\n'}
        r=extract(index(files),'```python\nfrom pkg import entry\nx=entry(helper=None)\n```')
        call=r['entries'][0]['related_calls'][0]
        self.assertEqual(call['definitions'],[])

    def test_doctest_prompts_only_without_executing_traceback(self):
        public='```\n>>> from pkg import Item\n>>> obj = Item()\nTraceback (most recent call last):\nRuntimeError: failure\n```'
        refs,_=public_references(public)
        self.assertEqual(refs[0]['api'],'pkg.Item')

    def test_benchmark_fields_never_become_public_references(self):
        from fim_patch.scope_planning import public_text
        text='```python\nfrom pkg import Item\nobj=Item()\n```'
        refs,_=public_references(public_text({'test_patch':text,'patch':text,'hints_text':text}))
        self.assertEqual(refs,[])


if __name__=='__main__':unittest.main()
