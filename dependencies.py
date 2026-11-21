"""Translate an applied patch and a baseline index into candidate test IDs."""

import ast
from dataclasses import dataclass
import symtable
from types import CodeType
import warnings


SESSION = "<session>"
FUNCTION = (ast.FunctionDef, ast.AsyncFunctionDef)
TRACKED_CLASS_ATTRIBUTES = {
    "WEEK_OFFSET",
    "NUMERIC_ESCAPES",
    "SHOW_PARSERS",
    "arg_types",
    "required_args",
}
IDENTITY_PAIRS = {(ast.Is, ast.Eq), (ast.IsNot, ast.NotEq)}


@dataclass
class Impact:
    names: set[str]
    fallback: str = ""


def tree_signature(table):
    return (
        table.get_name(),
        sorted(
            (s.get_name(), s.is_local(), s.is_global(), s.is_nonlocal(), s.is_free(),
             s.is_parameter(), s.is_assigned(), s.is_imported())
            for s in table.get_symbols()
        ),
        [tree_signature(child) for child in table.get_children()],
    )


def code_signature(code):
    children = [item for item in code.co_consts if isinstance(item, CodeType)]
    return code.co_name, code.co_flags, [code_signature(child) for child in children]


def shape(node):
    return ast.dump(node, include_attributes=False)


def class_member(node):
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
    elif isinstance(node, ast.AnnAssign):
        target = node.target
    else:
        return None
    return target.id if isinstance(target, ast.Name) else None


