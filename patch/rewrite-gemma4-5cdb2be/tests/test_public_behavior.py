import ast
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fim_patch.public_behavior import expectation_evidence, diagnose_expression, feedback_text, VERSION, FEEDBACK_VERSION
from fim_patch.generator import issue_brief
from fim_patch.iterative import IterativeRepairGenerator, feedback_block
from tests import test_local_fim as fixtures


class PublicEvidenceTests(unittest.TestCase):
    def test_inline_reproducer_provenance_and_dedup(self):
        body = '```python\nIn [13]: x == None  # Should be False\n```'
        evidence = expectation_evidence({'description':body, 'bug_report':body})
        self.assertEqual(evidence, [dict(field='description',line=2,kind='inline_expected_comment',
                                        text='x == None  # Should be False')])
        brief = issue_brief({'description':body}, infer_expected=True)
        self.assertIn('Public evidence (description:2): x == None  # Should be False', brief)
        self.assertNotIn('Expected behaviour --- Not stated.', brief)
        self.assertIn('Expected behaviour --- Not stated.', issue_brief({'description':body}))

    def test_allowlist_strings_and_unknowns(self):
        for body in ('```python\nx = "# Should be False"\n```',
                     '```python\n"x == None" # Should be False\n```',
                     '```python\ns = """\nx == None # Should be False\n"""\n```',
                     'Support new clip ufunc', '<!--\n>>> x == None # Should be False\n-->'):
            self.assertEqual(expectation_evidence({'description':body,'test_patch':'>>> x # Should be False',
                                                  'patch':'SECRET','FAIL_TO_PASS':['SECRET']}), [])

    def test_explicit_fields_headings_and_no_partial_units(self):
        evidence = expectation_evidence({'expected_behavior':'a'*401, 'description':
            '### Expected behavior\nReturn an empty list.\n### Actual behavior\nRaises an error.'})
        self.assertEqual([e['text'] for e in evidence], ['Return an empty list.'])
        self.assertEqual(evidence[0]['line'],2)
        self.assertEqual(expectation_evidence({'description':'```python\nx # Should be False'}), [])

    def test_evidence_respects_requirement_allowlist_and_budget(self):
        ticket = {'title':'crash', 'description':'```python\nx # Should be False\n```'}
        self.assertNotIn('Should be False',issue_brief(ticket,requirement_keys=('title',),infer_expected=True))
        brief=issue_brief(ticket,max_chars=100,infer_expected=True)
        self.assertNotIn('Expected behaviour --- Not stated.',brief)
        self.assertIn('Not retained within context budget.',brief)

    def test_first_expression_diagnosis_never_salvages(self):
        ticket={'description':'>>> x == None # Should be False'}
        for middle in ('f(x)', 'f(x)\n    def invented():\n        pass'):
            d=diagnose_expression({'middle':middle},'f(x)',ticket)
            self.assertTrue(d['first_expression_ast_unchanged'])
            self.assertFalse(d['candidate_accepted'])
            self.assertIn('Should be False',feedback_text(d))
            self.assertLessEqual(len(feedback_text(d)),600)
        d=diagnose_expression({'middle':'f('},'f(x)',ticket)
        self.assertIsNone(d['first_expression_ast_unchanged'])
        self.assertNotIn('matches the original',feedback_text(d))

    def test_feedback_sanitizes_fim_markers(self):
        d=diagnose_expression({'middle':'f(x)'},'f(x)',{'expected_behavior':'Return <MID> literally.'})
        text=feedback_block({'reason':'no_op','public_behavior_diagnostic':d},compact=True)
        self.assertNotIn('<MID>',text)
        self.assertIn('marker removed',text)


class PublicFeedbackIntegrationTests(unittest.TestCase):
    setUp = fixtures.LocalFIMTests.setUp
    replace_source = fixtures.LocalFIMTests.replace_source

    def test_two_bounded_rounds_receive_evidence_and_keep_noop_gate(self):
        self.ticket['description'] += '\n```python\nparse("a;b", ";") # Should return two items.\n```'
        backend, generator = fixtures.LocalFIMTests.generator(self,
            ['cleaned.split(",")','cleaned.split(",")'], local_fim_expressions=True)
        result=IterativeRepairGenerator(generator,2).generate(self.ticket,self.loc,self.repo)
        self.assertEqual(backend.calls,2)
        self.assertEqual(result['static_candidate_count'],0)
        self.assertEqual(result['repair_feedback_version'],FEEDBACK_VERSION)
        self.assertIn(VERSION,result['prompt_version'])
        for prefix,suffix in backend.prompts:
            self.assertIn('Should return two items.',prefix)
            self.assertTrue(prefix.endswith('    parsed = '))
            self.assertTrue(suffix.startswith('\n    return parsed'))
        self.assertIn('first complete expression matches the original AST',backend.prompts[1][0])
        self.assertNotIn('expected',result['repair_history'][0]['candidates'][0]['repair_diagnostic']
                         ['requirement_audit']['not_explicitly_stated'])
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)


if __name__ == '__main__':
    unittest.main()
