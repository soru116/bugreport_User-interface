import unittest
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from fim_patch.scope_compatibility import assess_scope


class ScopeCompatibilityTests(unittest.TestCase):
    def assess(self, text, public, body=True):
        begin=text.index('obj.old_api')
        spans={'expression':dict(start=begin,end=begin+len('obj.old_api')),
               'statement':dict(start=text.index('    value'),end=text.index('    if'))}
        if body:spans['body']=dict(start=text.index('    value'),end=len(text))
        return assess_scope(text.encode(), 'entry', {'description':public}, spans)

    source='def entry(obj):\n    value = obj.old_api\n    if value:\n        return ["legacy"]\n    return []\n'

    def test_explicit_dependency_and_downstream_branch(self):
        result=self.assess(self.source,'Avoid using old_api; use new_api instead.')
        self.assertEqual(result['action'],'start_at_body')
        self.assertEqual(result['evidence'][0]['incompatible_scopes'],['expression','statement'])
        self.assertFalse(result['semantic_compatibility_verified'])

    def test_benchmark_fields_never_used(self):
        spans={'expression':dict(start=0,end=0),'body':dict(start=0,end=len(self.source))}
        result=assess_scope(self.source.encode(),'entry',{'test_patch':'Avoid old_api','patch':'Avoid old_api'},spans)
        self.assertEqual(result['status'],'unknown')

    def test_overlap_not_a_conflict(self):
        self.assertEqual(self.assess(self.source,'old_api sometimes fails')['status'],'unknown')

    def test_no_branch_no_expansion(self):
        text='def entry(obj):\n    value = obj.old_api\n    return value\n'
        result=assess_scope(text.encode(),'entry',{'description':'Avoid old_api'},
                            {'expression':dict(start=text.index('obj.old_api'),end=len(text))})
        self.assertEqual(result['status'],'unknown')

    def test_no_covering_body_stops(self):
        self.assertEqual(self.assess(self.source,'Avoid old_api',body=False)['action'],'stop_no_covering_scope')

    def test_overwrite_clears_dataflow(self):
        text=self.source.replace('    if value:', '    value = 42\n    if value:')
        self.assertEqual(self.assess(text,'Avoid old_api')['status'],'unknown')

    def test_nested_function_does_not_trigger(self):
        text='def entry(obj):\n    def nested():\n        value = obj.old_api\n        if value:\n            return 1\n    return nested\n'
        result=assess_scope(text.encode(),'entry',{'description':'Avoid old_api'},
                            {'expression':dict(start=0,end=0)})
        self.assertEqual(result['status'],'unknown')

    def test_renamed_symbols_and_multiline_unicode_offsets(self):
        text=self.source.replace('old_api','deprecated_source').replace('value','result').replace('obj','source').replace('"legacy"','"中文"')
        begin=text.index('source.deprecated_source')
        report=assess_scope(text.encode(),'entry',{'description':'without deprecated_source'},
            {'expression':dict(start=begin,end=begin+len('source.deprecated_source')),
             'body':dict(start=text.index('    result'),end=len(text))})
        self.assertEqual(report['action'],'start_at_body')


if __name__=='__main__':unittest.main()
