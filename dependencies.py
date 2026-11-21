"""Translate an applied patch and a baseline index into candidate test IDs."""

import ast
from dataclasses import dataclass
import symtable
from types import CodeType
import operator
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
ORDER = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt,
         ast.GtE: operator.ge, ast.Eq: operator.eq, ast.NotEq: operator.ne}
UNKNOWN_OPERAND = 1 << 40
DISPATCH_FIELD = "TRANSFORMS"
TYPE_FIELD = "TYPE_MAPPING"


def integer_predicate(node):
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in ORDER:
        right = node.comparators[0]
        if isinstance(right, ast.Constant) and type(right.value) is int:
            return ORDER[type(node.ops[0])], right.value
    return None


def threshold_edit(before, after):
    """Recognize a single changed predicate with identical left-hand evaluation."""
    changes = []

    def match(a, b):
        if type(a) is not type(b):
            return False
        if isinstance(a, ast.Compare):
            x, y = integer_predicate(a), integer_predicate(b)
            if x and y and x != y and shape(a.left) == shape(b.left):
                changes.append((a, x, y))
                return True
        if isinstance(a, ast.AST):
            return all(match(getattr(a, f), getattr(b, f)) for f in a._fields)
        if isinstance(a, list):
            return len(a) == len(b) and all(match(x, y) for x, y in zip(a, b))
        return a == b

    if match(before, after) and len(changes) == 1:
        return changes[0]


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
            if not (s.is_global() and not s.is_declared_global() and table.get_name() != "top"
                    and not s.is_local() and not s.is_assigned())
        ),
        [tree_signature(child) for child in table.get_children()],
    )


def code_signature(code):
    children = [item for item in code.co_consts if isinstance(item, CodeType)]
    return code.co_name, code.co_flags, [code_signature(child) for child in children]


def shape(node):
    return ast.dump(node, include_attributes=False)


def factory_edits(a, b, path, files, sites):
    """Match edits confined to literal arguments of observed closure factories."""
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.Call) and isinstance(a.func, ast.Name) and shape(a.func) == shape(b.func):
        site = f"{path}/{a.lineno}:{a.col_offset}"
        registrations = files.get("@factory-definition/" + site)
        if registrations and all(markers == [0] for markers in registrations.values()):
            left = a.args + [k.value for k in a.keywords]
            right = b.args + [k.value for k in b.keywords]
            if (len(a.args) == len(b.args) and [k.arg for k in a.keywords] == [k.arg for k in b.keywords]
                and all(isinstance(x, ast.Constant) and isinstance(y, ast.Constant)
                        and type(x.value) is type(y.value) for x, y in zip(left, right))):
                if any(shape(x) != shape(y) for x, y in zip(left, right)):
                    sites.append(site)
                return True
    if isinstance(a, ast.AST):
        return all(factory_edits(getattr(a, f), getattr(b, f), path, files, sites) for f in a._fields)
    if isinstance(a, list):
        return len(a) == len(b) and all(factory_edits(x, y, path, files, sites) for x, y in zip(a, b))
    return a == b


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


def changed_literals(old, new):
    if not isinstance(old, ast.Dict) or not isinstance(new, ast.Dict) or data_shape(old) != data_shape(new):
        return []
    changed = []
    for left, right in zip(old.values, new.values):
        if shape(left) != shape(right):
            if not isinstance(left, ast.Constant) or type(left.value) not in (str, int, bool):
                return []
            changed.append(left.value)
    return changed


def deleted_dispatch_entries(old, new):
    """Match only deleted literal entries and preserve the order of all other items."""
    if not isinstance(old, ast.Dict) or not isinstance(new, ast.Dict) or len(old.keys) <= len(new.keys):
        return []
    wanted = iter(zip(new.keys, new.values))
    current = next(wanted, None)
    removed = []
    for key, value in zip(old.keys, old.values):
        if current and (
            key is current[0] or (key is not None and current[0] is not None and shape(key) == shape(current[0]))
        ) and shape(value) == shape(current[1]):
            current = next(wanted, None)
            continue
        if not (isinstance(key, ast.Attribute) and isinstance(key.value, ast.Name) and key.value.id == "exp"):
            return []
        removed.append((key.attr, value))
    return removed if current is None else []


