"""Pure AST diagnostics; no model, repository execution or semantic verdict."""
import importlib.util
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('operation_design',ROOT/'experiments/eval36_v7/diagnose_operation_contract.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

SOURCE='''class Value:
    # Conversion operator.
    def __lshift__(self, other):
        return convert(self, other)
    def __ilshift__(self, other):
        try:
            factor = scale(other)
        except ValueError:
            return NotImplemented
        self.store(factor)
        return self
'''
CHANGED='''class Value:
    # Conversion operator.
    def __lshift__(self, other):
        return convert(self, other)
    def __ilshift__(self, other):
        """Multiply the value."""
        return self * other
'''


class OperationDesignTests(unittest.TestCase):
    def diagnose(self,source=SOURCE,changed=CHANGED,ticket=None):
        return module.diagnose(source,changed,'Value.__ilshift__',ticket or {},adapters=('conversion',))

    def test_source_evidence_and_parameter_roles_produce_review_only(self):
        result=self.diagnose(ticket={'expected_behavior':'Return the appropriate converted value.',
                                     'test_patch':'SECRET_OFFICIAL_ANSWER','patch':'SECRET_REFERENCE_PATCH'})
        self.assertEqual(result['status'],'potential_operation_drift')
        self.assertEqual(result['source_facts']['related_source_purpose']['symbol'],'Value.__lshift__')
        self.assertEqual(result['source_facts']['removed_exception_handlers'],['ValueError'])
        self.assertIn('self.store',result['source_facts']['removed_calls'])
        self.assertFalse(result['automatic_rejection'])
        self.assertFalse(result['semantic_repair_verified'])
        self.assertIn('Public expected',result['feedback_preview'])
        self.assertLessEqual(len(result['feedback_preview']),600)
        self.assertNotIn('SECRET',str(result))

    def test_unchanged_and_conversion_factor_are_not_flagged_as_drift(self):
        self.assertEqual(self.diagnose(changed=SOURCE)['status'],'structural_evidence_only')
        source=SOURCE.replace('self.store(factor)','self.store(self * factor)')
        self.assertEqual(self.diagnose(changed=source)['status'],'structural_evidence_only')

    def test_traceback_keywords_cannot_invent_source_purpose(self):
        source=SOURCE.replace('# Conversion operator.','# Addition operator.')
        result=self.diagnose(source=source,ticket={'description':'UFuncError multiply conversion expected'})
        self.assertEqual(result['status'],'structural_evidence_only')
        self.assertEqual(result['public_expected_evidence'],[])

    def test_string_comment_and_unrelated_earlier_comment_are_not_evidence(self):
        source=SOURCE.replace('    # Conversion operator.','    """# Conversion operator."""')
        self.assertIsNone(self.diagnose(source=source)['source_facts']['related_source_purpose'])
        source=SOURCE.replace('    # Conversion operator.','    # Conversion operator.\n    x = 1')
        self.assertIsNone(self.diagnose(source=source)['source_facts']['related_source_purpose'])

    def test_missing_pair_stays_review_only_without_invented_label(self):
        source=SOURCE.replace('__lshift__','normal_method')
        self.assertEqual(self.diagnose(source=source)['status'],'structural_evidence_only')

    def test_dangerous_source_is_parsed_never_executed(self):
        import os
        from unittest.mock import patch
        source='import os\nos.system("must not run")\n'+SOURCE
        with patch.object(os,'system',side_effect=AssertionError('executed')) as system:
            self.diagnose(source=source)
            system.assert_not_called()


if __name__=='__main__':unittest.main()
