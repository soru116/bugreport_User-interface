import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'experiments/generalization_v1'),str(ROOT)]
from audit_official_stage import assess


class StageTests(unittest.TestCase):
    def test_false_flag_can_coexist_with_clean_application(self):
        r=assess('Applied patch pkg/a.py cleanly.\nINTERNALERROR> AttributeError: absent\n', ['pkg/a.py'],False)
        self.assertTrue(r['model_application_confirmed_by_log'])
        self.assertEqual(r['phase'],'import_or_test_startup_failure_after_apply')
        self.assertFalse(r['patch_causality_verified'])

    def test_only_test_patch_does_not_confirm_model_apply(self):
        r=assess('Applied patch pkg/tests/a.py cleanly.\n',['pkg/a.py'],False)
        self.assertFalse(r['model_application_confirmed_by_log'])

    def test_diff_echo_does_not_count_as_application(self):
        r=assess('+Applied patch pkg/a.py cleanly.\n',['pkg/a.py'],False)
        self.assertFalse(r['model_application_confirmed_by_log'])

    def test_all_model_files_required_and_install_stage_distinct(self):
        text='Applied patch pkg/a.py cleanly.\nERROR: Command errored out\n'
        self.assertFalse(assess(text,['pkg/a.py','pkg/b.py'],False)['model_application_confirmed_by_log'])
        self.assertEqual(assess(text,['pkg/a.py'],False)['phase'],'installation_or_metadata_failure_after_apply')


if __name__=='__main__':unittest.main()
