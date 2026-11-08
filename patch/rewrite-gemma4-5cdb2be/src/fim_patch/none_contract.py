"""Bounded synthetic None probes interpreted without executing repository code."""
import ast
import copy

VERSION = 'source-none-probe-v2-symbolic'
COPY, CALL, GETATTR, HASATTR, CALLABLE = (object() for _ in range(5))


class Unknown(Exception):
    pass


class Fault(Exception):
    def __init__(self, kind):self.kind=kind


class Returned(Exception):
    def __init__(self, value):self.value=value


class Obj:
    def __init__(self, fields):self.fields=fields


class NonNoneValue:
    """Unknown non-null value; do not invent its type, truthiness or equality."""
    def __bool__(self):raise Unknown('unknown_truthiness')
    def __eq__(self, other):raise Unknown('unknown_equality')
    def __ne__(self, other):raise Unknown('unknown_equality')


class VM:
    def __init__(self, env):self.env=env;self.steps=0

    def tick(self):
        self.steps+=1
        if self.steps>256:raise Unknown('step_limit')

    def value(self, node):
        self.tick()
        if isinstance(node,ast.Constant):
            if isinstance(node.value,str) and len(node.value)>256:raise Unknown('large_literal')
            return node.value
        if isinstance(node,ast.Name):
            if node.id not in self.env:raise Unknown('unknown_name')
            return self.env[node.id]
        if isinstance(node,ast.Attribute):
            value=self.value(node.value)
            if value is None:raise Fault('none_attribute_access')
            if not isinstance(value,Obj) or node.attr not in value.fields:raise Unknown('unknown_attribute')
            return value.fields[node.attr]
        if isinstance(node,ast.BoolOp):
            value=self.value(node.values[0])
            for child in node.values[1:]:
                if (isinstance(node.op,ast.And) and not value) or (isinstance(node.op,ast.Or) and value):break
                value=self.value(child)
            return value
        if isinstance(node,ast.Compare):
            left=self.value(node.left)
            for operator,right_node in zip(node.ops,node.comparators):
                right=self.value(right_node)
                if isinstance(operator,ast.Is):answer=left is right
                elif isinstance(operator,ast.IsNot):answer=left is not right
                elif isinstance(operator,ast.Eq):answer=left==right
                elif isinstance(operator,ast.NotEq):answer=left!=right
                else:raise Unknown('comparison')
                if not answer:return False
                left=right
            return True
        if isinstance(node,ast.UnaryOp) and isinstance(node.op,ast.Not):return not self.value(node.operand)
        if isinstance(node,ast.IfExp):return self.value(node.body if self.value(node.test) else node.orelse)
        if isinstance(node,ast.Dict) and not node.keys:return {}
        if isinstance(node,ast.Call):
            function=self.value(node.func)
            # Arguments are evaluated even for an uncallable None, as in Python.
            args=[self.value(value) for value in node.args]
            for keyword in node.keywords:
                if keyword.arg is None and self.value(keyword.value)!={}:raise Unknown('nonempty_kwargs')
                elif keyword.arg is not None:self.value(keyword.value)
            if function is None:raise Fault('none_not_callable')
            if function is COPY and len(args)==1:return copy.deepcopy(args[0])
            if function is CALL:raise Unknown('unknown_callback_semantics')
            if function is CALLABLE and len(args)==1:return any(args[0] is value for value in (COPY,CALL,GETATTR,HASATTR,CALLABLE))
            if (function is GETATTR or function is HASATTR) and len(args) in (2,3) and isinstance(args[1],str):
                present=isinstance(args[0],Obj) and args[1] in args[0].fields
                if function is HASATTR:return present
                if present:return args[0].fields[args[1]]
                if len(args)==3:return args[2]
                if args[0] is None:raise Fault('none_attribute_access')
            raise Unknown('unsupported_call')
        raise Unknown('unsupported_expression')

    def statements(self, statements):
        for node in statements:
            self.tick()
            if isinstance(node,ast.Return):raise Returned(self.value(node.value) if node.value else None)
            if isinstance(node,ast.If):self.statements(node.body if self.value(node.test) else node.orelse)
            elif isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name):
                self.env[node.targets[0].id]=self.value(node.value)
            elif isinstance(node,ast.Pass):pass
            elif isinstance(node,ast.Expr) and isinstance(node.value,ast.Constant) and isinstance(node.value.value,str):pass
            elif not isinstance(node,ast.If):raise Unknown('unsupported_statement')