def data_shape(node):
    """Compare configuration structure with scalar values erased."""
    if isinstance(node, ast.Constant):
        return type(node.value).__name__
    if isinstance(node, (ast.Tuple, ast.List)):
        return type(node).__name__, tuple(data_shape(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        return tuple(
            (shape(key) if key is not None else None, data_shape(value))
            for key, value in zip(node.keys, node.values)
        )
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return type(node.op).__name__, data_shape(node.operand)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return (
            node.func.id,
            tuple(shape(arg) for arg in node.args),
            tuple((arg.arg, data_shape(arg.value)) for arg in node.keywords),
        )
    return shape(node)


def compare_blocks(before, after, changed_functions, assignments, classes=()):
    if len(before) != len(after):
        return False
    for old, new in zip(before, after):
        if shape(old) == shape(new):
            continue
        if type(old) is not type(new):
            return False
        if isinstance(old, FUNCTION):
            if ast.get_docstring(old, clean=False) != ast.get_docstring(new, clean=False):
                return False
            fields = [field for field in old._fields if field != "body"]
            if ast.dump(type(old)(body=[], **{f: getattr(old, f) for f in fields})) != ast.dump(
                type(new)(body=[], **{f: getattr(new, f) for f in fields})
            ):
                return False
            changed_functions.append((old, new))
        elif isinstance(old, ast.ClassDef):
            fields = [field for field in old._fields if field != "body"]
            if ast.dump(ast.ClassDef(body=[], **{f: getattr(old, f) for f in fields})) != ast.dump(
                ast.ClassDef(body=[], **{f: getattr(new, f) for f in fields})
            ):
                return False
            if not compare_blocks(old.body, new.body, changed_functions, assignments, (*classes, old.name)):
                return False
        elif classes and (field := class_member(old)) in TRACKED_CLASS_ATTRIBUTES:
            if field != class_member(new) or not old.value or not new.value:
                return False
            if data_shape(old.value) != data_shape(new.value):
                return False
            # Match declared target and annotation as well as the value's structure.
            old_header = type(old)(**{f: getattr(old, f) for f in old._fields if f != "value"}, value=None)
            new_header = type(new)(**{f: getattr(new, f) for f in new._fields if f != "value"}, value=None)
            if shape(old_header) != shape(new_header):
                return False
            assignments.append((".".join(classes), field, old.value, new.value))
        elif not same_except_lambdas(old, new, changed_functions):
            return False
    return True


def same_except_lambdas(old, new, changed_functions):
    if type(old) is not type(new):
        return False
    if isinstance(old, ast.Lambda):
        if shape(old.args) != shape(new.args):
            return False
        if shape(old.body) != shape(new.body):
            changed_functions.append((old, new))
        return True
    if isinstance(old, ast.AST):
        return all(same_except_lambdas(getattr(old, key), getattr(new, key), changed_functions)
                   for key in old._fields)
    if isinstance(old, list):
        return len(old) == len(new) and all(same_except_lambdas(a, b, changed_functions)
                                            for a, b in zip(old, new))
    return old == new


def comparison_site(old, new):
    """Find a sole identity/equality operator substitution in otherwise equal ASTs."""
    if shape(old) == shape(new):
        return None
    matches = []

    def compare(before, after):
        if isinstance(before, ast.Compare) and isinstance(after, ast.Compare):
            if (
                len(before.ops) == len(after.ops) == 1
                and (type(before.ops[0]), type(after.ops[0])) in IDENTITY_PAIRS
                and shape(before.left) == shape(after.left)
                and shape(before.comparators[0]) == shape(after.comparators[0])
            ):
                matches.append(before)
                return True
        if type(before) is not type(after):
            return False
        if isinstance(before, ast.AST):
            return all(compare(getattr(before, key), getattr(after, key)) for key in before._fields)
        if isinstance(before, list):
            return len(before) == len(after) and all(compare(a, b) for a, b in zip(before, after))
        return before == after

    if compare(old, new) and len(matches) == 1:
        node = matches[0]
        return f"{node.lineno}:{node.col_offset}"
    return None


def code_ranges(change, old, new):
    if isinstance(old, ast.Lambda):
        return range(old.body.lineno, old.body.end_lineno + 1)
    first = old.body[0].lineno
    end = old.end_lineno
    full = range(first, end + 1)
    touched = {line for line in change.removed if first <= line <= end}
    inserted = {line for line in change.inserts if first <= line <= end + 1}
    if not touched or len(touched) != len(inserted) or end - old.lineno != new.end_lineno - new.lineno:
        return full
    # A coverage hit can be recorded at the start of a multiline expression
    # rather than at the line holding the edited comparison or literal.
    primary = touched.copy()
    for expr in ast.walk(old):
        if isinstance(expr, ast.expr) and any(expr.lineno <= n <= expr.end_lineno for n in primary):
            touched.add(expr.lineno)
    return touched


class DependencyIndex:
    def __init__(self, index, tests):
        self.names = {test["id"] for test in tests}
        self.files = index["files"]
        self.observed_attributes = index.get("observed_attributes", False)
        self.numeric_lookups = index.get("numeric_lookups", False)
        self.unverified = self.names & set(index.get("unverified", ()))
        self.unknown = self.names - set(index["tests"])

    def add(self, result, dependencies, lines=None):
        for scope, observed in dependencies.items():
            if lines is not None and not any(line in lines for line in observed):
                continue
            if scope == SESSION:
                return False
            if scope in self.names:
                result.add(scope)
            elif "::" not in scope or any(name.startswith(scope + "::") for name in self.names):
                result.update(name for name in self.names if name.startswith(scope + "::"))
        return True

    def fallback(self, reason):
        return Impact(set(self.names), reason)

    def candidates(self, changes):
        if self.unknown:
            return self.fallback("judge inventory differs from index")
        result = set(self.unverified)
        for change in changes:
            path = change.path
            if change.original is None or change.updated is None:
                return self.fallback(f"new or removed file {path}")
            if change.original == change.updated:
                continue
            dependencies = self.files.get(path, {})
            if path.startswith("tests/fixtures/"):
                if not dependencies:
                    return self.fallback(f"unmapped fixture {path}")
                if not self.add(result, dependencies):
                    return self.fallback(f"collection fixture {path}")
                continue
            if not path.startswith("sqlglot/") or not path.endswith(".py"):
                return self.fallback(f"test or non-Python source {path}")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                try:
                    old = ast.parse(change.original, filename=path)
                    new = ast.parse(change.updated, filename=path)
                    patched_code = compile(new, path, "exec", dont_inherit=True)
                except (SyntaxError, ValueError):
                    return self.fallback(f"invalid Python source {path}")
            if not self.add(result, dependencies, {0}):
                return self.fallback(f"source read during collection {path}")
            if shape(old) == shape(new):
                continue
            if not dependencies:
                return self.fallback(f"no baseline coverage for {path}")
            functions, assignments = [], []
            if not compare_blocks(old.body, new.body, functions, assignments):
                return self.fallback(f"structural Python edit {path}")
            if functions:
                observed = {}
                if len(changes) == 1 and not assignments:
                    site = comparison_site(old, new)
                    if site:
                        observed = self.files.get(f"@compare/{path}/{site}", {})
                previous = symtable.symtable(change.original, path, "exec")
                patched = symtable.symtable(change.updated, path, "exec")
                original_code = compile(old, path, "exec", dont_inherit=True)
                if (
                    tree_signature(previous) != tree_signature(patched)
                    or code_signature(original_code) != code_signature(patched_code)
                ):
                    return self.fallback(f"changed Python bindings {path}")
                for before, after in functions:
                    scope = {
                        scope: lines for scope, lines in dependencies.items()
                        if not (scope in observed and 2 not in observed[scope])
                    }
                    if not self.add(result, scope, code_ranges(change, before, after)):
                        return self.fallback(f"runtime code called during collection {path}")
            for owner, field, old_value, new_value in assignments:
                module = path.removesuffix(".py").replace("/", ".")
                if not self.add_assignment(result, module, owner, field, old_value, new_value):
                    return self.fallback(f"unresolved class configuration {path}:{owner}.{field}")
        return Impact(result)

    def add_assignment(self, result, module, owner, field, old_value, new_value):
        if not self.observed_attributes:
            return False
        keys = [field, "required_args"] if field == "arg_types" else [field]
        for key in keys:
            tag = f"@attr/{module}/{owner}/{key}"
            uses = self.files.get(tag, {})
            # The tokenizer's dialect metaclass reads only the keys of the
            # numeric-escape table. The shape check above preserves all keys.
            if field == "NUMERIC_ESCAPES":
                not_value_reads = {1}
                if self.numeric_lookups and isinstance(old_value, ast.Dict):
                    changed_keys = [
                        node.value for node, before, after in zip(old_value.keys, old_value.values, new_value.values)
                        if isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and shape(before) != shape(after)
                    ]
                    if changed_keys and all(isinstance(node, ast.Constant) for node in old_value.keys):
                        not_value_reads.add(3)
                        for key_name in changed_keys:
                            if not self.add(result, self.files.get(f"{tag}/key/{key_name}", {})):
                                return False
                        if not self.add(result, self.files.get("@numeric/unbound", {})):
                            return False
                uses = {
                    test: [line for line in lines if line not in not_value_reads]
                    for test, lines in uses.items()
                }
            # The expression metaclass converts arg_types into required_args.
            # The latter is tracked as a separate dependency above.
            if field == "arg_types" and key == field:
                uses = {
                    test: [line for line in lines if line != 2]
                    for test, lines in uses.items()
                }
            if not self.add(result, uses, {0}):
                return False
        return True
