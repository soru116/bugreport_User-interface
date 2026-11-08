"""Host delimiters and AST element gates; no real model inference."""
import ast
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fim_patch.inner_expression import narrow_container_span, check_inner_reconstruction


class InnerExpressionTests(unittest.TestCase):
    def span(self, expression, prefix='result = ', suffix='  # keep\n'):
        text = prefix+expression+suffix
        raw = text.encode('utf8')
        return raw, {'start':len(prefix), 'end':len(prefix)+len(expression),
                     'original_middle':expression, 'generation_scope':'python_expression',
                     'symbol':'f', 'expression_prefix_fragment':prefix,
                     'expression_suffix_fragment':suffix}

    def test_singleton_containers_keep_host_syntax(self):
        for expression, element in (('[helper(value)]','helper(value)'),
                                    ('(helper(value),)','helper(value)'),
                                    ('{helper(value)}','helper(value)'),
                                    ('[helper(value),]','helper(value)')):
            raw,span=self.span(expression)
            inner,audit=narrow_container_span(raw,span)
            self.assertEqual(audit['action'],'single_container_element')
            self.assertEqual(inner['original_middle'],element)
            self.assertEqual(inner['prefix']+element+inner['suffix'],raw.decode())
            replacement='alternate(value) if option else helper(value)'
            check_inner_reconstruction(inner,replacement)
            tree=ast.parse(inner['prefix']+replacement+inner['suffix'])
            self.assertEqual(len(tree.body[0].value.elts),1)

    def test_unicode_multiline_crlf_bom_comments_source_preserved(self):
        expression='[\r\n        helper("中文"),  # keep element comment\r\n    ]'
        raw,span=self.span(expression,prefix='名稱 = ',suffix='  # 保留\r\n')
        raw=b'\xef\xbb\xbf'+raw
        inner,audit=narrow_container_span(raw,span)
        self.assertEqual(inner['original_middle'],'helper("中文")')
        self.assertEqual(inner['prefix']+inner['original_middle']+inner['suffix'],raw.decode('utf-8-sig'))
        self.assertEqual(inner['expression_prefix_fragment'],'名稱 = [\r\n        ')
        self.assertIn('keep element comment',inner['expression_suffix_fragment'])
        check_inner_reconstruction(inner,'alternate("中文")')

    def test_unsupported_or_ambiguous_roots_retain_scope(self):
        for expression in ('helper(value)','[a,b]','[]','[*values]',
                           '[value for value in values]','{"key":value}','value,', '(value)'):
            raw,span=self.span(expression)
            inner,audit=narrow_container_span(raw,span)
            self.assertEqual(inner,span,expression)
            self.assertEqual(audit['action'],'retain_existing_scope')
        raw,span=self.span('[value]');span['generation_scope']='issue_statement'
        self.assertEqual(narrow_container_span(raw,span)[0],span)

    def test_element_count_scope_escape_and_incomplete_rejected(self):
        raw,span=self.span('[helper(value)]');inner,_=narrow_container_span(raw,span)
        for middle in ('alternate(value), helper(value)', 'alternate(value)]\nevil = [value',
                       'alternate(value)\nreturn value', 'alternate(', '[value] # explain'):
            with self.assertRaises(ValueError,msg=middle):check_inner_reconstruction(inner,middle)
        # A parenthesized tuple is one legitimate list element, unlike a bare
        # comma sequence that changes the host-owned list arity.
        check_inner_reconstruction(inner,'(alternate(value), helper(value))')

    def test_source_mismatch_and_trailing_comma_contract(self):
        raw,span=self.span('[helper(value)]');span['original_middle']='[wrong]'
        with self.assertRaisesRegex(ValueError,'source_mismatch'):narrow_container_span(raw,span)
        raw,span=self.span('(helper(value),)');inner,_=narrow_container_span(raw,span)
        with self.assertRaisesRegex(ValueError,'contract_violation'):
            check_inner_reconstruction(inner,'alternate(value),')


if __name__=='__main__':unittest.main()
