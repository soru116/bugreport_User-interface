from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'experiments/generalization_v1'))
from build_paired_public_plan import prompt_comparison


def preview(tokens,prefix='same',suffix='same',fits=True):
    return dict(status='captured',input_tokens=tokens,prefix_sha256=prefix,suffix_sha256=suffix,
                output_limit=128,fits_model_context=fits)


class PairedPublicPlanTests(unittest.TestCase):
    def test_real_prefix_effect_separated_from_constant_suffix_and_output(self):
        result=prompt_comparison(preview(100),preview(140,prefix='summary'))
        self.assertEqual(result['input_token_delta'],40)
        self.assertFalse(result['same_prefix'])
        self.assertTrue(result['same_suffix'] and result['same_output_limit'] and result['both_fit'])

    def test_unchanged_prompt_not_mistaken_for_actual_treatment(self):
        result=prompt_comparison(preview(100),preview(100))
        self.assertTrue(result['same_prefix'])
        self.assertEqual(result['input_token_delta'],0)

    def test_blocked_and_over_context_not_dropped_or_called_fit(self):
        blocked=prompt_comparison({'status':'blocked_before_first_prompt'},preview(100))
        self.assertEqual(blocked['control_status'],'blocked_before_first_prompt')
        self.assertNotIn('both_fit',blocked)
        self.assertFalse(prompt_comparison(preview(100),preview(200,fits=False))['both_fit'])


if __name__=='__main__':unittest.main()
