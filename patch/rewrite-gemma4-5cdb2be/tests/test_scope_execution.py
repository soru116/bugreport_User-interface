"""Offline controlled-backend checks, not evidence of model repair success."""
import json
import tempfile
import unittest
from pathlib import Path
from tests import test_local_fim as fixtures
from fim_patch.generator import git
from fim_patch.iterative import IterativeRepairGenerator
from scripts.run_stage4_batch import build_parser, build_run_config, validate_resume_config, make_generator
from fim_patch.cli import validate_generation_args
from fim_patch.scope_execution import (VERSION,signature_anchor,scope_brief,
                                       parameter_fragment,check_additive_signature,extract_signature)
from fim_patch.scope_planning import plan_scope


class ScopeExecutionTests(unittest.TestCase):
    setUp = fixtures.LocalFIMTests.setUp
    replace_source = fixtures.LocalFIMTests.replace_source

    def generator(self, outputs):
        self.ticket['description'] = ('Add a new parameter for split behavior.\n```python\n'
                                      'parse("a;b", separator=";")\n```')
        return fixtures.LocalFIMTests.generator(self,outputs,local_fim_expressions=True,
                                               scope_adjustment=True,research_mode=True)

    def test_atomic_success_preserves_source_and_old_call(self):
        b,g = self.generator(['separator=None','cleaned.split(separator or delimiter or ",")'])
        result = g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(result['patch_status'],'generated',result)
        self.assertEqual(result['static_candidate_count'],1)
        self.assertEqual(result['model_calls'],2)
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)
        self.assertIn('separator=None',b.prompts[1][0])
        self.assertTrue(b.prompts[1][0].endswith('parsed = '))
        self.assertEqual(len(result['scope_history']),2)
        self.assertTrue(b.prompts[0][0].endswith(', *, '))
        self.assertTrue(b.prompts[0][1].startswith('):'))
        self.assertEqual(result['scope_execution'],VERSION)
        git(self.repo,'apply',data=result['patch'].encode())
        ns={};exec((self.repo/'app.py').read_bytes(),ns)
        self.assertEqual(ns['parse']('a,b'),['a','b'])
        self.assertEqual(ns['parse']('a;b',separator=';'),['a','b'])

    def test_noop_retries_with_feedback_then_succeeds(self):
        b,g = self.generator(['separator=None','cleaned.split(",")',
                               'cleaned.split(separator or ",")'])
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'generated',r)
        self.assertEqual(r['model_calls'],3)
        self.assertIn('no_op',b.prompts[2][0])
        self.assertEqual(r['scope_history'][1]['status'],'rejected')

    def test_repeated_noop_never_accepts_signature_only(self):
        b,g=self.generator(['separator=None','cleaned.split(",")','cleaned.split(",")'])
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(b.calls,3)
        self.assertFalse(r['generated_before_validation'])
        self.assertEqual(r['patch'],'')
        self.assertNotIn('patch',r['candidates'][0])
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)

    def test_invalid_signature_stops_before_behavior(self):
        for fragment in (', separator=None','separator','other=None',
                         'separator=missing()','separator: int=None',
                         'separator=(None'):
            b,g=self.generator([fragment])
            r=g.generate(self.ticket,self.loc,self.repo)
            self.assertEqual(b.calls,1,fragment)
            self.assertFalse(r['generated_before_validation'],r)
            self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)

    def test_scope_escape_and_unknown_name_are_rejected_with_bound(self):
        for output in ('cleaned.split(separator)\ndef evil(): pass','missing(cleaned,separator)'):
            b,g=self.generator(['separator=None',output,output])
            r=g.generate(self.ticket,self.loc,self.repo)
            self.assertEqual(b.calls,3)
            self.assertFalse(r['generated_before_validation'])
            self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)

    def test_helper_binding_check_after_merge(self):
        raw=b'def parse(value):\n    parsed = split(value)\n    return parsed\n\ndef split(value):\n    return value.split(",")\n'
        self.replace_source(raw,'parse',1,3)
        b,g=self.generator(['separator=None','split(value,separator)','split(value,separator)'])
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertFalse(r['generated_before_validation'])
        self.assertIn('helper_call_incompatible',r['candidates'][0]['reason'])

    def test_unicode_multiline_header_crlf_bom_rebased(self):
        raw=('\ufeffdef parse(\r\n    value,\r\n    delimiter=None\r\n):\r\n'
             '    """原始說明。"""\r\n    名稱 = value.split(",")  # 保留\r\n    return 名稱\r\n').encode('utf8')
        self.replace_source(raw,'parse',1,7)
        b,g=self.generator(['separator=(\n    None\n)','value.split(separator or delimiter or ",")'])
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'generated',r)
        self.assertTrue(b.prompts[1][0].endswith('名稱 = '))
        git(self.repo,'apply',data=r['patch'].encode())
        after=(self.repo/'app.py').read_bytes()
        self.assertTrue(after.startswith(b'\xef\xbb\xbf'))
        self.assertNotIn(b'\n',after.replace(b'\r\n',b''))
        self.assertIn('# 保留'.encode(),after)

    def test_existing_kwonly_and_async_decorator_retained(self):
        raw=b'@staticmethod\nasync def parse(value, *, delimiter=None) -> list:\n    parsed = value.split(",")\n    return parsed\n'
        self.replace_source(raw,'parse',2,4)
        _,g=self.generator(['separator=None','value.split(separator or delimiter or ",")'])
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'generated',r)
        self.assertNotIn('-@staticmethod',r['patch'])

    def test_length_signature_does_not_retry(self):
        b,g=self.generator(['separator=None']);b.stop_reason='length'
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(b.calls,1)
        self.assertFalse(r['generated_before_validation'])
        self.assertIn('incomplete_signature',r['candidates'][0]['reason'])

    def test_no_explicit_conflict_retains_single_hole(self):
        b,g=self.generator(['cleaned.split(delimiter or ",")'])
        self.ticket['description']='Use delimiter when calling split.'
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'generated',r)
        self.assertEqual(b.calls,1)
        self.assertEqual(r['scope_plan']['action'],'retain_scope')

    def test_plan_only_never_calls_backend(self):
        b,g=self.generator([])
        b.describe=lambda: self.fail('describe in plan-only')
        r=g.generate(self.ticket,self.loc,self.repo,plan_only=True)
        self.assertEqual(r['patch_status'],'planned')
        self.assertEqual(r['scope_plan']['action'],'propose_signature_and_behavior_fim')
        self.assertEqual(b.calls,0)

    def test_outer_rounds_cannot_multiply_budget(self):
        _,g=self.generator([])
        with self.assertRaisesRegex(ValueError,'three-call budget'):
            IterativeRepairGenerator(g,2)

    def test_optin_configuration_and_resume_isolation(self):
        parser=build_parser()
        args=parser.parse_args(['--tickets','x','--localizations','y','--output','out','--research-mode','--candidates','1',
                               '--local-fim','--local-fim-expressions','--scope-adjustment',
                               '--prompt-layout','compact_fim'])
        validate_generation_args(args,parser)
        self.assertTrue(make_generator(args).scope_adjustment)
        config=build_run_config(args,[])
        self.assertEqual(config['scope_generation_call_limit'],3)
        self.assertEqual(config['scope_adjustment'],VERSION)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'run_config.json';path.write_text(json.dumps(config))
            for old_version in ('two-hole-fim-v1','two-hole-fim-v2','two-hole-fim-v3','two-hole-fim-v4','two-hole-fim-v5'):
                old=dict(config,scope_adjustment=old_version)
                path.write_text(json.dumps(old))
                with self.assertRaisesRegex(ValueError,'mismatch'):
                    validate_resume_config(path,config)
            path.write_text(json.dumps(config))
            args.scope_adjustment=False
            with self.assertRaisesRegex(ValueError,'mismatch'):
                validate_resume_config(path,build_run_config(args,[]))
        args.scope_adjustment=True;args.repair_rounds=2
        with self.assertRaises(SystemExit):validate_generation_args(args,parser)

    def test_test_failure_retains_only_combined_static_candidate(self):
        _,g=self.generator(['separator=None','cleaned.split(separator or ",")'])
        g.validator=lambda patch,repo:{'status':'failed','feedback':'assertion failed'}
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'no_valid_candidate')
        self.assertTrue(r['generated_before_validation'])
        self.assertEqual(r['static_candidate_count'],1)
        self.assertEqual(r['model_calls'],2)

    def test_validator_environment_error_does_not_generate_again(self):
        b,g=self.generator(['separator=None','cleaned.split(separator or ",")'])
        def unavailable(*args):raise OSError('test environment unavailable')
        g.validator=unavailable
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(b.calls,2)
        self.assertEqual(r['patch'],'')
        self.assertTrue(r['generated_before_validation'])
        self.assertEqual(r['validation_status'],'error')

    def test_snapshot_changes_stop_before_candidate_is_accepted(self):
        b,g=self.generator(['separator=None','cleaned.split(separator or ",")'])
        original=b.infill
        def changing(*args,**kwargs):
            response=original(*args,**kwargs)
            if b.calls==2:(self.repo/'app.py').write_bytes(self.raw+b'# external edit\n')
            return response
        b.infill=changing
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(b.calls,2)
        self.assertFalse(r['generated_before_validation'])
        self.assertIn('source_snapshot_recheck_failed',r['candidates'][0]['reason'])

    def test_ambiguous_proposals_stop_without_inference(self):
        b,g=self.generator([])
        self.ticket['description']+='\nAdd a new parameter.\n```python\nparse("a", other=True)\n```'
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(b.calls,0)
        self.assertEqual(r['patch_status'],'blocked_scope_expansion')
        self.assertFalse(r['generated_before_validation'])

    def test_anchor_handles_empty_posonly_vararg_comments_and_nested_defaults(self):
        for header,separator in (
            ('def f():','*, '),
            ('def f(value, /):',', *, '),
            ('def f(value, *rest):',', '),
            ('def f(value, *, mode=None):',', '),
            ('def f(value,\n):',' *, '),
            ('def f(value,  # keep comment\n):',' *, '),
            ('def f(value=(1, (2, 3))):',', *, ')):
            source=header+'\n    return 1\n'
            pos,actual,_=signature_anchor(source,'f')
            self.assertEqual(actual,separator,header)
            draft=source[:pos]+actual+'separator=None'+source[pos:]
            check_additive_signature(source,draft,'f',['separator'],signature_only=True)
        with self.assertRaisesRegex(ValueError,'kwargs'):
            signature_anchor('def f(**kwargs):\n    return 1\n','f')

    def test_parameter_contract_rejects_host_separators_and_continuations(self):
        for value in (', separator=None','*, separator=None','separator=None):\n    pass',
                      'separator=None # explain','separator=(', '**kwargs'):
            with self.assertRaises(ValueError,msg=value):parameter_fragment(value)
        self.assertEqual(parameter_fragment('separator=None, other=False'),'separator=None, other=False')
        self.assertEqual(parameter_fragment('separator=(\n None\n)'), 'separator=(\n None\n)')

    def test_two_new_parameters_are_generated_by_model_in_one_hole(self):
        b,g=self.generator(['separator=None, reverse=False',
                            'cleaned.split(separator or ",")[::-1] if reverse else cleaned.split(separator or ",")'])
        self.ticket['description']='Add new parameters for split.\n```python\nparse("a;b", separator=";", reverse=True)\n```'
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'generated',r)
        self.assertEqual(b.calls,2)

    def test_complete_evidence_survives_long_intro_and_title_deduplicates(self):
        b,g=self.generator([])
        self.ticket.update(title='Split support',description=('Split support\n\n'+'x'*2000+
          '\n\n```python\n# Option A: add a new parameter for split.\nparse("a;b", separator=";")\n```'))
        selected=g.generate(self.ticket,self.loc,self.repo,plan_only=True)
        p=selected['scope_plan'];brief,audit=scope_brief(self.ticket,p)
        self.assertIn('Option A: add a new parameter for split.',brief)
        self.assertIn('parse("a;b", separator=";")',brief)
        self.assertEqual(brief.count('Split support'),1)
        self.assertNotIn('x'*100,brief)
        self.assertLessEqual(len(brief),1600)
        self.assertFalse(audit['mid_unit_truncation'])
        self.assertEqual(b.calls,0)

    def test_public_brief_ignores_gold_and_never_splits_sentence(self):
        ticket={'title':'One title','description':'A complete sentence. '+'q'*1700,
                'test_patch':'SECRET','patch':'SECRET','FAIL_TO_PASS':['SECRET']}
        brief,audit=scope_brief(ticket,{'evidence':[]},max_chars=100)
        self.assertIn('A complete sentence.',brief)
        self.assertNotIn('q',brief)
        self.assertNotIn('SECRET',brief)
        self.assertGreater(audit['omitted_units'],0)
        with self.assertRaisesRegex(ValueError,'evidence_exceeds_bound'):
            scope_brief({'description':'Add a new parameter '+'x'*100},
                        {'evidence':[{'explicit_add_parameter':True,'intent_excerpt':'Add a new parameter','call':'f(new=True)'}]},max_chars=50)

    def test_extracted_length_signature_enters_behavior_and_combines(self):
        raw_signature='separator=None):\n    return 999\n# unrelated continuation\ndef wrong('
        b,g=self.generator([raw_signature,'cleaned.split(separator or ",")'])
        original=b.infill
        def replay(*args,**kwargs):
            response=original(*args,**kwargs)
            if b.calls==1:response['stop_reason']='length'
            return response
        b.infill=replay
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'generated',r)
        self.assertEqual(b.calls,2)
        sig=r['scope_history'][0]
        self.assertEqual(sig['signature_extraction']['status'],'extracted')
        self.assertEqual(sig['generation']['middle'],raw_signature)
        self.assertEqual(sig['generation']['stop_reason'],'length')
        self.assertEqual(sig['normalized_middle'],'separator=None')
        self.assertNotIn('wrong',r['patch'])
        self.assertNotIn('999',r['patch'])
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)

    def test_extraction_still_requires_behavior_change_and_three_call_limit(self):
        b,g=self.generator(['separator=None):\n    pass','cleaned.split(",")','cleaned.split(",")'])
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(b.calls,3)
        self.assertFalse(r['generated_before_validation'])
        self.assertEqual(r['scope_history'][0]['status'],'validated_draft')
        self.assertEqual(r['patch'],'')

    def test_extracted_wrong_names_defaults_and_annotations_still_stop(self):
        for fragment in ('wrong=None','separator','separator=missing()',
                         'separator: int=None','separator=None, other=False'):
            b,g=self.generator([fragment+'):\n    pass\n# continued'])
            r=g.generate(self.ticket,self.loc,self.repo)
            self.assertEqual(b.calls,1)
            self.assertFalse(r['generated_before_validation'])
            self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)
            self.assertEqual(r['scope_history'][0]['signature_extraction']['status'],'extracted')

    def test_token_boundary_ignores_literals_and_nested_parentheses(self):
        for fragment in ('separator="):"','separator=(None)',
                         'separator={"key": (1, 2)}','separator=(\n None\n), other=False',
                         '名稱="中文):"'):
            output=fragment+'):\n    pass\n# later invalid ('
            normalized,audit=extract_signature(output,'):\n    pass\n')
            self.assertEqual(normalized,fragment)
            self.assertEqual(audit['boundary_offset'],len(fragment))
            self.assertFalse(audit['synthesized_parameters'])
        source='def f(value):\n    return value\n'
        pos,sep,_=signature_anchor(source,'f')
        fragment,_=extract_signature('separator={"key": (1, 2)}):\n pass','):\n pass')
        check_additive_signature(source,source[:pos]+sep+fragment+source[pos:],'f',['separator'],signature_only=True)

    def test_exact_header_closure_including_multiline_return_annotation(self):
        suffix='  # original comment\n) -> tuple[\n    int, str\n]:\n    pass\n'
        fragment,audit=extract_signature('separator=None) -> tuple[int, str]:\n    pass',suffix)
        self.assertEqual(fragment,'separator=None')
        self.assertEqual(audit['closure_tokens'],[')','->','tuple','[','int',',','str',']',':'])
        with self.assertRaisesRegex(ValueError,'closure differs'):
            extract_signature('separator=None) -> list:\n    pass',suffix)

    def test_no_arbitrary_prefix_or_unwitnessed_boundary_is_accepted(self):
        for output in ('separator=None','separator=(None','separator=[None):\n pass',
                       'separator="unterminated):\n pass','separator="):"',
                       'separator=None\n# boundary ):','separator=None)',
                       'separator=None)):\n pass','separator=None\nreturn 1):\n pass',
                       'separator=None # explain\n):\n pass'):
            with self.assertRaises(ValueError,msg=output):extract_signature(output,'):\n pass')

    def test_body_length_stop_is_not_relaxed_by_signature_extraction(self):
        b,g=self.generator(['separator=None):\n pass','cleaned.split(separator or ",")','cleaned.split(separator or ",")'])
        b.stop_reason='length'
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(b.calls,3)
        self.assertFalse(r['generated_before_validation'])
        self.assertIn('incomplete_behavior',r['candidates'][0]['reason'])

    def test_unknown_backend_stop_cannot_use_signature_extraction(self):
        b,g=self.generator(['separator=None):\n pass']);b.stop_reason='connection_error'
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(b.calls,1)
        self.assertFalse(r['generated_before_validation'])
        self.assertNotIn('signature_extraction',r['scope_history'][0])

    def test_length_noop_receives_behavior_feedback_before_stop_gate(self):
        b,g=self.generator(['separator=None','cleaned.split(",")\n    continued(',
                            'cleaned.split(separator or ",")'])
        self.ticket['expected_behavior']='Split using the requested separator.'
        original=b.infill
        def replay(*args,**kwargs):
            response=original(*args,**kwargs)
            if b.calls==2:response['stop_reason']='length'
            return response
        b.infill=replay
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(b.calls,3)
        self.assertEqual(r['patch_status'],'generated',r)
        rejected=r['scope_history'][1]
        self.assertTrue(rejected['behavior_diagnostic']['no_op'])
        self.assertEqual(rejected['reason'],'incomplete_behavior: requires model end marker')
        self.assertIn('no_op', b.prompts[2][0])
        self.assertIn('parameter_not_referenced: separator', b.prompts[2][0])
        self.assertIn('Public expectation: "Split using the requested separator."', b.prompts[2][0])
        self.assertEqual(r['scope_history'][2]['feedback'],rejected['retry_feedback'])
        self.assertEqual(r['scope_history'][1]['generation']['middle'],'cleaned.split(",")\n    continued(')
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)

    def test_parameter_reference_diagnostic_does_not_add_semantic_gate(self):
        b,g=self.generator(['separator=None','cleaned.split(delimiter or ";")'])
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'generated',r)
        diagnostic=r['scope_history'][1]['behavior_diagnostic']
        self.assertEqual(diagnostic['parameters_not_referenced'],['separator'])
        self.assertFalse(diagnostic['semantic_change_verified'])
        self.assertEqual(b.calls,2)

    def test_partial_expression_diagnostic_preserves_stop_gate(self):
        b,g=self.generator(['separator=None','cleaned.split(separator','cleaned.split(separator'])
        original=b.infill
        def replay(*args,**kwargs):
            response=original(*args,**kwargs)
            if b.calls>1:response['stop_reason']='length'
            return response
        b.infill=replay
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(b.calls,3)
        self.assertFalse(r['generated_before_validation'])
        for record in r['scope_history'][1:]:
            self.assertIsNone(record['behavior_diagnostic']['no_op'])
            self.assertIn('incomplete_behavior',record['reason'])

    def test_singleton_list_hole_combines_atomically_and_expected_in_first_prompt(self):
        raw=b'def parse(value):\n    parsed = [split(value)]\n    return parsed\n\ndef split(value, separator=","):\n    return value.split(separator)\n'
        self.replace_source(raw,'parse',1,3)
        b,g=self.generator(['separator=None','split(value, separator or ",")'])
        self.ticket['expected_behavior']='Split using the requested separator.'
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'generated',r)
        self.assertEqual(b.calls,2)
        self.assertTrue(b.prompts[1][0].endswith('    parsed = ['))
        self.assertTrue(b.prompts[1][1].startswith(']\n'))
        self.assertIn('Public expectation: "Split using the requested separator."',b.prompts[1][0])
        self.assertLessEqual(r['behavior_public_context']['chars'],600)
        self.assertEqual(r['behavior_anchor']['action'],'single_container_element')
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)
        git(self.repo,'apply',data=r['patch'].encode())
        ns={};exec((self.repo/'app.py').read_bytes(),ns)
        self.assertEqual(ns['parse']('a,b'),[['a','b']])
        self.assertEqual(ns['parse']('a;b',separator=';'),[['a','b']])

    def test_singleton_element_noop_retry_and_arity_escape_rejected(self):
        raw=b'def parse(value):\n    parsed = [value.split(",")]\n    return parsed\n'
        self.replace_source(raw,'parse',1,3)
        b,g=self.generator(['separator=None','value.split(",")','value.split(separator or ",")'])
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'generated',r)
        self.assertTrue(r['scope_history'][1]['behavior_diagnostic']['no_op'])
        self.assertIn('no_op',b.prompts[2][0])
        for output in ('value.split(separator), value', '[value.split(separator)]\nreturn value'):
            b,g=self.generator(['separator=None',output,output])
            r=g.generate(self.ticket,self.loc,self.repo)
            self.assertEqual(b.calls,3)
            self.assertFalse(r['generated_before_validation'])
            self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)

    def test_singleton_length_stop_still_requires_terminator(self):
        raw=b'def parse(value):\n    parsed = [value.split(",")]\n    return parsed\n'
        self.replace_source(raw,'parse',1,3)
        b,g=self.generator(['separator=None','value.split(separator or ",")','value.split(separator or ",")'])
        original=b.infill
        def replay(*args,**kwargs):
            response=original(*args,**kwargs)
            if b.calls>1:response['stop_reason']='length'
            return response
        b.infill=replay
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(b.calls,3)
        self.assertFalse(r['generated_before_validation'])
        self.assertIn('incomplete_behavior',r['candidates'][0]['reason'])

    def test_long_mandatory_behavior_evidence_retained_with_bounded_fallback(self):
        b,g=self.generator(['separator=None','cleaned.split(separator or ",")'])
        self.ticket['description']='x'*650+' Add a new parameter for split.\n```python\nparse("a;b", separator=";")\n```'
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(b.calls,2)
        self.assertEqual(r['patch_status'],'generated',r)
        self.assertEqual(r['behavior_public_context']['max_chars'],1600)
        self.assertEqual(r['behavior_public_context']['fallback'],
                         'mandatory_public_evidence_exceeds_preferred_600_chars')
        self.assertIn('x'*650,b.prompts[1][0])

    def test_static_behavior_gap_retains_candidate_and_retries_once(self):
        b,g=self.generator(['separator=None','cleaned.split(delimiter or ";")','cleaned.split(separator or ",")'])
        self.ticket['expected_behavior']='Split using the requested separator.'
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(b.calls,3)
        self.assertEqual(r['patch_status'],'generated',r)
        self.assertEqual(r['static_candidate_count'],2)
        self.assertEqual(r['selected_candidate_id'],2)
        self.assertTrue(r['behavior_retry_performed'])
        self.assertEqual(r['candidates'][0]['status'],'generated')
        self.assertIn('delimiter or ";"',r['candidates'][0]['patch'])
        self.assertIn('public_option_behavior_gap',b.prompts[2][0])
        self.assertIn('separator',b.prompts[2][0])
        self.assertIn('Static candidate retained',b.prompts[2][0])
        self.assertIn('Public expectation:',b.prompts[2][0])
        self.assertEqual(r['scope_history'][1]['status'],'static_valid_behavior_retry')
        self.assertFalse(r['behavior_assessment']['semantic_repair_verified'])
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)

    def test_failed_retry_keeps_first_static_candidate(self):
        for output in ('cleaned.split(', 'cleaned.split(",")', 'missing(cleaned,separator)'):
            b,g=self.generator(['separator=None','cleaned.split(delimiter or ";")',output])
            self.ticket['expected_behavior']='Split using the requested separator.'
            r=g.generate(self.ticket,self.loc,self.repo)
            self.assertEqual(b.calls,3)
            self.assertEqual(r['patch_status'],'generated',r)
            self.assertEqual(r['static_candidate_count'],1)
            self.assertEqual(r['selected_candidate_id'],1)
            self.assertEqual(r['candidates'][0]['status'],'generated')
            self.assertEqual(r['candidates'][1]['status'],'rejected')
            self.assertIn('delimiter or ";"',r['patch'])
            self.assertTrue(r['behavior_assessment']['retry_recommended'])

    def test_duplicate_gap_retry_deduplicates_and_stops(self):
        b,g=self.generator(['separator=None','cleaned.split(delimiter or ";")','cleaned.split(delimiter or ";")'])
        self.ticket['expected_behavior']='Split using the requested separator.'
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(b.calls,3)
        self.assertEqual(r['static_candidate_count'],1)
        self.assertEqual(len(r['candidates']),2)
        self.assertEqual(r['selected_candidate_id'],1)
        self.assertEqual(len(r['generated_candidates'][0]['attempts']),2)

    def test_backend_retry_error_keeps_static_candidate(self):
        from fim_patch.backend import GenerationError
        b,g=self.generator(['separator=None','cleaned.split(delimiter or ";")'])
        self.ticket['expected_behavior']='Split using the requested separator.'
        original=b.infill
        def replay(*args,**kwargs):
            if b.calls==2:
                b.calls+=1
                raise GenerationError('connection_error',{'saved':'error'})
            return original(*args,**kwargs)
        b.infill=replay
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(b.calls,3)
        self.assertEqual(r['patch_status'],'generated',r)
        self.assertEqual(r['static_candidate_count'],1)
        self.assertEqual(r['scope_history'][2]['backend_response'],{'saved':'error'})

    def test_validator_runs_once_on_selected_candidate_and_counts_both(self):
        b,g=self.generator(['separator=None','cleaned.split(delimiter or ";")','cleaned.split(separator or ",")'])
        self.ticket['expected_behavior']='Split using the requested separator.'
        validated=[]
        def validator(patch,repo):
            validated.append(patch)
            return {'status':'failed','feedback':'failed assertion'}
        g.validator=validator
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(b.calls,3)
        self.assertEqual(len(validated),1)
        self.assertIn('separator or',validated[0])
        self.assertEqual(r['static_candidate_count'],2)
        self.assertTrue(r['generated_before_validation'])
        self.assertEqual(r['patch_status'],'no_valid_candidate')
        self.assertEqual(r['patch'],'')

    def test_snapshot_change_during_soft_retry_prevents_stale_selection(self):
        b,g=self.generator(['separator=None','cleaned.split(delimiter or ";")','cleaned.split(separator or ",")'])
        self.ticket['expected_behavior']='Split using the requested separator.'
        original=b.infill
        def replay(*args,**kwargs):
            response=original(*args,**kwargs)
            if b.calls==3:(self.repo/'app.py').write_bytes(self.raw+b'# external change\n')
            return response
        b.infill=replay
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(b.calls,3)
        self.assertEqual(r['patch_status'],'no_valid_candidate')
        self.assertEqual(r['patch'],'')
        self.assertEqual(r['static_candidate_count'],1)
        self.assertIn('source_snapshot_recheck_failed',r['explanation'])


if __name__=='__main__':unittest.main()