def dtype_literal(node):
    path = ast.unparse(node) if isinstance(node, ast.Attribute) else ""
    parts = path.split(".")
    if len(parts) == 3 and parts[:2] == ["exp", "DType"]:
        return parts[2]


def changed_type_keys(old, new):
    if not isinstance(old, ast.Dict) or not isinstance(new, ast.Dict):
        return set()
    def split(node):
        literal = {}
        rest = []
        for key, value in zip(node.keys, node.values):
            name = dtype_literal(key)
            if not (name and isinstance(value, ast.Constant) and type(value.value) is str):
                rest.append((shape(key) if key else "", shape(value)))
                continue
            if name in literal:
                return None, None
            literal[name] = value.value
        return literal, rest
    before, old_rest = split(old)
    after, new_rest = split(new)
    # A changed ordering around a spread can change which value wins. Limit
    # this rule to an unchanged prefix of spreads and a suffix of literals.
    if before is None or after is None or old_rest != new_rest:
        return set()
    if [name for name in before if name in after] != [name for name in after if name in before]:
        return set()
    for node, rest in ((old, old_rest), (new, new_rest)):
        if any(key is not None for key in node.keys[:len(rest)]) or any(
            key is None for key in node.keys[len(rest):]
        ):
            return set()
    return {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}


def compare_blocks(before, after, changed_functions, assignments, classes=(), scalar_fields=(), map_fields=()):
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
            if not compare_blocks(old.body, new.body, changed_functions, assignments, (*classes, old.name),
                                  scalar_fields, map_fields):
                return False
        elif classes and (
            class_member(old) == class_member(new) == DISPATCH_FIELD and deleted_dispatch_entries(old.value, new.value)
            or class_member(old) == class_member(new) == TYPE_FIELD and changed_type_keys(old.value, new.value)
        ):
            old_header = type(old)(**{f: getattr(old, f) for f in old._fields if f != "value"}, value=None)
            new_header = type(new)(**{f: getattr(new, f) for f in new._fields if f != "value"}, value=None)
            if shape(old_header) != shape(new_header):
                return False
            assignments.append((".".join(classes), class_member(old), old.value, new.value))
        elif classes and ((field := class_member(old)) in TRACKED_CLASS_ATTRIBUTES or (
            field in scalar_fields and isinstance(old.value, ast.Constant)
            and type(old.value.value) in (str, int, bool)
        ) or (field in map_fields and changed_literals(old.value, new.value))):
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


def guard_site(path, node):
    return f"@guard/{path}/{node.lineno}:{node.col_offset}"


@dataclass(frozen=True)
class GuardEdit:
    anchor: ast.expr
    unchanged: int
    entry_lines: range | None = None