def run(node, env):
    try:VM(copy.deepcopy(env,{id(value):value for value in (COPY,CALL,GETATTR,HASATTR,CALLABLE)})).statements(node.body)
    except Returned:return dict(status='returned')
    except Fault as exc:return dict(status='error',reason=exc.kind)
    except Unknown as exc:return dict(status='unknown',reason=str(exc))
    return dict(status='returned')


def assess_none_contract(before, after, symbol):
    from .generator import _target_function_node
    old=_target_function_node(before,symbol);new=_target_function_node(after,symbol)
    globals_bound=set();copy_names=set()
    for statement in ast.parse(before).body:
        if isinstance(statement,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
            globals_bound.add(statement.name)
            copy_names.discard(statement.name)
        elif isinstance(statement,(ast.Import,ast.ImportFrom)):
            for alias in statement.names:
                name=alias.asname or alias.name.split('.')[0]
                globals_bound.add(name)
                copy_names.discard(name)
                if isinstance(statement,ast.ImportFrom) and statement.module=='copy' and not statement.level and alias.name=='deepcopy':
                    copy_names.add(name)
        elif isinstance(statement,(ast.Assign,ast.AnnAssign,ast.AugAssign)):
            for name in (n.id for n in ast.walk(statement) if isinstance(n,ast.Name) and isinstance(n.ctx,ast.Store)):
                globals_bound.add(name);copy_names.discard(name)
    params=[a.arg for a in old.args.posonlyargs+old.args.args+old.args.kwonlyargs]
    guarded=sorted({node.left.id for node in ast.walk(old) if isinstance(node,ast.Compare)
        and isinstance(node.left,ast.Name) and node.left.id in params and node.left.id not in ('self','cls')
        and len(node.ops)==1 and isinstance(node.ops[0],ast.Is)
        and isinstance(node.comparators[0],ast.Constant) and node.comparators[0].value is None})[:8]
    fields={name:set() for name in params}
    calls=set()
    for tree in (old,new):
        for node in ast.walk(tree):
            if isinstance(node,ast.Attribute) and isinstance(node.value,ast.Name) and node.value.id in fields:
                fields[node.value.id].add(node.attr)
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id in params:calls.add(node.func.id)
    cases=[]
    for guard in guarded:
        env={name:Obj({field:NonNoneValue() for field in fields[name]}) if fields[name] else NonNoneValue() for name in params}
        for name in calls:env[name]=CALL
        if old.args.kwarg:env[old.args.kwarg.arg]={}
        env[guard]=None
        # No execution/import of source helpers; all dependencies are synthetic.
        for name,value in dict(getattr=GETATTR,hasattr=HASATTR,callable=CALLABLE).items():
            if name not in env and name not in globals_bound:env[name]=value
        for name in copy_names:
            if name not in env:env[name]=COPY
        # Preserve sentinel identities while copying per-probe environments.
        original=run(old,env);modified=run(new,env)
        cases.append(dict(parameter=guard,value=None,original=original,modified=modified,
                          regression=original['status']=='returned' and modified['status']=='error'))
    failed=[row for row in cases if row['regression']]
    return dict(version=VERSION,status='failed' if failed else 'passed_bounded' if cases and all(
        row['original']['status']=='returned' and row['modified']['status']=='returned' for row in cases) else 'unknown_not_checked',
        cases=cases,failed_parameters=[row['parameter'] for row in failed],
        synthetic_inputs=True,official_tests_run=False,semantic_repair_verified=False)


def failure_feedback(check):
    messages=['Source-derived synthetic None probes failed (not official tests).']
    for case in check['cases']:
        if case['regression']:
            reason={'none_attribute_access':'AttributeError (attribute access on None)',
                    'none_not_callable':'TypeError (None is not callable)'}.get(case['modified']['reason'],case['modified']['reason'])
            messages.append(case['parameter']+'=None: original returned; candidate raises '+reason+'.')
    messages.append('Preserve the original supported None cases while fixing the public issue; output only the complete target body.')
    return ' '.join(messages)[:600]
