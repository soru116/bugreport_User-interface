import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from fim_patch.iterative import test_failure_excerpt,feedback_block

class FailureExcerptTests(unittest.TestCase):
    def test_ansi_exception_survives_docstring_diff_at_end(self):
        actual=('astropy/io/ascii/qdp.py:64: in _line_type\n'
                '\x1b[31mE    re.error: missing ), unterminated subpattern at position 4\x1b[0m\n'
                '\x1b[31mFAILED\x1b[0m test_qdp.py::test_roundtrip[True] - re.error: broken regex\n')
        diff=('diff --git a/qdp.py b/qdp.py\n--- a/qdp.py\n+++ b/qdp.py\n@@ -1 +1 @@\n'
              '+    >>> _line_type("gibberish")\n+    Traceback (most recent call last):\n'
              '+    ValueError: Unrecognized QDP line\n')*100
        log=actual+diff+'2026-10-04 14:44:32,195 - INFO - container removed\n'
        excerpt=test_failure_excerpt(log)
        compact=feedback_block({'reason':'tests_failed','validation':{'feedback':excerpt}},compact=True)
        for text in ('re.error: missing ), unterminated subpattern','test_roundtrip[True]', 'qdp.py:64: in _line_type'):
            self.assertIn(text,excerpt);self.assertIn(text,compact)
        self.assertNotIn('ValueError: Unrecognized',compact)
        self.assertNotIn('\x1b[',compact)
    def test_actual_tests_after_diff_are_still_visible(self):
        log='--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n+AssertionError: example\nTraceback (most recent call last):\n  File \"app.py\", line 7, in f\nValueError: actual failure\n'
        excerpt=test_failure_excerpt(log)
        self.assertIn('ValueError: actual failure',excerpt)
        self.assertNotIn('AssertionError: example',excerpt)

    def test_many_failures_leave_room_for_stack_frame(self):
        log='E AssertionError: expected empty string\napp.py:25: in normalize\n'+''.join(
            'FAILED tests/test_app.py::test_case_'+str(i)+' - AssertionError: bad value\n' for i in range(80))
        excerpt=test_failure_excerpt(log,limit=1200)
        self.assertLessEqual(len(excerpt),1200)
        self.assertIn('AssertionError: expected empty string',excerpt)
        self.assertIn('FAILED tests/test_app.py::test_case_0',excerpt)
        self.assertIn('app.py:25: in normalize',excerpt)
    def test_standard_unittest_trace_and_noncompact_feedback(self):
        log='Traceback (most recent call last):\n  File "app.py", line 7, in normalize\nAssertionError: None should become empty\n'
        compact=test_failure_excerpt(log,limit=1200)
        self.assertIn('File "app.py", line 7',compact)
        full=feedback_block({'reason':'failed','validation':{'feedback':log+'noise\n'*3000}})
        self.assertIn('AssertionError: None should become empty',full)
    def test_no_failure_preserves_environment_diagnostic(self):
        self.assertEqual(test_failure_excerpt('connection refused'), 'connection refused')
        self.assertEqual(test_failure_excerpt('text',limit=0),'')
    def test_diff_examples_are_not_real_assertion_errors(self):
        log='--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n+AssertionError: example only\n'
        self.assertEqual(test_failure_excerpt(log),'')

if __name__=='__main__':unittest.main()
