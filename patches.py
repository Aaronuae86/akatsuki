"""Apply the ordinary text diffs used by the dependency selector."""

from dataclasses import dataclass
from pathlib import PurePosixPath
import re


HEADER = re.compile(rb"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
METADATA = (b"index ", b"new file mode ", b"deleted file mode ")


@dataclass
class Change:
    path: str
    original: bytes | None
    updated: bytes | None
    removed: set[int]
    inserts: set[int]


def parse_path(line, prefix):
    path = line.removeprefix(prefix).rstrip(b"\r\n").decode("utf-8")
    if path == "/dev/null":
        return None
    expected = "a/" if prefix == b"--- " else "b/"
    if not path.startswith(expected):
        raise ValueError("unsupported diff path")
    path = path[2:]
    pure = PurePosixPath(path)
    parts = pure.parts
    if (
        len(parts) < 2 or parts[0] not in ("sqlglot", "tests") or ".." in parts
        or "\\" in path or pure.as_posix() != path
    ):
        raise ValueError("path outside supported source trees")
    if any(char in path for char in "\t\r\n\x00:"):
        raise ValueError("unsupported path character")
    return path


def read_hunk(lines, position, header):
    old_count = int(header[2] or 1)
    new_count = int(header[4] or 1)
    old = []
    new = []
    removed = set()
    inserts = set()
    while len(old) < old_count or len(new) < new_count:
        line = lines[position]
        position += 1
        marker, payload = line[:1], line[1:]
        if position < len(lines) and lines[position].startswith(b"\\ No newline at end of file"):
            payload = payload.removesuffix(b"\n")
            position += 1
        if marker == b"-":
            removed.add(len(old))
        if marker == b"+":
            inserts.add(len(old))
        if marker in (b" ", b"-"):
            old.append(payload)
        if marker in (b" ", b"+"):
            new.append(payload)
        if marker not in (b" ", b"-", b"+") or len(old) > old_count or len(new) > new_count:
            raise ValueError("invalid hunk")
    return position, old, new, removed, inserts


def apply_diff(root, patch):
    lines = patch.splitlines(keepends=True)
    position = 0
    changes = {}
    while position < len(lines):
        if not lines[position].startswith(b"diff --git "):
            raise ValueError("missing git diff header")
        position += 1
        while lines[position].startswith(METADATA):
            position += 1
        if not lines[position].startswith(b"--- ") or not lines[position + 1].startswith(b"+++ "):
            raise ValueError("unsupported diff metadata")
        old_path = parse_path(lines[position], b"--- ")
        new_path = parse_path(lines[position + 1], b"+++ ")
        path = old_path or new_path
        if not path or (old_path and new_path and old_path != new_path) or path in changes:
            raise ValueError("rename or duplicate diff")
        original = (root / old_path).read_bytes() if old_path else None
        old_lines = original.splitlines(keepends=True) if original else []
        result = []
        cursor = 0
        position += 2
        removed = set()
        inserts = set()
        hunk_count = 0
        while position < len(lines) and (header := HEADER.match(lines[position])):
            position, old, new, drops, additions = read_hunk(lines, position + 1, header)
            hunk_count += 1
            start = int(header[1]) - bool(old)
            if start < cursor or start > len(old_lines) or old_lines[start:start + len(old)] != old:
                matches = [
                    index for index in range(cursor, len(old_lines) - len(old) + 1)
                    if old_lines[index:index + len(old)] == old
                ]
                if len(matches) != 1:
                    raise ValueError("hunk context is missing or ambiguous")
                start = matches[0]
            result.extend(old_lines[cursor:start])
            result.extend(new)
            removed.update(start + offset + 1 for offset in drops)
            inserts.update(start + offset + 1 for offset in additions)
            cursor = start + len(old)
        if not hunk_count:
            raise ValueError("file has no text hunks")
        result.extend(old_lines[cursor:])
        if not new_path and result:
            raise ValueError("deleted file has leftover content")
        changes[path] = Change(path, original, b"".join(result) if new_path else None, removed, inserts)
    if not changes:
        raise ValueError("empty patch")
    return list(changes.values())
