import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fim_patch.generator import FIMPatchGenerator, git
from fim_patch.multi_function import MultiFunctionRepairGenerator, related_chain
from tests import test_fim, test_iterative_repair


class MultiFunctionTests(unittest.TestCase):
    def setUp(self):
        test_fim.PatchTests.setUp(self)
        self.raw = b'def f(x):\n    return helper(x)\n\ndef helper(x):\n    return x.strip()\n\ndef unrelated(x):\n    return x\n'
        (self.repo / 'app.py').write_bytes(self.raw)
        git(self.repo, 'add', 'app.py')
        git(self.repo, '-c', 'user.name=T', '-c', 'user.email=t@example.invalid', 'commit', '-m', 'helpers')
        base = git(self.repo, 'rev-parse', 'HEAD').decode().strip()
        self.ticket['base_commit'] = base
        self.loc['base_commit'] = base
        self.loc['stage3_ranked_symbols'] += [
            {'file_path': 'app.py', 'symbol_qualified_name': 'unrelated', 'start_line': 7, 'end_line': 8}]

    def configure(self, outputs, validator=None, calls=3, extraction=False):
        backend = test_iterative_repair.RepairTests.backend(self, outputs)
        inner = FIMPatchGenerator(backend, candidates=1, research_mode=True, target_policy='related_targets_top5',
            prompt_layout='compact_fim', fixed_signature_body=True, helper_contracts=True,
            helper_implementations=True, validator=validator, fixed_body_extraction=extraction)
        return backend, inner, MultiFunctionRepairGenerator(inner, calls)

    def test_two_same_file_bodies_form_one_working_patch_and_preserve_inputs(self):
        before = copy.deepcopy(self.loc)
        backend, inner, repair = self.configure([
            '    return "" if x is None else helper(x)\n',
            '    return x.strip().lower()\n'])
        result = repair.generate(self.ticket, self.loc, self.repo)
        self.assertEqual(result['patch_status'], 'generated')
        self.assertEqual(backend.calls, 2)
        self.assertEqual(result['patch'].count('--- a/app.py'), 1)
        self.assertEqual({x['function'] for x in result['changed_functions']}, {'f', 'helper'})
        self.assertNotIn('unrelated', [x['row']['symbol_qualified_name'] for x in result['repair_plan']])
        self.assertIn('Proposed body for f', backend.prompts[1])
        self.assertEqual(self.loc, before)
        self.assertEqual((self.repo / 'app.py').read_bytes(), self.raw)
        self.assertEqual(inner.target_policy, 'related_targets_top5')
        self.assertEqual(inner.seed, 42)
        git(self.repo, 'apply', data=result['patch'].encode())
        namespace = {}
        exec((self.repo / 'app.py').read_text(), namespace)
        self.assertEqual(namespace['f'](None), '')
        self.assertEqual(namespace['f'](' HeLLo '), 'hello')
        self.assertEqual(namespace['unrelated'](5), 5)

    def test_combined_test_failure_feedback_replaces_body_within_total_limit(self):
        patches = []
        def validator(patch, repo):
            patches.append(patch)
            self.assertIn('return "" if x is None', patch)
            self.assertIn('return x.strip().', patch)
            return ({'status': 'failed', 'official_resolved': False, 'feedback': 'AssertionError in helper: expected lower'}
                    if len(patches) == 1 else {'status': 'plausible', 'official_resolved': True})
        backend, _, repair = self.configure([
            '    return "" if x is None else helper(x)\n',
            '    return x.strip().upper()\n',
            '    return x.strip().lower()\n'], validator)
        result = repair.generate(self.ticket, self.loc, self.repo)
        self.assertEqual(backend.calls, 3)
        self.assertEqual(len(patches), 2)
        self.assertTrue(result['official_resolved'])
        self.assertEqual(result['repair_stop_reason'], 'tests_passed')
        self.assertIn('AssertionError in helper', backend.prompts[2])
        self.assertNotIn('+    return x.strip().upper()', result['patch'])
        self.assertEqual((self.repo / 'app.py').read_bytes(), self.raw)

    def test_extracted_runtime_failure_uses_real_tests_and_remaining_call(self):
        from fim_patch.validation import UnittestValidator
        self.raw = (b'import re\n\ndef f(x):\n    return helper(x) if aux(x) else False\n\n'
                    b'def helper(x):\n    return bool(re.fullmatch("UPPER", x))\n\n'
                    b'def aux(x):\n    return True\n')
        (self.repo / 'app.py').write_bytes(self.raw)
        (self.repo / 'test_spec.py').write_text(
            'import unittest\nfrom app import f\nclass Behaviour(unittest.TestCase):\n'
            '    def test_lower(self):\n        self.assertTrue(f("upper"))\n'
            '    def test_upper(self):\n        self.assertTrue(f("UPPER"))\n', encoding='utf-8')
        git(self.repo, 'add', '.')
        git(self.repo, '-c', 'user.name=T', '-c', 'user.email=t@example.invalid', 'commit', '-m', 'runtime fixture')
        base = git(self.repo, 'rev-parse', 'HEAD').decode().strip()
        self.ticket.update(base_commit=base, description='helper should accept lowercase commands')
        self.loc['base_commit'] = base
        self.loc['stage3_ranked_symbols'] = [{'file_path': 'app.py', 'symbol_qualified_name': 'f', 'start_line': 3, 'end_line': 4}]
        validator = UnittestValidator({'expected_failure': 'False is not true',
            'reproducer': {'names': ['test_spec.Behaviour.test_lower']},
            'regression': {'names': ['test_spec.Behaviour.test_upper']}})
        backend, _, repair = self.configure([
            '    return helper(x) if aux(x) else False\n\ndef sibling():\n    pass\n',
            '    return bool(re.fullmatch("(", x))\n\ndef sibling():\n    return (\n',
            '    return bool(re.fullmatch("UPPER", x, re.I))\n\ndef sibling():\n    pass\n'
        ], validator, extraction=True)
        backend.stop_reason = 'length'
        result = repair.generate(self.ticket, self.loc, self.repo)
        self.assertEqual(backend.calls, 3)
        self.assertEqual([h['span']['symbol'] for h in result['repair_history']], ['f', 'helper', 'helper'])
        self.assertEqual(result['validation_schedule'], 'after_each_changed_body')
        self.assertEqual(result['repair_history'][1]['combined_validation']['status'], 'failed')
        self.assertIn('unterminated subpattern', backend.prompts[2])
        self.assertEqual(result['patch_status'], 'generated')
        self.assertEqual(result['validation_status'], 'plausible')
        self.assertEqual(result['repair_stop_reason'], 'tests_passed')
        self.assertIsNone(result['official_resolved'])
        self.assertEqual((self.repo / 'app.py').read_bytes(), self.raw)
        for history in result['repair_history']:
            self.assertEqual(history['candidates'][0]['stop_reason'], 'length')
            self.assertEqual(history['candidates'][0]['body_extraction']['status'], 'extracted')

    def test_noops_exhaust_budget_without_accepting_unrelated_siblings(self):
        backend, _, repair = self.configure([
            '    return helper(x)\n',
            '    return x.strip()\n',
            '    return "changed"\n\ndef unrelated(x):\n    return "changed"\n'])
        result = repair.generate(self.ticket, self.loc, self.repo)
        self.assertEqual(backend.calls, 3)
        self.assertEqual(result['patch_status'], 'no_valid_candidate')
        self.assertEqual(result['patch'], '')
        self.assertIn('body_contract_violation', result['repair_history'][-1]['candidates'][0]['reason'])

    def test_environment_errors_stop_and_do_not_publish_component_patch(self):
        def validator(*args):
            return {'status': 'error', 'reason': 'docker unavailable'}
        backend, _, repair = self.configure([
            '    return "" if x is None else helper(x)\n',
            '    return x.strip().lower()\n'], validator)
        result = repair.generate(self.ticket, self.loc, self.repo)
        self.assertEqual(backend.calls, 2)
        self.assertEqual(result['repair_stop_reason'], 'test_environment_error')
        self.assertEqual(result['patch'], '')
        self.assertIsNone(result['official_resolved'])

    def test_budget_and_timeout_do_not_spend_remaining_calls(self):
        backend, _, repair = self.configure([])
        def timeout(*args, **kwargs):
            backend.calls += 1
            raise ValueError('timed out')
        backend.infill = timeout
        result = repair.generate(self.ticket, self.loc, self.repo)
        self.assertEqual(backend.calls, 1)
        self.assertEqual(result['repair_stop_reason'], 'backend_environment_error')

    def test_preflight_selection_makes_no_backend_calls(self):
        backend, inner, _ = self.configure([])
        backend.describe = lambda: self.fail('planning must not describe backend')
        result = inner.generate(self.ticket, self.loc, self.repo, plan_only=True)
        self.assertEqual(result['patch_status'], 'planned')
        self.assertEqual(backend.calls, 0)

    def test_method_edges_exclude_rebound_receiver_and_decorated_helpers(self):
        source = ('class A:\n    def f(self, x):\n        return self.helper(x)\n'
                  '    def helper(self, x):\n        return x\n')
        self.assertEqual([e['symbol'] for e in related_chain(source, 'A.f')], ['A.helper'])
        self.assertEqual(related_chain(source.replace('return self.helper', 'self = x\n        return self.helper'), 'A.f'), [])
        self.assertEqual(related_chain(source.replace('    def helper', '    @property\n    def helper'), 'A.f'), [])
        self.assertEqual(related_chain(source.replace('return self.helper', 'self.helper = x\n        return self.helper'), 'A.f'), [])

    def test_predicted_caller_of_leaf_is_included_only_with_source_evidence(self):
        self.loc['stage3_ranked_symbols'] = [
            {'file_path': 'app.py', 'symbol_qualified_name': 'helper', 'start_line': 4, 'end_line': 5},
            {'file_path': 'app.py', 'symbol_qualified_name': 'f', 'start_line': 1, 'end_line': 2},
            {'file_path': 'app.py', 'symbol_qualified_name': 'unrelated', 'start_line': 7, 'end_line': 8}]
        backend, _, repair = self.configure(['    return x.strip().lower()\n',
            '    return "" if x is None else helper(x)\n'])
        result = repair.generate(self.ticket, self.loc, self.repo)
        self.assertEqual(result['patch_status'], 'generated')
        self.assertEqual([p['row']['symbol_qualified_name'] for p in result['repair_plan']], ['helper', 'f'])
        self.assertEqual(backend.calls, 2)

    def test_module_or_missing_fl_symbols_do_not_abort_related_plan(self):
        self.loc['stage3_ranked_symbols'] += [
            {'file_path': 'app.py', 'symbol_qualified_name': '<module>', 'start_line': 1, 'end_line': 8},
            {'file_path': 'app.py', 'symbol_qualified_name': 'missing', 'start_line': 1, 'end_line': 2}]
        backend, _, repair = self.configure(['    return helper(x).lower()\n', '    return x.strip().upper()\n'])
        result = repair.generate(self.ticket, self.loc, self.repo)
        self.assertEqual(result['patch_status'], 'generated')
        self.assertEqual(backend.calls, 2)
        self.assertEqual({r['symbol'] for r in result['plan_rejections']}, {'<module>', 'missing'})

    def test_failed_official_verdict_is_retained_without_publishing_patch(self):
        validator = lambda *args: {'status': 'failed', 'official_resolved': False, 'feedback': 'AssertionError in helper'}
        backend, _, repair = self.configure(['    return helper(x).lower()\n',
            '    return x.strip().upper()\n'], validator, calls=2)
        result = repair.generate(self.ticket, self.loc, self.repo)
        self.assertEqual(backend.calls, 2)
        self.assertFalse(result['official_resolved'])
        self.assertEqual(result['validation_status'], 'failed')
        self.assertEqual(result['patch'], '')

    def test_method_changed_arguments_are_checked_after_composition(self):
        self.raw = b'class A:\n    def f(self, x):\n        return self.helper(x)\n    def helper(self, x):\n        return x\n'
        (self.repo / 'app.py').write_bytes(self.raw)
        git(self.repo, 'add', 'app.py')
        git(self.repo, '-c', 'user.name=T', '-c', 'user.email=t@example.invalid', 'commit', '-m', 'class')
        base = git(self.repo, 'rev-parse', 'HEAD').decode().strip()
        self.ticket['base_commit'] = base; self.loc['base_commit'] = base
        self.loc['stage3_ranked_symbols'] = [{'file_path': 'app.py', 'symbol_qualified_name': 'A.f', 'start_line': 2, 'end_line': 3}]
        backend, _, repair = self.configure(['        return self.helper(x, bad=True)\n',
            '        return str(x)\n'], calls=2)
        result = repair.generate(self.ticket, self.loc, self.repo)
        self.assertEqual(result['patch_status'], 'no_valid_candidate')
        self.assertIn('helper_call_incompatible', result['repair_history'][-1]['combined_rejection'])

    def test_crlf_bom_and_shifted_offsets_are_composed_against_base(self):
        self.raw = b'\xef\xbb\xbf' + self.raw.replace(b'\n', b'\r\n')
        (self.repo / 'app.py').write_bytes(self.raw)
        git(self.repo, 'add', 'app.py')
        git(self.repo, '-c', 'user.name=T', '-c', 'user.email=t@example.invalid', 'commit', '-m', 'encoding')
        base = git(self.repo, 'rev-parse', 'HEAD').decode().strip()
        self.ticket['base_commit'] = base
        self.loc['base_commit'] = base
        backend, _, repair = self.configure([
            '    if x is None:\n        return ""\n    return helper(x)\n',
            '    return x.strip().lower()\n'])
        result = repair.generate(self.ticket, self.loc, self.repo)
        self.assertEqual(result['patch_status'], 'generated')
        git(self.repo, 'apply', data=result['patch'].encode())
        after = (self.repo / 'app.py').read_bytes()
        self.assertTrue(after.startswith(b'\xef\xbb\xbf'))
        self.assertNotIn(b'\n', after.replace(b'\r\n', b''))
        self.assertIn(b'def unrelated(x):\r\n    return x', after)


if __name__ == '__main__':
    unittest.main()
