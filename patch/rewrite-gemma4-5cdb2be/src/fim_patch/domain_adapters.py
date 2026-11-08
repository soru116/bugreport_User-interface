"""Explicit, versioned domain adapters; disabled in the generic policy."""
import ast
import io
import re
import tokenize

VERSIONS = {'tolerance': 'tolerance-adapter-v1', 'conversion': 'conversion-adapter-v1'}


def validate_adapters(names):
    names = tuple(names or ())
    if len(names) != len(set(names)) or any(name not in VERSIONS for name in names):
        raise ValueError('Unknown or duplicate domain adapter')
    return names


def tolerance_matches(names, ticket, source):
    public = ' '.join(str(ticket.get(k) or '') for k in ('title','summary','body','description','bug_report')).casefold()
    return 'tolerance' in names and any(t in public for t in ('rtol','atol','tolerance')) and 'np.ndarray' in source


def directives(names, ticket, source):
    if tolerance_matches(names,ticket,source) and ('a != b' in source or 'np.where' in source):
        return [
            '# For numeric ndarray values, determine differing indices with a tolerance-aware comparison instead of exact a != b.',
            '# Use the existing where_not_allclose helper with rtol=rtol and atol=atol for floating arrays.',
            '# Preserve exact comparison for non-floating arrays and preserve recursive reporting behavior.',
            '# Add or propagate rtol/atol parameters when the selected function needs them.',
        ]
    return []


def check_tolerance_change(middle):
    if not any(name in middle for name in ('where_not_allclose','np.isclose','np.allclose')):
        raise ValueError('missing_tolerance_aware_change')
    if 'rtol' not in middle or 'atol' not in middle:
        raise ValueError('missing_tolerance_parameters')


def propagate_tolerance_keywords(raw, span, helper_symbol):
    """Add the caller-side tolerance contract at the selected call sites.

    The repair model is responsible for changing the helper's behaviour and
    signature. Once that candidate has passed the semantic tolerance gates,
    wiring the two known keyword arguments through a localized caller is a
    mechanical edit. Use AST positions to avoid asking a second model call to
    regenerate an otherwise unrelated method.
    """

    from .generator import build_patch
    text = raw.decode("utf-8-sig")
    lines = text.splitlines(keepends=True)
    starts = []
    offset = 0
    for line in lines:
        starts.append(offset)
        offset += len(line)

    def absolute_offset(lineno, byte_col):
        line = lines[lineno - 1]
        prefix = line.encode("utf-8")[:byte_col].decode("utf-8")
        return starts[lineno - 1] + len(prefix)

    wanted = span["symbol"]
    functions = []

    def find_functions(node, parents):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names = parents + [child.name]
                if (".".join(names) == wanted
                        and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))):
                    functions.append(child)
                find_functions(child, names)
            else:
                find_functions(child, parents)

    tree = ast.parse(text)
    find_functions(tree, [])
    if len(functions) != 1:
        raise ValueError("Unsupported or ambiguous caller symbol")

    helper_name = str(helper_symbol or "").rsplit(".", 1)[-1]
    insertions = []
    for node in ast.walk(functions[0]):
        if not isinstance(node, ast.Call):
            continue
        called_name = (node.func.id if isinstance(node.func, ast.Name)
                       else node.func.attr if isinstance(node.func, ast.Attribute)
                       else None)
        if called_name != helper_name:
            continue
        present = {keyword.arg for keyword in node.keywords if keyword.arg}
        missing = [name for name in ("rtol", "atol") if name not in present]
        if not missing:
            continue
        if any(keyword.arg is None for keyword in node.keywords):
            raise ValueError("cannot_propagate_tolerance_after_kwargs")
        close = absolute_offset(node.end_lineno, node.end_col_offset) - 1
        if text[close:close + 1] != ")" or not span["start"] <= close < span["end"]:
            raise ValueError("caller_call_position_outside_target")
        previous = close - 1
        while previous >= 0 and text[previous].isspace():
            previous -= 1
        if previous < 0:
            raise ValueError("caller_call_has_no_opening_delimiter")
        values = ", ".join(f"{name}=self.{name}" for name in missing)
        trailing = text[previous + 1:close]
        if text[previous] == "(":
            addition = values
        elif text[previous] == ",":
            if "\n" in trailing:
                arg_line = text[text.rfind("\n", 0, previous) + 1:previous]
                indent = arg_line[:len(arg_line) - len(arg_line.lstrip())]
                addition = "\n" + indent + values + ","
            else:
                addition = " " + values
        elif "\n" in trailing:
            arg_line = text[text.rfind("\n", 0, previous) + 1:previous + 1]
            indent = arg_line[:len(arg_line) - len(arg_line.lstrip())]
            addition = ",\n" + indent + values
        else:
            addition = ", " + values
        insertions.append((previous + 1, addition))

    if not insertions:
        raise ValueError("caller_has_no_selected_helper_call")
    middle = span["original_middle"]
    for position, addition in sorted(insertions, reverse=True):
        relative = position - span["start"]
        if not 0 <= relative <= len(middle):
            raise ValueError("caller_call_position_outside_target")
        middle = middle[:relative] + addition + middle[relative:]
    # The host transformation is accepted only if it remains valid Python.
    build_patch(raw, span, middle, "caller.py")
    return middle



def conversion_purpose(before, symbol, node):
    from .generator import _target_function_node
    # Only use a real immediately preceding comment on the corresponding
    # non-in-place sibling, not error text or arbitrary earlier comments.
    sibling = None
    if re.fullmatch(r'__i[a-z]+__', node.name):
        name = symbol.rsplit('.',1)[0]+'.__'+node.name[3:] if '.' in symbol else '__'+node.name[3:]
        try:
            sibling = _target_function_node(before, name)
        except ValueError:
            pass
    label = None
    if sibling is not None:
        comments = {t.start[0]:t for t in tokenize.generate_tokens(io.StringIO(before).readline)
                    if t.type == tokenize.COMMENT}
        line = min([sibling.lineno]+[d.lineno for d in sibling.decorator_list])-1
        token = comments.get(line)
        source_line = before.splitlines()[line-1] if line > 0 else ''
        if (token and not source_line[:token.start[1]].strip()
                and re.search(r'\b(?:convert|conversion)\b',token.string,re.I)):
            label = dict(symbol=name,line=line,text=token.string.lstrip('# ').strip(),
                         kind='related_operator_comment')
    return label
