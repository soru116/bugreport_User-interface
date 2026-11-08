import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'experiments/generalization_v1'))
from public_api_context import SourceIndex, extract
from candidate_compatibility import assess


class CandidateCompatibilityTests(unittest.TestCase):
    def analyze(self, source, public=None):
        public=public or 'Input should be rejected.\n```python\nfrom pkg import Device\nDevice(data=1)\n```'
        blobs={'pkg/__init__.py':source.encode()}
        index=SourceIndex(blobs, blobs.__getitem__)
        context=extract(index,public)
        return assess(index,context,public),context,index

    def test_named_input_used(self):
        result,_,_=self.analyze('class Device:\n def __init__(self, data):\n  self.value=data\n')
        row=next(c for c in result['candidates'] if c['role']=='constructor')
        self.assertEqual(row['status'],'input_path_evidence')
        self.assertFalse(row['compatible_verified']); self.assertFalse(row['selected'])

    def test_unused_input_unknown(self):
        r,_,_=self.analyze('class Device:\n def __init__(self, data):\n  pass\n')
        self.assertEqual(r['status'],'unknown')

    def test_kwargs_not_assumed_bound(self):
        r,_,_=self.analyze('class Device:\n def __init__(self, **kwargs):\n  self.value=kwargs\n')
        self.assertEqual(r['status'],'unknown')

    def test_setter_is_evidence_not_selection(self):
        r,_,_=self.analyze('class Device:\n @property\n def data(self):\n  return self._data\n @data.setter\n def data(self, value):\n  self._data=value\n')
        setter=next(c for c in r['candidates'] if c['role']=='input_keyword_setter')
        self.assertEqual(setter['status'],'input_path_evidence')
        self.assertIn('runtime_dispatch_not_verified',setter['unknowns'])
        self.assertFalse(setter['selected'])

    def test_nested_parameter_shadow_unknown(self):
        r,_,_=self.analyze('class Device:\n def __init__(self, data):\n  def inner(data):\n   return data\n')
        self.assertEqual(r['status'],'unknown')

    def test_reassigned_input_unknown(self):
        r,_,_=self.analyze('class Device:\n def __init__(self, data):\n  data=0\n  self.value=data\n')
        self.assertEqual(r['status'],'unknown')

    def test_comprehension_shadow_unknown(self):
        r,_,_=self.analyze('class Device:\n def __init__(self, data):\n  self.value=[data for data in range(2)]\n')
        self.assertEqual(r['status'],'unknown')

    def test_requirement_missing(self):
        r,_,_=self.analyze('class Device:\n def __init__(self, data):\n  self.value=data\n',
                         '```python\nfrom pkg import Device\nDevice(data=1)\n```')
        self.assertEqual(r['status'],'unknown')

    def test_hash_mismatch_fails_closed(self):
        _,context,index=self.analyze('class Device:\n def __init__(self, data):\n  self.value=data\n')
        changed=copy.deepcopy(context)
        changed['entries'][0]['candidate_locations'][0]['normalized_source_sha256']='wrong'
        with self.assertRaises(ValueError): assess(index,changed,'Input should be rejected.')

    def test_callback_dispatch_unknown(self):
        r,_,_=self.analyze('class Device:\n def __init__(self, data, callback=None):\n  callback(data)\n')
        call=next(e for c in r['candidates'] for e in c['evidence'] if e['kind']=='input_passed_to_call')
        self.assertEqual(call['definitions'],[]); self.assertFalse(call['dispatch_verified'])

    def test_inherited_role_remains_unverified(self):
        r,_,_=self.analyze('class Base:\n def __init__(self, data):\n  self.value=data\nclass Device(Base):\n pass\n')
        self.assertEqual(r['status'],'input_path_evidence')
        self.assertTrue(all(not c['compatible_verified'] and not c['selected'] for c in r['candidates']))


if __name__=='__main__':unittest.main()
