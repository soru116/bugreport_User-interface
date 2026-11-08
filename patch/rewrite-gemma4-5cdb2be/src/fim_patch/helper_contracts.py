"""Conservative same-module helper evidence and call binding, without execution."""
import ast
import inspect


def direct_calls(node, include_comprehensions=False):
    calls = []
    class Visitor(ast.NodeVisitor):
        def visit_Call(self, call):
            calls.append(call)
            self.generic_visit(call)
        def visit_FunctionDef(self, node):
            pass
        visit_AsyncFunctionDef = visit_FunctionDef
        def visit_ClassDef(self, node):
            pass
        def visit_Lambda(self, node):
            pass
        def visit_ListComp(self, node):
            if include_comprehensions:
                self.generic_visit(node)
        visit_SetComp = visit_ListComp
        visit_DictComp = visit_ListComp
        visit_GeneratorExp = visit_ListComp
    visitor = Visitor()
    for statement in node.body:
        visitor.visit(statement)
    return calls


def stable_helpers(text):
    tree = ast.parse(text)
    definitions = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definitions.setdefault(node.name, []).append(node)
    rebound = set()
    class Bindings(ast.NodeVisitor):
        def visit_Name(self, node):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                rebound.add(node.id)
        def visit_Import(self, node):
            rebound.update(a.asname or a.name.split('.')[0] for a in node.names)
        def visit_ImportFrom(self, node):
            rebound.update(a.asname or a.name for a in node.names)
            if any(a.name == '*' for a in node.names):
                rebound.update(definitions)
        def visit_ClassDef(self, node):
            rebound.add(node.name)
        def visit_FunctionDef(self, node):
            if node not in tree.body:
                rebound.add(node.name)
        visit_AsyncFunctionDef = visit_FunctionDef
    Bindings().visit(tree)
    return {name:nodes[0] for name,nodes in definitions.items()
            if len(nodes) == 1 and name not in rebound and not nodes[0].decorator_list}


def signature(node):
    args = node.args
    positional = args.posonlyargs + args.args
    default_start = len(positional) - len(args.defaults)
    parameters = []
    # Defaults are opaque sentinels, never evaluated.
    for index, arg in enumerate(positional):
        kind = inspect.Parameter.POSITIONAL_ONLY if index < len(args.posonlyargs) else inspect.Parameter.POSITIONAL_OR_KEYWORD
        parameters.append(inspect.Parameter(arg.arg, kind,
            default=None if index >= default_start else inspect.Parameter.empty))
    if args.vararg:
        parameters.append(inspect.Parameter(args.vararg.arg, inspect.Parameter.VAR_POSITIONAL))
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        parameters.append(inspect.Parameter(arg.arg, inspect.Parameter.KEYWORD_ONLY,
            default=None if default is not None else inspect.Parameter.empty))
    if args.kwarg:
        parameters.append(inspect.Parameter(args.kwarg.arg, inspect.Parameter.VAR_KEYWORD))
    return inspect.Signature(parameters)


def eligible_calls(text, qualified_name, include_comprehensions=False):
    from .generator import _target_function_node, _function_symbol_table
    node = _target_function_node(text, qualified_name)
    table = _function_symbol_table(text, qualified_name)
    helpers = stable_helpers(text)
    comprehension_bound = {n.id for c in ast.walk(node) if isinstance(c, ast.comprehension)
                           for n in ast.walk(c.target) if isinstance(n, ast.Name)}
    for call in direct_calls(node, include_comprehensions):
        if not isinstance(call.func, ast.Name) or call.func.id not in helpers:
            continue
        name = call.func.id
        if include_comprehensions and name in comprehension_bound:
            continue
        try:
            symbol = table.lookup(name)
        except KeyError:
            # Comprehensions have their own symbol tables. Graph/context may
            # follow only references resolved globally in those scopes.
            symbols = []
            def collect(table):
                for child in table.get_children():
                    if child.get_name() in ('listcomp', 'setcomp', 'dictcomp', 'genexpr'):
                        try:
                            symbols.append(child.lookup(name))
                        except KeyError:
                            pass
                        collect(child)
            if include_comprehensions:
                collect(table)
            if not symbols or not all(s.is_global() and not s.is_assigned() and not s.is_imported() for s in symbols):
                continue
            symbol = symbols[0]
        if not symbol.is_global() or symbol.is_assigned() or symbol.is_imported():
            continue
        # *args/**kwargs and nested scopes are deliberately not guessed.
        if any(isinstance(arg, ast.Starred) for arg in call.args) or any(k.arg is None for k in call.keywords):
            continue
        yield call, helpers[name]


def context(text, qualified_name, max_chars=2000):
    lines = []
    seen = set()
    for call, helper in eligible_calls(text, qualified_name):
        if helper.name in seen or helper.name == qualified_name.split('.')[-1]:
            continue
        seen.add(helper.name)
        line = '# Same-file helper: ' + helper.name + '(' + ast.unparse(helper.args) + ')\n'
        if len(''.join(lines)) + len(line) <= max_chars and len(lines) < 8:
            lines.append(line)
    return ''.join(lines)


def check_calls(before_text, after_text, qualified_name):
    before_calls = {ast.dump(call, include_attributes=False)
                    for call, _ in eligible_calls(before_text, qualified_name)}
    checked = 0
    for call, helper in eligible_calls(after_text, qualified_name):
        if ast.dump(call, include_attributes=False) in before_calls:
            continue
        if len({k.arg for k in call.keywords}) != len(call.keywords):
            raise ValueError('helper_call_incompatible: duplicate keyword for ' + helper.name)
        try:
            signature(helper).bind(*([None] * len(call.args)),
                **{keyword.arg:None for keyword in call.keywords})
        except TypeError as exc:
            raise ValueError('helper_call_incompatible: ' + helper.name + '(' + ast.unparse(helper.args) + '): ' + str(exc)) from exc
        checked += 1
    return {'version':'same-file-helper-contract-v1', 'changed_calls_checked':checked,
            'scope':'unshadowed undecorated same-module functions; no dynamic unpacking or nested scopes'}
