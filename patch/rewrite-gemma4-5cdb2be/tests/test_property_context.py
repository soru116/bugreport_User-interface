import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from fim_patch.api_source_index import SourceIndex
from fim_patch.property_context import extract


class PropertyContextTests(unittest.TestCase):
    def check(self,source,symbol='Owner.target',**kwargs):
        files={'pkg/__init__.py':b'','pkg/model.py':source.encode()}
        index=SourceIndex(files,files.__getitem__,{'pkg/model.py':'blob'})
        return extract(index,'pkg/model.py',source,symbol,**kwargs)

    def test_direct_getter_with_conditional_return_and_unknown_shape(self):
        s="class Owner:\n @property\n def values(self):\n  return self._values if flag else ()\n def target(self):\n  return self.values\n"
        r=self.check(s)
        self.assertEqual(r['status'],'property_source_available')
        self.assertIn('self._values if flag else ()',r['rendered'])
        self.assertFalse(r['entries'][0]['return_type_verified'])
        self.assertFalse(r['prompt_integration_enabled'])

    def test_recursive_properties_and_cycles_bounded(self):
        s='class Owner:\n @property\n def first(self):\n  return self.second\n @property\n def second(self):\n  return self.first\n def target(self):\n  return self.first\n'
        r=self.check(s,max_depth=2)
        self.assertEqual(len(r['entries']),2)
        self.assertEqual([e['depth'] for e in r['entries']],[0,1])

    def test_getter_setter_not_merged(self):
        s='class Owner:\n @property\n def value(self):\n  return self._value\n @value.setter\n def value(self, v):\n  self._value = v\n def target(self):\n  return self.value\n'
        r=self.check(s);facts=r['entries'][0]['facts']
        self.assertFalse(any('v' in f['detail'].split() for f in facts))
        self.assertIn('return self._value',r['rendered'])

    def test_single_base_property_and_requested_owner_override(self):
        s='class Base:\n @property\n def first(self):\n  return self.second\n @property\n def second(self):\n  return 1\nclass Owner(Base):\n @property\n def second(self):\n  return 2\n def target(self):\n  return self.first\n'
        r=self.check(s)
        self.assertEqual(r['entries'][0]['declaring_owner'],'pkg.model.Base')
        self.assertEqual(r['entries'][1]['declaring_owner'],'pkg.model.Owner')

    def test_dynamic_other_receiver_and_callback_do_not_guess(self):
        s='class Owner:\n def target(self,col):\n  return col.groups.aggregate(callback)\n'
        r=self.check(s)
        self.assertEqual(r['entries'],[]);self.assertEqual(r['status'],'unknown')
        self.assertTrue(any('col.groups' in x for x in r['unknowns']))

    def test_reassigned_receiver_blocks_lookup(self):
        s='class Owner:\n @property\n def value(self):\n  return 1\n def target(self):\n  self = other\n  return self.value\n'
        self.assertEqual(self.check(s)['entries'],[])

    def test_complex_bases_and_metaclass_unknown(self):
        for declaration in ('class Owner(A,B):','class Owner(metaclass=Meta):'):
            r=self.check(declaration+'\n def target(self):\n  return self.value\n')
            self.assertEqual(r['entries'],[])

    def test_shadowed_property_and_custom_descriptor_unknown(self):
        s='def property(fn):\n return fn\nclass Owner:\n @property\n def value(self):\n  return 1\n def target(self):\n  return self.value\n'
        self.assertEqual(self.check(s)['entries'],[])
        s=s[s.index('class Owner:'):].replace('@property','@cached_property')
        self.assertEqual(self.check(s)['entries'],[])

    def test_dynamic_attribute_hooks_and_generator_getter_unknown(self):
        s='class Owner:\n def __getattr__(self,name):\n  return custom(name)\n @property\n def value(self):\n  return 1\n def target(self):\n  return self.value\n'
        self.assertEqual(self.check(s)['entries'],[])
        s=s.replace(' def __getattr__(self,name):\n  return custom(name)\n','').replace('return 1','yield 1')
        self.assertEqual(self.check(s)['entries'],[])

    def test_nested_function_reads_not_attributed_to_target(self):
        s='class Owner:\n @property\n def value(self):\n  return 1\n def target(self):\n  def nested(other):\n   return self.value\n  return 0\n'
        self.assertEqual(self.check(s)['entries'],[])

    def test_context_bounded_and_tokens_sanitized_no_source_execution(self):
        s="raise RuntimeError('do not execute')\nclass Owner:\n @property\n def value(self):\n  return '<MID>'\n def target(self):\n  return self.value\n"
        r=self.check(s,max_chars=500)
        self.assertLessEqual(r['rendered_chars'],500);self.assertNotIn('<MID>',r['rendered']);self.assertFalse(r['source_executed'])
        with self.assertRaises(ValueError):self.check(s,max_properties=True)

    def test_standalone_function_and_static_method_unknown(self):
        self.assertEqual(self.check('def target(value):\n return value.name\n','target')['status'],'unknown')
        s='class Owner:\n @property\n def value(self):\n  return 1\n @staticmethod\n def target(other):\n  return other.value\n'
        self.assertEqual(self.check(s)['entries'],[])


if __name__=='__main__':unittest.main()
