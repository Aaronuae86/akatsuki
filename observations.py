"""Build-time instrumentation for SQLGlot's class configuration reads."""

import ast
import builtins
from enum import Enum, Flag
import importlib.abc
import importlib.machinery
import os
from pathlib import Path
import sys

from dependencies import TRACKED_CLASS_ATTRIBUTES


CALLBACK = None
ATTRIBUTE_NAME = "__blast_radius_observe_receiver__"
COMPARE_NAME = "__blast_radius_observe_comparison__"
LOOKUP_NAME = "__blast_radius_observe_numeric_lookup__"
DECLARATIONS = {}
TYPE_CATEGORIES = {}
NUMERIC_TABLES = {}


def observe(receiver, name):
    callback = CALLBACK
    if callback is not None:
        cls = receiver if isinstance(receiver, type) else type(receiver)
        key = (cls, name)
        found = DECLARATIONS.get(key)
        if found is None:
            found = tuple(
                f"@attr/{base.__module__}/{base.__qualname__}/{name}"
                for base in cls.__mro__
                if name in vars(base) and base.__module__.startswith("sqlglot.")
            )
            DECLARATIONS[key] = found
        if found:
            marker = 0
            frame = sys._getframe(1)
            module = frame.f_globals.get("__name__")
            function = frame.f_code.co_qualname
            if name == "NUMERIC_ESCAPES":
                if (module, function) in {
                    ("sqlglot.dialects.dialect", "_Dialect.__new__"),
                    ("sqlglot.generator", "Generator.bytestring_sql"),
                    ("sqlglot.generator", "Generator.escape_str"),
                }:
                    marker = 1
                if (module, function) == ("sqlglot.tokens", "Tokenizer._init_core"):
                    table = getattr(receiver, name)
                    if type(table) is dict:
                        previous = NUMERIC_TABLES.get(id(table))
                        aliases = tuple(set(previous[1]).union(found)) if previous else found
                        NUMERIC_TABLES[id(table)] = (table, aliases)
                        marker = 3
            if name == "arg_types" and frame.f_code.co_qualname.endswith(".__init_subclass__"):
                if module == "sqlglot.expressions.core":
                    marker = 2
            for dependency in found:
                callback(dependency, marker)
    return receiver


def numeric_lookup(table, key):
    callback = CALLBACK
    if callback is not None:
        registered = NUMERIC_TABLES.get(id(table))
        if registered:
            for dependency in registered[1]:
                callback(f"{dependency}/key/{key}", 0)
        else:
            callback("@numeric/unbound", 0)
    return table.get(key)


def wrap(node, name):
    return ast.copy_location(
        ast.Call(func=ast.Name(id=ATTRIBUTE_NAME, ctx=ast.Load()), args=[node, ast.Constant(name)], keywords=[]),
        node,
    )


def compare_operands(left, right, site, negate):
    callback = CALLBACK
    if callback is not None:
        pair = (type(left), type(right))
        marker = TYPE_CATEGORIES.get(pair)
        if marker is None:
            a, b = pair
            marker = 2
            if b is type(None) and (
                a is type(None) or (a.__eq__ is object.__eq__ and a.__ne__ is object.__ne__)
            ):
                marker = 0
            elif a is b and issubclass(a, Enum) and not issubclass(a, Flag):
                if a.__eq__ is object.__eq__ and a.__ne__ is object.__ne__:
                    marker = 1
                elif a.__eq__ is str.__eq__ and a.__ne__ is str.__ne__:
                    members = list(a)
                    if not any(str.__eq__(x, y) for i, x in enumerate(members) for y in members[i + 1:]):
                        marker = 1
            TYPE_CATEGORIES[pair] = marker
        callback(site, marker)
    return left is not right if negate else left is right


class Reads(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or not isinstance(node.ops[0], (ast.Is, ast.IsNot)):
            return node
        right = node.comparators[0]
        if not isinstance(node.left, (ast.Attribute, ast.Call)):
            return node
        if not (
            isinstance(right, ast.Constant) and right.value is None
            or isinstance(right, ast.Attribute) and right.attr.isupper()
        ):
            return node
        site = f"@compare/{self.path}/{node.lineno}:{node.col_offset}"
        return ast.copy_location(
            ast.Call(
                func=ast.Name(id=COMPARE_NAME, ctx=ast.Load()),
                args=[node.left, right, ast.Constant(site), ast.Constant(isinstance(node.ops[0], ast.IsNot))],
                keywords=[],
            ),
            node,
        )

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load) and node.attr in TRACKED_CLASS_ATTRIBUTES:
            node.value = wrap(node.value, node.attr)
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        if (
            self.path == "sqlglot/tokenizer_core.py"
            and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "numeric_escapes"
            and len(node.args) == 1 and not node.keywords
        ):
            return ast.copy_location(
                ast.Call(func=ast.Name(id=LOOKUP_NAME, ctx=ast.Load()),
                         args=[node.func.value, node.args[0]], keywords=[]),
                node,
            )
        if (
            isinstance(node.func, ast.Name) and node.func.id in {"getattr", "hasattr"}
            and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str) and node.args[1].value in TRACKED_CLASS_ATTRIBUTES
        ):
            node.args[0] = wrap(node.args[0], node.args[1].value)
        return node


class ProfileLoader(importlib.machinery.SourceFileLoader):
    def __init__(self, fullname, path, relative_path):
        super().__init__(fullname, path)
        self.relative_path = relative_path

    def get_code(self, fullname):
        source = self.get_data(self.path)
        needs_comparisons = self.relative_path.startswith("sqlglot/") and b" is " in source
        if not needs_comparisons and b"numeric_escapes.get" not in source and not any(
            name.encode() in source for name in TRACKED_CLASS_ATTRIBUTES
        ):
            return self.source_to_code(source, self.path)
        tree = Reads(self.relative_path).visit(ast.parse(source, filename=self.path))
        return compile(ast.fix_missing_locations(tree), self.path, "exec", dont_inherit=True)


class ProfileFinder(importlib.abc.MetaPathFinder):
    def __init__(self, root):
        self.root = root

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] not in {"sqlglot", "tests"}:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            if Path(spec.origin).is_relative_to(self.root):
                relative_path = Path(spec.origin).relative_to(self.root).as_posix()
                spec.loader = ProfileLoader(fullname, spec.origin, relative_path)
                return spec
        return None


def install(root):
    if not any(isinstance(finder, ProfileFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, ProfileFinder(Path(root)))
        setattr(builtins, ATTRIBUTE_NAME, observe)
        setattr(builtins, COMPARE_NAME, compare_operands)
        setattr(builtins, LOOKUP_NAME, numeric_lookup)
    os.environ["BLAST_PROFILE_SOURCE"] = str(root)
