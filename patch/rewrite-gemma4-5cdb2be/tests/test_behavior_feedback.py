"""Diagnostic tests; AST previews never establish patch/repair success."""
import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fim_patch.behavior_feedback import (diagnose_behavior, expression_preview, retry_feedback,
                                       public_expectations, assess_option_usage, option_retry_feedback)


class BehaviorFeedbackTests(unittest.TestCase):
    def diagnose(self, output, original='value.split(",")'):
        return diagnose_behavior(output, original, 'python_expression', ['separator'],
                                 {'expected_behavior': 'Split using the requested separator.'}, {})

    def test_complete_noop_before_invalid_continuation(self):
        result = self.diagnose('value.split(",")\n    wrong = helper(')
        self.assertTrue(result['no_op'])
        self.assertEqual(result['parameters_not_referenced'], ['separator'])
        self.assertFalse(result['candidate_accepted'])
        self.assertFalse(result['semantic_change_verified'])
        feedback = retry_feedback('incomplete_behavior: requires model end marker', result)
        for text in ('no_op', 'separator', 'Public expectation:', 'Split using', 'incomplete_behavior'):
            self.assertIn(text, feedback)
        self.assertLessEqual(len(feedback), 600)

    def test_multiline_balanced_expression_and_literal_newline(self):
        preview = expression_preview('[\n helper("x\\ny", separator)\n]\nwrong(')
        self.assertEqual(preview[0], '[\n helper("x\\ny", separator)\n]')
        result = self.diagnose('value.split(\n separator\n)\ncontinued(')
        self.assertFalse(result['no_op'])
        self.assertEqual(result['parameters_referenced'], ['separator'])
        self.assertEqual(result['parameters_not_referenced'], [])

    def test_partial_expression_cannot_claim_noop_or_unused_parameter(self):
        for value in ('value.split(', '[separator', 'value.split(","); other()', '# comment\n', ''):
            result = self.diagnose(value)
            self.assertIsNone(result['no_op'], value)
            self.assertIsNone(result['parameters_not_referenced'], value)
            self.assertEqual(result['status'], 'unknown_incomplete_or_invalid')

    def test_ast_changes_do_not_claim_semantic_change(self):
        result = self.diagnose('helper(value, header=header)', 'helper(value, header)')
        self.assertFalse(result['no_op'])
        self.assertFalse(result['semantic_change_verified'])
        self.assertEqual(result['call_targets_preview'], ['helper'])
        self.assertTrue(result['call_targets_unchanged'])
        self.assertIn('behavioral repair is not verified', retry_feedback('incomplete_behavior', result))
        self.assertIn('Same named call targets: helper', retry_feedback('incomplete_behavior', result))

    def test_public_call_comment_no_gold_or_unrelated_option(self):
        ticket = {'description': '# proposal\nitem.f(mode=True) # Return the requested alternate format.\n'
                                'item.g(other=True) # Unrelated expected behavior.\n',
                  'patch': 'SECRET', 'test_patch': 'SECRET', 'FAIL_TO_PASS': ['SECRET']}
        expected = public_expectations(ticket, {'evidence': [{'call': 'item.f(mode=True)'}]})
        self.assertEqual(expected, ['Return the requested alternate format.'])
        self.assertNotIn('SECRET', str(expected))
        ticket['description']='text = "item.f(mode=True)" # A string is not the chosen call.\n'
        self.assertEqual(public_expectations(ticket, {'evidence':[{'call':'item.f(mode=True)'}]}), [])
        ticket['description']='>>> result = item.f(mode = "alt") # Return alternate format.\n'
        self.assertEqual(public_expectations(ticket, {'evidence':[{'call':"item.f(mode='alt')"}]}),
                         ['Return alternate format.'])

    def test_long_public_units_omitted_whole_and_feedback_bounded(self):
        ticket = {'expected_behavior': 'x'*1000+'. Complete requirement.'}
        self.assertEqual(public_expectations(ticket, {}), ['Complete requirement.'])
        result = self.diagnose('value.split(",")')
        result['public_expectations'] = ['y'*1000, 'Short public requirement.']
        feedback = retry_feedback('helper_call_incompatible: helper(value, required): missing required', result)
        self.assertLessEqual(len(feedback), 600)
        self.assertNotIn('yyy', feedback)
        self.assertIn('Short public requirement.', feedback)
        self.assertIn('missing required', feedback)

    def test_statement_and_body_scope_diagnostics(self):
        result = diagnose_behavior('    parsed = value.split(",")\n', '    parsed=value.split(",")\n',
                                   'issue_statement', ['separator'], {}, {})
        self.assertTrue(result['no_op'])
        self.assertEqual(result['preview_kind'], 'complete_statement_sequence')
        result = diagnose_behavior('    return value.split(separator)\n', '    return value.split(",")\n',
                                   'function_body', ['separator'], {}, {})
        self.assertEqual(result['parameters_referenced'], ['separator'])

    def test_option_usage_hint_requires_public_expectation_and_no_indirect_usage(self):
        text='def f(value, *, option=False):\n    return helper(value)\n'
        hint=assess_option_usage(text,'f',['option'],['Return alternate format when requested.'])
        self.assertTrue(hint['retry_recommended'])
        self.assertFalse(hint['static_candidate_rejected'])
        self.assertFalse(hint['semantic_repair_verified'])
        self.assertIn('option',option_retry_feedback(hint))
        self.assertLessEqual(len(option_retry_feedback(hint)),600)
        self.assertFalse(assess_option_usage(text,'f',['option'],[])['retry_recommended'])
        indirect='def f(value, *, option=False):\n    return helper(value, locals())\n'
        hint=assess_option_usage(indirect,'f',['option'],['Return alternate format.'])
        self.assertFalse(hint['retry_recommended'])
        self.assertEqual(hint['status'],'unknown_indirect_usage')
        read='def f(value, *, option=False):\n    return helper(value, option)\n'
        self.assertFalse(assess_option_usage(read,'f',['option'],['Return alternate format.'])['retry_recommended'])


if __name__ == '__main__':
    unittest.main()