def guard_edit(old, new):
    """Find an unchanged condition that must allow an edit to execute."""
    changes = []

    def same_statements(left, right):
        return len(left) == len(right) and all(shape(x) == shape(y) for x, y in zip(left, right))

    def match(a, b):
        if type(a) is not type(b):
            return False
        if isinstance(a, ast.If) and shape(a.test) == shape(b.test) and not isinstance(a.test, ast.BoolOp):
            changed_body = not same_statements(a.body, b.body)
            changed_else = not same_statements(a.orelse, b.orelse)
            if changed_body != changed_else:
                branch = a.body if changed_body else a.orelse
                first = branch[0] if branch else None
                # An expression statement that calls a function always has
                # executable code at branch entry, even with a multiline call.
                entry = (range(first.lineno, first.end_lineno + 1)
                         if isinstance(first, ast.Expr) and isinstance(first.value, ast.Call) else None)
                changes.append(GuardEdit(a.test, 0 if changed_body else 1, entry))
                return True
        if isinstance(a, (ast.If, ast.While, ast.IfExp)) and shape(a.test) != shape(b.test):
            before, after = a.test, b.test
            left = before.op if isinstance(before, ast.BoolOp) else None
            right = after.op if isinstance(after, ast.BoolOp) else None
            if (left or right) and (not left or not right or type(left) is type(right)):
                before_parts = before.values if left else [before]
                after_parts = after.values if right else [after]
                anchor = None
                for x, y in zip(before_parts, after_parts):
                    if shape(x) != shape(y):
                        break
                    anchor = x
                if anchor is not None and not isinstance(anchor, (ast.BoolOp, ast.IfExp)):
                    changes.append(GuardEdit(anchor, 0 if isinstance(left or right, ast.And) else 1))
                    return all(match(getattr(a, field), getattr(b, field))
                               for field in a._fields if field != "test")
        if isinstance(a, ast.AST):
            return all(match(getattr(a, field), getattr(b, field)) for field in a._fields)
        if isinstance(a, list):
            return len(a) == len(b) and all(match(x, y) for x, y in zip(a, b))
        return a == b

    if match(old, new) and len(changes) == 1:
        return changes[0]


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
        self.scalar_fields = set(index.get("scalar_fields", []))
        self.map_fields = set(index.get("map_fields", []))
        self.call_coverage = index.get("call_coverage", False)
        self.function_calls = index.get("function_calls", False)
        self.dispatch_exports = set(index.get("dispatch_exports", ()))
        self.dispatch_modules = set(index.get("dispatch_modules", ()))
        self.dtype_names = index.get("dtype_names", {})
        self.factory_paths = {key.removeprefix("@factory-definition/").rsplit("/", 1)[0]
                              for key in self.files if key.startswith("@factory-definition/")}
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
            test_module = path.startswith("tests/") and path.split("/")[-1].startswith("test_")
            if not (path.startswith("sqlglot/") or test_module) or not path.endswith(".py"):
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
            if not compare_blocks(old.body, new.body, functions, assignments, scalar_fields=self.scalar_fields,
                                  map_fields=self.map_fields):
                sites = []
                if (len(changes) == 1 and path in self.factory_paths
                    and factory_edits(old, new, path, self.files, sites) and sites
                    and all(self.add(result, self.files.get("@factory-call/" + site, {})) for site in sites)):
                    continue
                return self.fallback(f"structural Python edit {path}")
            if test_module:
                result.update(name for name in self.names if name.startswith(path + "::"))
            isolated_assignment = len(changes) == 1 and len(assignments) == 1 and not functions
            if functions:
                observed = {}
                entry_lines = None
                if len(changes) == 1 and not assignments:
                    site = comparison_site(old, new)
                    if site:
                        observed = self.files.get(f"@compare/{path}/{site}", {})
                    elif edit := threshold_edit(old, new):
                        node, (op1, value1), (op2, value2) = edit
                        tag = f"@int/{path}/{node.lineno}:{node.col_offset}"
                        observed = {
                            scope: [0] for scope, values in self.files.get(tag, {}).items()
                            if values and UNKNOWN_OPERAND not in values
                            and all(op1(v, value1) == op2(v, value2) for v in values)
                        }
                    elif edit := guard_edit(old, new):
                        entry_lines = edit.entry_lines
                        observed = {
                            scope: [0] for scope, values in self.files.get(guard_site(path, edit.anchor), {}).items()
                            if values == [edit.unchanged]
                        }
                previous = symtable.symtable(change.original, path, "exec")
                patched = symtable.symtable(change.updated, path, "exec")
                original_code = compile(old, path, "exec", dont_inherit=True)
                binding_change = tree_signature(previous) != tree_signature(patched)
                if (code_signature(original_code) != code_signature(patched_code)
                    or (binding_change and not self.function_calls)):
                    return self.fallback(f"changed Python bindings {path}")
                for before, after in functions:
                    scope = {
                        scope: lines for scope, lines in dependencies.items()
                        if not (scope in observed and 2 not in observed[scope])
                    }
                    if binding_change:
                        # A new local can alter name lookup before the edited line.
                        # Retain callers and all tests executing nested code in this body.
                        first = min([before.lineno, *(n.lineno for n in getattr(before, "decorator_list", []))])
                        calls = self.files.get("@calls/" + path, {})
                        body = before.body if isinstance(before, ast.Lambda) else before.body[0]
                        if (not self.add(result, calls, range(first, before.end_lineno + 1))
                            or not self.add(result, dependencies, range(body.lineno, before.end_lineno + 1))):
                            return self.fallback(f"changed bindings used during collection {path}")
                    elif isinstance(before, ast.Lambda) and self.call_coverage:
                        # Class dictionary construction and lambda calls can share a source line.
                        calls = self.files.get(f"@lambda/{path}", {})
                        if not self.add(result, calls, {before.lineno}):
                            return self.fallback(f"lambda executed during collection {path}")
                    elif not self.add(result, scope, entry_lines or code_ranges(change, before, after)):
                        return self.fallback(f"runtime code called during collection {path}")
            for owner, field, old_value, new_value in assignments:
                module = path.removesuffix(".py").replace("/", ".")
                if not self.add_assignment(result, module, owner, field, old_value, new_value, path, isolated_assignment):
                    return self.fallback(f"unresolved class configuration {path}:{owner}.{field}")
        return Impact(result)

    def add_assignment(self, result, module, owner, field, old_value, new_value, path, isolated_assignment):
        if not self.observed_attributes:
            return False
        if (field == TYPE_FIELD and isolated_assignment and (keys := changed_type_keys(old_value, new_value))
            and self.dtype_names
            and (data_shape(old_value) != data_shape(new_value)
                 or self.files.get(f"@eager/@attr/{module}/{owner}/{field}"))):
            if module not in self.dispatch_modules or self.files.get("@type-map/eager"):
                return False
            if not keys <= self.dtype_names.keys():
                return False
            names = {self.dtype_names[key] for key in keys}
            if any(self.files.get("@type-map/eager/key/" + name) for name in names):
                return False
            if not self.add(result, self.files.get("@type-map/whole", {})):
                return False
            return all(self.add(result, self.files.get("@type-map/key/" + name, {})) for name in names)
        if field == TYPE_FIELD and data_shape(old_value) != data_shape(new_value):
            # Old coverage indices track values only and need the original keys.
            return False
        if field == DISPATCH_FIELD:
            tag = f"@attr/{module}/{owner}/{field}"
            if not isolated_assignment or module not in self.dispatch_modules or self.files.get("@eager/" + tag):
                return False
            for key, value in deleted_dispatch_entries(old_value, new_value):
                if key not in self.dispatch_exports:
                    return False
                if isinstance(value, ast.Lambda) and not (value.args.defaults or any(value.args.kw_defaults)):
                    pass
                elif isinstance(value, ast.Call):
                    location = f"{path}/{value.lineno}:{value.col_offset}"
                    definitions = self.files.get("@factory-definition/" + location, {})
                    pure = self.files.get("@pure-dispatch-factory/" + location, {})
                    if not definitions or pure != definitions:
                        return False
                else:
                    return False
                if not self.add(result, self.files.get(f"{tag}/key/{key}", {})):
                    return False
            return self.add(result, self.files.get(tag, {}))
        keys = [field, "required_args"] if field == "arg_types" else [field]
        for key in keys:
            tag = f"@attr/{module}/{owner}/{key}"
            if self.files.get("@eager/" + tag):
                return False
            if field in self.map_fields:
                for literal in changed_literals(old_value, new_value):
                    if not self.add(result, self.files.get(tag + "/value/" + repr(literal), {})):
                        return False
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
