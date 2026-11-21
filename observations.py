"""Build-time instrumentation for SQLGlot's class configuration reads."""

import ast
import builtins
from enum import Enum, Flag
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import sys
from types import FunctionType

from dependencies import TRACKED_CLASS_ATTRIBUTES, class_member, integer_predicate, UNKNOWN_OPERAND


CALLBACK = None
ATTRIBUTE_NAME = "__blast_radius_observe_receiver__"
COMPARE_NAME = "__blast_radius_observe_comparison__"
LOOKUP_NAME = "__blast_radius_observe_numeric_lookup__"
DECLARATIONS = {}
TYPE_CATEGORIES = {}
NUMERIC_TABLES = {}
SCALAR_FIELDS = set()
LOADING = 0
INTEGER_NAME = "__blast_radius_observe_integer__"
GETATTR_NAME = "__blast_radius_observe_getattr__"
FACTORY_NAME = "__blast_radius_observe_factory__"
FACTORIES = set()
ORIGINS = {}
MAP_FIELDS = set()
MAP_GET_NAME = "__blast_radius_observe_map_get__"


def simple_factory(node):
    if not isinstance(node, ast.FunctionDef) or node.decorator_list:
        return False
    body = list(node.body)
    if ast.get_docstring(node) is not None:
        body = body[1:]
    if len(body) == 1 and isinstance(body[0], ast.Return):
        fn = body[0].value
        return isinstance(fn, ast.Lambda) and not (fn.args.defaults or any(fn.args.kw_defaults))
    if len(body) == 2 and isinstance(body[0], ast.FunctionDef) and isinstance(body[1], ast.Return):
        fn, result = body[0], body[1].value
        return (isinstance(result, ast.Name) and result.id == fn.name and not fn.decorator_list
                and not fn.args.defaults and not any(fn.args.kw_defaults))
    return False


def factory_call(site, factory, /, *args, **kwargs):
    value = factory(*args, **kwargs)
    if type(factory) is FunctionType and type(value) is FunctionType:
        if (factory.__module__, factory.__qualname__) in FACTORIES:
            code = value.__code__.replace()
            value.__code__ = code
            ORIGINS[id(code)] = (code, site)
            if CALLBACK is not None:
                CALLBACK("@factory-definition/" + site, 0)
            return value
    if CALLBACK is not None:
        CALLBACK("@factory-definition/" + site, 1)
    return value


def discover_scalars(root):
    declarations = set()
    bare_reads = set()
    map_candidates = set()
    unhandled = set()
    for path in (*((root / "sqlglot").rglob("*.py")), *((root / "tests").rglob("*.py"))):
        tree = ast.parse(path.read_bytes())
        module = ".".join(path.relative_to(root).with_suffix("").parts)
        FACTORIES.update((module, node.name) for node in tree.body if simple_factory(node))
        direct = {id(node.func.value) for node in ast.walk(tree) if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                  and isinstance(node.func.value, ast.Attribute)}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for statement in node.body:
                    name = class_member(statement)
                    if name and name.isupper() and isinstance(statement.value, ast.Constant):
                        if type(statement.value.value) in (bool, str, int):
                            declarations.add(name)
                    if name and name.isupper() and isinstance(statement.value, ast.Dict):
                        if any(isinstance(value, ast.Constant) for value in statement.value.values):
                            map_candidates.add(name)
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and id(node) not in direct:
                unhandled.add(node.attr)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                bare_reads.add(node.id)
    MAP_FIELDS.update((map_candidates - unhandled - bare_reads - TRACKED_CLASS_ATTRIBUTES) | {"TYPE_MAPPING"})
    return declarations - bare_reads - TRACKED_CLASS_ATTRIBUTES - MAP_FIELDS


def integer_value(value, site):
    if CALLBACK is not None:
        marker = value if type(value) is int and abs(value) < UNKNOWN_OPERAND else UNKNOWN_OPERAND
        CALLBACK(site, marker)
    return value


def dynamic_get(getter, receiver, name, *default):
    if (getter is getattr or getter is hasattr) and type(name) is str and (name in SCALAR_FIELDS or name in MAP_FIELDS):
        observe(receiver, name)
    return getter(receiver, name, *default)


def observe(receiver, name, value_tag=None):
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
                callback(dependency if value_tag is None else dependency + "/value/" + value_tag, marker)
                if LOADING and (name in SCALAR_FIELDS or name in MAP_FIELDS):
                    callback("@eager/" + dependency, 0)
    return receiver


