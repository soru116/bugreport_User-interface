"""Scope proposals require public intent, target evidence and explicit bounds."""
import ast
import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from fim_patch.generator import function_span
from fim_patch.scope_planning import plan_scope,diagnose_expression_output


class ScopePlanningTests(unittest.TestCase):
    def plan(self,description,raw=b'def render(value):\n    return value\n',symbol='render',**kw):
        node=ast.parse(raw.decode()).body[0]
        if symbol!='render':node=node.body[0]
        span=function_span(raw,{'symbol_qualified_name':symbol,'start_line':node.lineno,'end_line':node.end_lineno})
        span['lock_signature']=True
        return plan_scope(raw,span,{'description':description,**kw},'app.py')

    def test_explicit_option_proposes_two_holes_with_call_cap(self):
        p=self.plan('Add a new parameter to render.\n```python\nrender(value, compact=True)\n```')
        self.assertEqual(p['status'],'incompatible_with_requested_option')
        self.assertEqual(p['proposed_parameters'],['compact'])
        self.assertEqual(len(p['proposal']['holes']),2)
        self.assertEqual(p['proposal']['generation_call_limit'],3)
        self.assertFalse(p['executable']);self.assertEqual(p['model_calls'],0)
        self.assertTrue(p['proposal_is_not_mandatory'])

    def test_unexpected_keyword_error_does_not_authorize_signature_change(self):
        p=self.plan('It raises TypeError: unexpected keyword compact.\n```python\nrender(value, compact=True)\n```')
        self.assertEqual(p['status'],'needs_requirement_review')
        self.assertNotIn('proposal',p)

    def test_benchmark_fields_never_influence_scope(self):
        base=self.plan('Rendering is broken.')
        other=self.plan('Rendering is broken.',patch='Add a new parameter.\n```python\nrender(x, secret=True)\n```',test_patch='gold',FAIL_TO_PASS=['oracle'])
        self.assertEqual(base,other)

    def test_alias_matches_target_class_and_other_class_does_not(self):
        raw=b'class Renderer:\n    def render(self, value):\n        return value\n'
        positive=self.plan('Add a new parameter.\n```python\nobj = Renderer.read("file")\nobj.render(value, compact=True)\n```',raw,'Renderer.render')
        self.assertEqual(positive['proposed_parameters'],['compact'])
        negative=self.plan('Add a new parameter.\n```python\nobj = OtherRenderer()\nobj.render(value, compact=True)\n```',raw,'Renderer.render')
        self.assertEqual(negative['status'],'no_explicit_conflict')

    def test_other_object_method_does_not_match_free_function(self):
        p=self.plan('Add a new parameter.\n```python\nother.render(value, compact=True)\n```')
        self.assertEqual(p['status'],'no_explicit_conflict')
        p=self.plan('Add a new parameter.\n```python\napp.render(value, compact=True)\n```')
        self.assertEqual(p['proposed_parameters'],['compact'])

    def test_var_keyword_and_existing_keyword_need_no_signature_proposal(self):
        for raw in (b'def render(value, **kwargs):\n    return value\n',
                    b'def render(value, compact=False):\n    return value\n'):
            p=self.plan('Add a new parameter.\n```python\nrender(value, compact=True)\n```',raw)
            self.assertEqual(p['action'],'retain_scope')
            self.assertFalse(p['semantic_compatibility_verified'])

    def test_positional_only_conflict_is_not_treated_as_new_parameter(self):
        p=self.plan('Add a new parameter.\n```python\nrender(value=1)\n```',b'def render(value, /):\n    return value\n')
        self.assertEqual(p['status'],'needs_requirement_review')
        self.assertNotIn('proposal',p)

    def test_too_many_or_conflicting_parameter_sets_stop_expansion(self):
        p=self.plan('Add new parameters.\n```python\nrender(value, a=True, b=True, c=True)\n```')
        self.assertEqual(p['action'],'stop_scope_expansion')
        p=self.plan('Add new parameters.\n```python\nrender(value, compact=True)\nrender(value, verbose=True)\n```')
        self.assertEqual(p['status'],'ambiguous_public_proposals')

    def test_option_boundary_does_not_inherit_prior_parameter_intent(self):
        p=self.plan('```python\n# Option A: add a new parameter\nrender(value, compact=True)\n# Option B: a different approach\nrender(value, alternate=True)\n```')
        self.assertEqual(p['proposed_parameters'],['compact'])
        self.assertFalse(p['evidence'][1]['explicit_add_parameter'])

    def test_broken_examples_are_not_executed_or_joined(self):
        p=self.plan('Add a new parameter.\n```python\nrender(value, compact=\nTrue)\nbroken [\n```')
        self.assertNotIn('proposal',p)

    def test_saved_complete_noop_and_continuation_are_diagnostic_only(self):
        d=diagnose_expression_output({'raw_response':'[make(value)]\nif unrelated:\n'},'[make(value)]')
        self.assertTrue(d['first_line_no_op']);self.assertTrue(d['continuation_after_first_line'])
        self.assertFalse(d['candidate_accepted']);self.assertIn('unchanged',d['feedback'])
        d=diagnose_expression_output({'raw_response':'[make(\nvalue)]'},'[make(value)]')
        self.assertFalse(d['first_line_expression_available'])


if __name__=='__main__':unittest.main()
