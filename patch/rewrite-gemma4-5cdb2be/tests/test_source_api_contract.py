from pathlib import Path
import sys
import unittest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'experiments/generalization_v1'),str(Path(__file__).resolve().parents[1]/'src')]
from public_api_context import SourceIndex
from source_api_contract import assess,context_for_target


class SourceAPIContractTests(unittest.TestCase):
    def check(self,base,after,api='def convert(value, *, unit=None):\n return value\n',symbol='entry'):
        files={'pkg/__init__.py':b'', 'pkg/api.py':api.encode(),'pkg/entry.py':base.encode()}
        return assess(SourceIndex(files,files.__getitem__), 'pkg/entry.py',base,after,symbol)

    def test_cross_file_named_import_keyword_mismatch(self):
        b='from .api import convert\ndef entry(value):\n return value\n'
        r=self.check(b,b.replace('return value','return convert(value, copy=True)'))
        self.assertEqual(r['counts'],{'known_incompatible':1})
        self.assertIn('copy',r['calls'][0]['error']);self.assertIn('pkg.api.convert',r['compact_context'])

    def test_binding_pass_not_type_pass(self):
        b='from .api import convert as conv\ndef entry(value):\n return value\n'
        r=self.check(b,b.replace('return value','return conv(value, unit=None)'))
        self.assertEqual(r['counts'],{'binding_compatible':1});self.assertFalse(r['calls'][0]['value_type_verified'])

    def test_module_alias(self):
        b='import pkg.api as api\ndef entry(value):\n return value\n'
        r=self.check(b,b.replace('return value','return api.convert(value, bad=True)'))
        self.assertEqual(r['counts'],{'known_incompatible':1})

    def test_local_callback_and_attribute_unknown(self):
        b='from .api import convert\ndef entry(convert):\n return 1\n'
        r=self.check(b,b.replace('return 1','return convert(1)'))
        self.assertEqual(r['calls'][0]['reason'],'local_or_dynamic_receiver')
        b='def entry(self):\n return 1\n'
        r=self.check(b,b.replace('return 1','return self.unit.to(copy=True)'))
        self.assertEqual(r['counts'],{'unknown':1})

    def test_unchanged_call_not_checked(self):
        b='from .api import convert\ndef entry(value):\n return convert(value)\n'
        r=self.check(b,b.replace('return convert(value)','return convert(value) + 1'))
        self.assertEqual(r['calls'],[])

    def test_unpacking_has_signature_but_unknown(self):
        b='from .api import convert\ndef entry(args):\n return 1\n'
        r=self.check(b,b.replace('return 1','return convert(*args)'))
        self.assertEqual(r['calls'][0]['reason'],'argument_unpacking_unknown')
        self.assertIn('convert',r['compact_context'])

    def test_static_class_method(self):
        api='class Converter:\n @staticmethod\n def convert(value, *, unit=None):\n  return value\n'
        b='from .api import Converter\ndef entry(value):\n return value\n'
        r=self.check(b,b.replace('return value','return Converter.convert(value, copy=True)'),api)
        self.assertEqual(r['counts'],{'known_incompatible':1})

    def test_classmethod_implicit_cls(self):
        api='class Converter:\n @classmethod\n def convert(cls, value):\n  return value\n'
        b='from .api import Converter\ndef entry(value):\n return value\n'
        r=self.check(b,b.replace('return value','return Converter.convert(value)'),api)
        self.assertEqual(r['counts'],{'binding_compatible':1})

    def test_decorated_function_unknown(self):
        api='@factory\ndef convert(value):\n return value\n'
        b='from .api import convert\ndef entry(value):\n return value\n'
        r=self.check(b,b.replace('return value','return convert(value)'),api)
        self.assertEqual(r['calls'][0]['reason'],'decorated_function_unknown')

    def test_conditional_global_rebinding_unknown(self):
        b='from .api import convert\nif enabled:\n convert=callback\ndef entry(value):\n return value\n'
        r=self.check(b,b.replace('return value','return convert(value)'))
        self.assertEqual(r['calls'][0]['reason'],'global_rebinding')

    def test_constructor_unknown(self):
        b='from .api import Converter\ndef entry(value):\n return value\n'
        r=self.check(b,b.replace('return value','return Converter(value)'), 'class Converter:\n pass\n')
        self.assertEqual(r['calls'][0]['reason'],'constructor_or_non_function_not_supported')

    def test_new_import_not_guessed(self):
        b='def entry(value):\n return value\n'
        after='from .api import convert\n'+b.replace('return value','return convert(value)')
        self.assertEqual(self.check(b,after)['calls'][0]['reason'],'module_bindings_changed')

    def test_source_never_executed_and_context_bounded(self):
        api='raise RuntimeError("must not execute")\ndef convert(value):\n return value.split(".")\n'
        b='from .api import convert\ndef entry(value):\n return value\n'
        r=self.check(b,b.replace('return value','return convert(value)'),api)
        self.assertEqual(r['counts'],{'binding_compatible':1})
        self.assertEqual(r['calls'][0]['parameter_operations'][0]['operation'],'split')
        self.assertLessEqual(len(r['compact_context']),2000)

    def test_inherited_receiver_unknown(self):
        api='class Base:\n def convert(self, value):\n  return value\nclass Converter(Base):\n pass\n'
        b='from .api import Converter\ndef entry(value):\n return value\n'
        r=self.check(b,b.replace('return value','return Converter.convert(value)'),api)
        self.assertEqual(r['counts'],{'unknown':1})

    def test_shadowed_staticmethod_unknown(self):
        api='def staticmethod(fn):\n return wrapper\nclass Converter:\n @staticmethod\n def convert(value):\n  return value\n'
        b='from .api import Converter\ndef entry(value):\n return value\n'
        r=self.check(b,b.replace('return value','return Converter.convert(value)'),api)
        self.assertEqual(r['calls'][0]['reason'],'decorator_binding_shadowed')

    def test_positional_only_keyword_rejected(self):
        b='from .api import convert\ndef entry(value):\n return value\n'
        r=self.check(b,b.replace('return value','return convert(value=value)'), 'def convert(value, /):\n return value\n')
        self.assertEqual(r['counts'],{'known_incompatible':1})

    def test_first_round_context_is_source_only_and_not_injected(self):
        base='from .api import convert\ndef entry(value):\n return convert(value)\n'
        files={'pkg/__init__.py':b'','pkg/api.py':b'def convert(value):\n return value\n','pkg/entry.py':base.encode()}
        index=SourceIndex(files,files.__getitem__)
        context=context_for_target(index,'pkg/entry.py',base,'entry')
        self.assertIn('pkg.api.convert(value)',context['compact_context'])
        self.assertFalse(context['prompt_integration_enabled']);self.assertFalse(context['token_budget_verified'])
        tiny=context_for_target(index,'pkg/entry.py',base,'entry',max_chars=5)
        self.assertEqual(tiny['compact_context'],'')

    def test_fim_markers_in_defaults_sanitized(self):
        b='from .api import convert\ndef entry(value):\n return value\n'
        r=self.check(b,b.replace('return value','return convert(value)'), 'def convert(value, suffix="<MID>"):\n return value\n')
        self.assertNotIn('<MID>',r['compact_context'])

    def test_candidate_changed_callee_unknown(self):
        b='def helper(value):\n return value\ndef entry(value):\n return value\n'
        after=b.replace('def helper(value):','def helper(value, extra):').replace('def entry(value):\n return value','def entry(value):\n return helper(value)')
        r=self.check(b,after)
        self.assertEqual(r['calls'][0]['reason'],'callee_also_changed')

    def test_ambiguous_reexports_unknown(self):
        base='from pkg import convert\ndef entry(value):\n return value\n'
        files={'pkg/__init__.py':b'from .a import *\nfrom .b import *\n',
               'pkg/a.py':b'def convert(value):\n return value\n', 'pkg/b.py':b'def convert(value):\n return value\n',
               'pkg/entry.py':base.encode()}
        r=assess(SourceIndex(files,files.__getitem__),'pkg/entry.py',base,base.replace('return value','return convert(value)'),'entry')
        self.assertEqual(r['counts'],{'unknown':1})


if __name__=='__main__':unittest.main()