def map_getter(receiver, name):
    mapping = getattr(receiver, name)
    method = mapping.get
    if type(mapping) is not dict:
        observe(receiver, name)
        return method

    def get(*args, **kwargs):
        result = method(*args, **kwargs)
        if type(result) in (str, int, bool):
            observe(receiver, name, repr(result))
        else:
            observe(receiver, name)
        return result
    return get


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

    def visit_Dict(self, node):
        self.generic_visit(node)
        for call in node.values:
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and any(call.func.id == name for _, name in FACTORIES)
                and all(isinstance(a, ast.Constant) for a in call.args)
                and all(k.arg and isinstance(k.value, ast.Constant) for k in call.keywords)):
                site = f"{self.path}/{call.lineno}:{call.col_offset}"
                call.args = [ast.Constant(site), call.func, *call.args]
                call.func = ast.Name(id=FACTORY_NAME, ctx=ast.Load())
        return node

    def visit_Compare(self, node):
        self.generic_visit(node)
        if integer_predicate(node):
            site = f"@int/{self.path}/{node.lineno}:{node.col_offset}"
            node.left = ast.copy_location(ast.Call(
                func=ast.Name(id=INTEGER_NAME, ctx=ast.Load()),
                args=[node.left, ast.Constant(site)], keywords=[],
            ), node.left)
            return node
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
        if isinstance(node.ctx, ast.Load) and (
            node.attr in TRACKED_CLASS_ATTRIBUTES or node.attr in SCALAR_FIELDS or node.attr in MAP_FIELDS
        ):
            node.value = wrap(node.value, node.attr)
        return node

    def visit_Call(self, node):
        if (isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            and isinstance(node.func.value, ast.Attribute) and node.func.value.attr in MAP_FIELDS):
            field = node.func.value
            node.func = ast.copy_location(ast.Call(
                func=ast.Name(id=MAP_GET_NAME, ctx=ast.Load()),
                args=[self.visit(field.value), ast.Constant(field.attr)], keywords=[]), node.func)
            node.args = [self.visit(arg) for arg in node.args]
            node.keywords = [self.visit(kw) for kw in node.keywords]
            return node
        self.generic_visit(node)
        if (isinstance(node.func, ast.Name) and node.func.id in {"getattr", "hasattr"}
            and len(node.args) >= 2 and not isinstance(node.args[1], ast.Constant)
            and not node.keywords):
            node.args.insert(0, node.func)
            node.func = ast.Name(id=GETATTR_NAME, ctx=ast.Load())
            return node
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
            and isinstance(node.args[1].value, str)
            and (node.args[1].value in TRACKED_CLASS_ATTRIBUTES or node.args[1].value in SCALAR_FIELDS
                 or node.args[1].value in MAP_FIELDS)
        ):
            node.args[0] = wrap(node.args[0], node.args[1].value)
        return node


class ProfileLoader(importlib.machinery.SourceFileLoader):
    def __init__(self, fullname, path, relative_path):
        super().__init__(fullname, path)
        self.relative_path = relative_path

    def get_code(self, fullname):
        source = self.get_data(self.path)
        tree = Reads(self.relative_path).visit(ast.parse(source, filename=self.path))
        return compile(ast.fix_missing_locations(tree), self.path, "exec", dont_inherit=True)

    def exec_module(self, module):
        global LOADING
        LOADING += 1
        try:
            super().exec_module(module)
        finally:
            LOADING -= 1


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
        catalog = os.environ.get("BLAST_SCALAR_FIELDS")
        fields = json.loads(catalog) if catalog else sorted(discover_scalars(Path(root)))
        if catalog:
            FACTORIES.update(map(tuple, json.loads(os.environ["BLAST_FACTORIES"])))
            MAP_FIELDS.update(json.loads(os.environ["BLAST_MAP_FIELDS"]))
        os.environ["BLAST_MAP_FIELDS"] = json.dumps(sorted(MAP_FIELDS))
        os.environ["BLAST_FACTORIES"] = json.dumps(sorted(FACTORIES))
        SCALAR_FIELDS.update(fields)
        os.environ["BLAST_SCALAR_FIELDS"] = json.dumps(fields)
        sys.meta_path.insert(0, ProfileFinder(Path(root)))
        setattr(builtins, ATTRIBUTE_NAME, observe)
        setattr(builtins, COMPARE_NAME, compare_operands)
        setattr(builtins, LOOKUP_NAME, numeric_lookup)
        setattr(builtins, INTEGER_NAME, integer_value)
        setattr(builtins, GETATTR_NAME, dynamic_get)
        setattr(builtins, FACTORY_NAME, factory_call)
        setattr(builtins, MAP_GET_NAME, map_getter)
    os.environ["BLAST_PROFILE_SOURCE"] = str(root)
