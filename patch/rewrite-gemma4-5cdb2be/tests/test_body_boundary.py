import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fim_patch.body_boundary import extract_fixed_body
from fim_patch.generator import FIMPatchGenerator
from tests import test_fim


class BodyBoundaryTests(unittest.TestCase):
    def setUp(self):
        test_fim.PatchTests.setUp(self)

    def replay(self, text, stop='length', enabled=True):
        backend = test_fim.FakeBackend([text], stop_reason=stop)
        result = FIMPatchGenerator(backend, candidates=1, prompt_layout='compact_fim',
            fixed_signature_body=True, fixed_body_extraction=enabled, helper_contracts=True
            ).generate(self.ticket, self.loc, self.repo)
        self.assertEqual(backend.calls, 1)
        self.assertEqual((self.repo / 'app.py').read_bytes(), self.raw)
        return result

    def test_length_stop_with_complete_target_and_truncated_sibling_passes_all_gates(self):
        text = '    return "" if x is None else x.strip()\n\ndef g():\n    return (\n'
        self.assertEqual(self.replay(text, enabled=False)['patch_status'], 'no_valid_candidate')
        result = self.replay(text)
        self.assertEqual(result['patch_status'], 'generated')
        candidate = result['candidates'][0]
        self.assertEqual(candidate['stop_reason'], 'length')
        self.assertEqual(candidate['untrimmed_middle'], text)
        self.assertEqual(candidate['body_extraction']['status'], 'extracted')
        self.assertNotIn('def g', candidate['middle'])

    def test_no_boundary_does_not_override_length_stop(self):
        result = self.replay('    return "fixed"\n')
        self.assertEqual(result['patch_status'], 'no_valid_candidate')
        self.assertTrue(result['candidates'][0]['reason'].startswith('incomplete_body'))

    def test_noop_doc_only_and_undefined_name_still_rejected(self):
        for body, reason in [('    return x.strip()\n', 'no_executable_change'),
                             ('    """new docs"""\n    return x.strip()\n', 'no_executable_change'),
                             ('    return missing(x)\n', 'undefined')]:
            with self.subTest(reason=reason):
                result = self.replay(body + '\ndef next_fn():\n    return 1\n')
                self.assertEqual(result['patch_status'], 'no_valid_candidate')
                self.assertIn(reason, result['candidates'][0]['reason'])

    def test_strings_and_nested_definitions_are_not_boundaries(self):
        text = ('    text = """\ndef fake():\n    imaginary\n"""\n'
                '    def inner():\n        return text\n    return inner()\n\ndef sibling():\n    pass\n')
        middle, evidence = extract_fixed_body(text, 'def f():\n', 0)
        self.assertIn('def fake', middle)
        self.assertIn('def inner', middle)
        self.assertNotIn('def sibling', middle)
        self.assertEqual(evidence['boundary_evidence'], 'sibling_definition')

    def test_method_and_decorated_async_sibling(self):
        text = '        return x\n\n    @staticmethod\n    async def sibling(x):\n        return x\n'
        middle, evidence = extract_fixed_body(text, 'class A:\n    def f(self, x):\n', 4)
        self.assertNotIn('@staticmethod', middle)
        self.assertEqual(evidence['boundary_line'], 3)

    def test_truncated_target_and_wrong_scope_code_are_not_salvaged(self):
        for text in ['    return (\n\ndef sibling():\n    pass\n',
                     '    if True:\n\ndef sibling():\n    pass\n',
                     '    return x\nchanged = 1\ndef sibling():\n    pass\n',
                     '    return x\n@decorator(\n']:
            with self.subTest(text=text), self.assertRaises((ValueError, SyntaxError)):
                extract_fixed_body(text, 'def f(x):\n', 0)


if __name__ == '__main__':
    unittest.main()
