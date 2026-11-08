import ast
import copy
import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from fim_patch.generator import FIMPatchGenerator,git,function_span,build_patch
from fim_patch.local_fim import select_span,prompt_context
from fim_patch.iterative import IterativeRepairGenerator
from tests import test_fim

class LocalFIMTests(unittest.TestCase):
    def setUp(self):
        test_fim.PatchTests.setUp(self)
        self.raw=(b'import re\n\ndef parse(value, delimiter=None):\n'
            b'    """A long irrelevant docstring.\n    second documentation line.\n    """\n'
            b'    cleaned = value.strip()\n    parsed = cleaned.split(",")\n    return parsed\n\n'
            b'def unrelated():\n    return 123\n')
        self.replace_source(self.raw,'parse',3,9)
        self.ticket['description']='Use delimiter when calling split; preserve whitespace cleanup.'
    def replace_source(self,raw,symbol,start,end):
        self.raw=raw;(self.repo/'app.py').write_bytes(raw)
        git(self.repo,'add','app.py');git(self.repo,'-c','user.name=T','-c','user.email=t@invalid','commit','-m','fixture')
        base=git(self.repo,'rev-parse','HEAD').decode().strip()
        self.ticket['base_commit']=base;self.loc['base_commit']=base
        self.row={'file_path':'app.py','symbol_qualified_name':symbol,'start_line':start,'end_line':end}
        self.loc['stage3_ranked_symbols']=[self.row]
    def generator(self,outputs,**kwargs):
        class Backend(test_fim.FakeBackend):
            def infill(this,prefix,suffix,**kw):
                this.prompts.append((prefix,suffix));return super().infill(prefix,suffix,**kw)
        b=Backend(outputs);b.prompts=[]
        return b,FIMPatchGenerator(b,candidates=1,prompt_layout='compact_fim',local_fim=True,
                                  include_original_body=True,helper_contracts=True,**kwargs)
    def test_changes_one_statement_and_preserves_other_source_bytes(self):
        b,g=self.generator(['    parsed = cleaned.split(delimiter or ",")\n'])
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'generated',r.get('candidates'))
        self.assertEqual(r['span']['generation_scope'],'issue_statement')
        self.assertEqual(r['span']['body_lines'],1)
        self.assertEqual(r['local_fim_selection']['action'],'local_statement')
        self.assertNotIn('-    cleaned =',r['patch']);self.assertNotIn('-    return parsed',r['patch'])
        self.assertNotIn('second documentation line',b.prompts[0][0])
        self.assertIn('def parse(value, delimiter=None):',b.prompts[0][0])
        self.assertIn('cleaned = value.strip()',b.prompts[0][0])
        self.assertIn('return parsed',b.prompts[0][1])
        self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)
        git(self.repo,'apply',data=r['patch'].encode());ns={};exec((self.repo/'app.py').read_text(),ns)
        self.assertEqual(ns['parse'](' a;b ',';'),['a','b']);self.assertEqual(ns['unrelated'](),123)
        self.assertTrue(r['generated_before_validation'])
    def test_noop_is_not_counted_as_generated(self):
        _,g=self.generator(['    parsed = cleaned.split(",")\n'])
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertFalse(r['generated_before_validation']);self.assertEqual(r['candidates'][0]['reason'],'no_op')
    def test_extra_statement_and_sibling_are_rejected(self):
        for output in ('    parsed = cleaned.split(delimiter)\n    value = "changed"\n',
                       'def parse(value):\n    return value\n'):
            _,g=self.generator([output]);r=g.generate(self.ticket,self.loc,self.repo)
            self.assertFalse(r['generated_before_validation'])
            self.assertEqual((self.repo/'app.py').read_bytes(),self.raw)
    def test_length_stop_does_not_accept_partial_statement(self):
        b,g=self.generator(['    parsed = cleaned.split(delimiter)\n']);b.stop_reason='length'
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertFalse(r['generated_before_validation'])
        self.assertIn('requires model end marker',r['candidates'][0]['reason'])
    def test_unknown_new_names_still_rejected(self):
        _,g=self.generator(['    parsed = missing_parser(cleaned)\n']);r=g.generate(self.ticket,self.loc,self.repo)
        self.assertFalse(r['generated_before_validation'])
        self.assertIn('undefined',r['candidates'][0]['reason'])
    def test_ambiguous_public_terms_fall_back_with_audit(self):
        raw=b'def f(value):\n    a = value.strip()\n    b = value.strip()\n    return a + b\n'
        span=function_span(raw,{'symbol_qualified_name':'f','start_line':1,'end_line':4})
        local,audit=select_span(raw,span,{'description':'strip should handle Unicode'},'app.py')
        self.assertEqual(audit['reason'],'ambiguous_statement_evidence')
        self.assertEqual(local['original_middle'],span['original_middle'])
    def test_public_trace_resolves_ambiguous_terms(self):
        raw=b'def f(value):\n    a = value.strip()\n    b = value.strip()\n    return a + b\n'
        span=function_span(raw,{'symbol_qualified_name':'f','start_line':1,'end_line':4})
        local,audit=select_span(raw,span,{'error_message':'File "app.py", line 3'},'app.py')
        self.assertEqual(audit['reason'],'matching_public_trace');self.assertEqual(local['line_start'],3)
    def test_no_gold_fields_used_to_choose_statement(self):
        span=function_span(self.raw,self.row)
        a,aa=select_span(self.raw,span,self.ticket,'app.py')
        t=dict(self.ticket,patch='replace cleaned',test_patch='replace value',fail_to_pass=['wrong.py:7'])
        b,bb=select_span(self.raw,span,t,'app.py')
        self.assertEqual(a['start'],b['start']);self.assertEqual(aa,bb)
    def test_semicolon_shared_line_not_narrowed(self):
        raw=b'def f(value):\n    a = value.strip(); b = value.lower()\n    return a + b\n'
        span=function_span(raw,{'symbol_qualified_name':'f','start_line':1,'end_line':3})
        local,audit=select_span(raw,span,{'description':'strip must handle Unicode'},'app.py')
        self.assertEqual(audit['action'],'fallback_full_body')
        self.assertIn('shared_physical_line',str(audit['rejected_blocks']))
    def test_complete_compound_block_is_selected(self):
        raw=b'def f(value):\n    if value is None:\n        return None\n    return value.strip()\n'
        span=function_span(raw,{'symbol_qualified_name':'f','start_line':1,'end_line':4})
        local,audit=select_span(raw,span,{'description':'None must become an empty string'},'app.py')
        self.assertEqual(local['statement_kind'],'If');self.assertEqual(local['body_lines'],2)
    def test_crlf_bom_unicode_and_comments_preserved(self):
        raw=b'\xef\xbb\xbf'+self.raw.replace(b'    parsed =', '    # 中文註解\n    parsed ='.encode()).replace(b'\n',b'\r\n')
        self.replace_source(raw,'parse',3,10)
        _,g=self.generator(['    parsed = cleaned.split(delimiter or ",")\n'])
        r=g.generate(self.ticket,self.loc,self.repo);self.assertEqual(r['patch_status'],'generated',r['candidates'])
        git(self.repo,'apply',data=r['patch'].encode());after=(self.repo/'app.py').read_bytes()
        expected=raw.replace(b'parsed = cleaned.split(",")',b'parsed = cleaned.split(delimiter or ",")')
        self.assertEqual(after,expected)
    def test_complete_multiline_signature_is_kept(self):
        raw=b'def parse(\n    value,\n    delimiter=None,\n):\n    parsed = value.split(",")\n    return parsed\n'
        self.replace_source(raw,'parse',1,6)
        b,g=self.generator(['    parsed = value.split(delimiter or ",")\n'])
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'generated',r['candidates'])
        self.assertIn('def parse(\n    value,\n    delimiter=None,\n):',b.prompts[0][0])
    def test_iterative_noop_feedback_stays_local_and_stops_on_success(self):
        b,g=self.generator(['    parsed = cleaned.split(",")\n','    parsed = cleaned.split(delimiter or ",")\n'])
        r=IterativeRepairGenerator(g,3).generate(self.ticket,self.loc,self.repo)
        self.assertEqual(b.calls,2);self.assertTrue(r['generated_before_validation'])
        self.assertIn('no_op',b.prompts[1][0]);self.assertEqual(r['span']['body_lines'],1)
    def test_incompatible_modes_rejected_without_backend_calls(self):
        for kw in ({'fixed_body_extraction':True},{'helper_implementations':True},
                   {'target_policy':'related_targets_top5'},{'large_function_strategy':'issue_statement'}):
            with self.assertRaises(ValueError):self.generator([],**kw)
    def test_constructor_default_is_unchanged(self):
        b=test_fim.FakeBackend([]);g=FIMPatchGenerator(b)
        self.assertFalse(g.local_fim);self.assertFalse(g.fixed_signature_body)

    def test_large_function_narrows_before_body_limit(self):
        raw=(b'def parse(value, delimiter=None):\n'+b'    value = value.strip()\n'*125+
             b'    parsed = value.split(",")\n    return parsed\n')
        self.replace_source(raw,'parse',1,128)
        _,g=self.generator(['    parsed = value.split(delimiter or ",")\n'],max_body_lines=120)
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'generated',r)
        self.assertEqual(r['span']['body_lines'],1)
        self.assertGreater(r['local_fim_selection']['function_body_lines'],120)

    def test_missing_evidence_falls_back_but_keeps_original_signature(self):
        self.ticket['description']='Unexpected behavior.';self.ticket['title']='Bug'
        b,g=self.generator(['    cleaned = value.strip()\n    parsed = cleaned.split(delimiter or ",")\n    return parsed\n'])
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['local_fim_selection']['action'],'fallback_full_body')
        self.assertEqual(r['patch_status'],'generated',r)
        self.assertIn('def parse(value, delimiter=None):',b.prompts[0][0])

    def test_async_class_method_preserves_signature_and_import_dependency(self):
        raw=(b'import re\n\nclass Parser:\n    async def parse(self, value, delimiter=None):\n'
             b'        cleaned = value.strip()\n        parsed = re.split(",", cleaned)\n        return parsed\n')
        self.replace_source(raw,'Parser.parse',4,7)
        self.ticket['description']='Use delimiter in re.split.'
        b,g=self.generator(['        parsed = re.split(delimiter or ",", cleaned)\n'])
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'generated',r)
        self.assertIn('import re',b.prompts[0][0])
        self.assertIn('async def parse(self, value, delimiter=None):',b.prompts[0][0])
        git(self.repo,'apply',data=r['patch'].encode())
        ast.parse((self.repo/'app.py').read_text())

    def test_context_does_not_split_multiline_string_or_exceed_budget(self):
        raw=(b'def parse(value, delimiter=None):\n    note = """'+b'x'*3400+
             b'\nmore text"""\n    parsed = value.split(",")\n    return parsed\n')
        self.replace_source(raw,'parse',1,5)
        b,g=self.generator(['    parsed = value.split(delimiter or ",")\n'])
        r=g.generate(self.ticket,self.loc,self.repo)
        self.assertEqual(r['patch_status'],'generated',r)
        self.assertNotIn('more text',b.prompts[0][0])
        self.assertIn(2,r['prompt_context']['omitted_block_lines'])
        self.assertLessEqual(r['prompt_context']['source_context_chars'],3200)

    def test_one_very_long_statement_is_not_classed_as_small(self):
        raw=b'def f(value):\n    blob = "'+b'x'*2100+b'"\n    return value\n'
        span=function_span(raw,{'symbol_qualified_name':'f','start_line':1,'end_line':3})
        _,audit=select_span(raw,span,{'description':'blob is incorrect'},'app.py')
        self.assertEqual(audit['action'],'fallback_full_body')
        self.assertIn('block_exceeds_2000_chars',str(audit['rejected_blocks']))

if __name__=='__main__':unittest.main()
