import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from fim_patch.helper_contracts import check_calls, context


class HelperContractsTests(unittest.TestCase):
    def source(self, call, helper='def h(x, /, y=None, *, flag=False):\n    return x\n'):
        return helper + '\ndef f(value):\n    return ' + call + '\n'

    def test_context_preserves_exact_source_signature(self):
        self.assertIn('h(x, /, y=None, *, flag=False)', context(self.source('h(value)'), 'f'))

    def test_changed_keyword_and_positional_shapes_are_checked(self):
        before = self.source('h(value)')
        for call in ('h(value, bad=1)', 'h(x=value)', 'h(value, 1, 2)', 'h()'):
            with self.subTest(call=call), self.assertRaisesRegex(ValueError, 'helper_call_incompatible'):
                check_calls(before, self.source(call), 'f')
        self.assertEqual(check_calls(before, self.source('h(value, flag=True)'), 'f')['changed_calls_checked'], 1)

    def test_varargs_and_kwargs_are_permitted(self):
        helper = 'def h(x, *args, **kwargs):\n    return x\n'
        check_calls(self.source('h(value)', helper), self.source('h(value, 1, bad=1)', helper), 'f')

    def test_dynamic_unpacking_is_not_guessed(self):
        before = self.source('h(value)')
        self.assertEqual(check_calls(before, self.source('h(*value, **value)'), 'f')['changed_calls_checked'], 0)

    def test_shadowing_decorators_and_rebindings_are_skipped(self):
        samples = ['def h(x):\n    return x\ndef f(h):\n    return h(bad=1)\n',
                   '@wrap\ndef h(x):\n    return x\ndef f():\n    return h(bad=1)\n',
                   'def h(x):\n    return x\nh = other\ndef f():\n    return h(bad=1)\n',
                   'def h(x):\n    return x\ndef f():\n    from other import h\n    return h(bad=1)\n']
        for source in samples:
            with self.subTest(source=source):
                self.assertEqual(context(source, 'f'), '')
                self.assertEqual(check_calls(source, source, 'f')['changed_calls_checked'], 0)

    def test_nested_scopes_and_unchanged_calls_are_skipped(self):
        source = self.source('h(value)')
        self.assertEqual(check_calls(source, source, 'f')['changed_calls_checked'], 0)
        nested = 'def h(x):\n    return x\ndef f():\n    def nested():\n        return h(bad=1)\n    return 1\n'
        self.assertEqual(context(nested, 'f'), '')

    def test_comprehension_shadowing_is_not_treated_as_module_helper(self):
        source='def h(x):\n    return x\ndef f(values):\n    return [h(bad=1) for h in values]\n'
        self.assertEqual(context(source,'f'),'')
        self.assertEqual(check_calls(source,source,'f')['changed_calls_checked'],0)

    def test_no_default_expression_is_executed(self):
        helper = 'def h(x, y=explode()):\n    return x\n'
        check_calls(self.source('h(value)', helper), self.source('h(value, y=2)', helper), 'f')


if __name__ == '__main__':
    unittest.main()
