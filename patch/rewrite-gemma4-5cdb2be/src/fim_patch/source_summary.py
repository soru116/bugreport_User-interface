"""Bounded descriptive AST evidence for a removed body, never a repair oracle."""
import ast
import hashlib

VERSION = 'source-body-summary-v1'


def summarize(raw, symbol, max_chars=2200):
    from .generator import _target_function_node
    if type(max_chars) is not int or max_chars < 500:
        raise ValueError('source summary limit must be at least 500 characters')
    node = _target_function_node(raw.decode('utf-8-sig'), symbol)
    facts = []

    def label(n):
        value = ast.unparse(n).replace('\n', ' ')
        return value if len(value) <= 160 else value[:157]+'...'

    def shape(value):
        if value is None:return 'None (bare return)'
        if isinstance(value, ast.List):return 'list literal'
        if isinstance(value, ast.Tuple):return 'tuple literal'
        if isinstance(value, ast.Dict):return 'dict literal'
        if isinstance(value, ast.Set):return 'set literal'
        if isinstance(value, ast.Constant):return type(value.value).__name__+' literal'
        return 'unknown (no execution or type inference)'

    def record(n, kind, detail):
        expression=(n.test if kind=='branch' or isinstance(n,ast.While) else
                    n.iter if kind=='loop' else n.exc if kind=='raise' else
                    n.type if kind=='handler' else n.value if kind in ('return','binding','mutation') else n)
        # Only the expression for this fact, not a branch's nested suite.
        attributes=sorted({ast.unparse(a) for a in ast.walk(expression) if isinstance(a,ast.Attribute)}) if expression else []
        facts.append(dict(kind=kind, line=n.lineno, end_line=n.end_lineno, detail=detail,attributes=attributes))

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, n):pass
        def visit_AsyncFunctionDef(self, n):pass
        def visit_ClassDef(self, n):pass
        def visit_Lambda(self, n):pass
        def visit_Return(self, n):
            record(n,'return','return '+(label(n.value) if n.value else 'None')+'; shape='+shape(n.value))
        def visit_Assign(self, n):
            record(n,'binding',', '.join(label(t) for t in n.targets)+' <- '+type(n.value).__name__+' '+label(n.value))
            self.visit(n.value)
        def visit_AnnAssign(self, n):
            record(n,'binding',label(n.target)+' <- '+(type(n.value).__name__+' '+label(n.value) if n.value else 'annotation only'))
            if n.value:self.visit(n.value)
        def visit_AugAssign(self, n):
            record(n,'mutation',label(n.target)+' '+type(n.op).__name__+'= '+label(n.value))
            self.visit(n.value)
        def visit_If(self, n):
            record(n,'branch','if '+label(n.test))
            self.generic_visit(n)
        def visit_For(self, n):
            record(n,'loop','for '+label(n.target)+' in '+label(n.iter))
            self.generic_visit(n)
        visit_AsyncFor=visit_For
        def visit_While(self, n):
            record(n,'loop','while '+label(n.test));self.generic_visit(n)
        def visit_Raise(self, n):
            record(n,'raise',label(n.exc) if n.exc else 're-raise')
        def visit_ExceptHandler(self, n):
            record(n,'handler',label(n.type) if n.type else 'bare except');self.generic_visit(n)
        def visit_Call(self, n):
            record(n,'call',label(n.func)+'; keywords='+','.join(k.arg or '**' for k in n.keywords))
            self.generic_visit(n)

    visitor=Visitor()
    for statement in node.body:
        if isinstance(statement,ast.Expr) and isinstance(statement.value,ast.Constant) and isinstance(statement.value.value,str):continue
        visitor.visit(statement)
    # Prioritize observable outputs, then construction/control, then helper calls.
    priority={'return':0,'binding':1,'mutation':1,'branch':2,'loop':2,'raise':3,'handler':3,'call':4}
    ordered=sorted(facts,key=lambda f:(priority[f['kind']],f['line']))
    header=('# OBSERVED ORIGINAL BODY (AST facts, not a repair specification):\n'
            '# Public bug requirements may supersede these facts. Preserve unrelated behavior.\n'
            '# Name/call return types are unknown; docstring wording is not a complete contract.\n')
    footer='# Source facts omitted: {count}. Full audit is saved separately.\n'
    kept=[];rendered=header
    for fact in ordered:
        line='# L'+str(fact['line'])+' '+fact['kind']+': '+fact['detail']+'\n'
        if len(rendered)+len(line)+len(footer.format(count=len(ordered)))<=max_chars:
            kept.append(fact);rendered+=line
    rendered+=footer.format(count=len(ordered)-len(kept))
    return dict(version=VERSION,symbol=symbol,source_sha256=hashlib.sha256(raw).hexdigest(),
                origin='original_source_ast_only',semantic_contract_verified=False,model_calls=0,
                facts=facts,rendered=rendered,max_chars=max_chars,rendered_chars=len(rendered),
                omitted_facts=len(ordered)-len(kept),truncated=len(ordered)!=len(kept))
