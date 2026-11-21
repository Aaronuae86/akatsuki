"""Read baseline dependencies on demand from the immutable image."""

from functools import lru_cache
import json
from pathlib import Path
import sqlite3
import sys


class FileDependencies:
    def __init__(self, connection):
        self.connection = connection

    @lru_cache(maxsize=128)
    def read(self, key):
        row = self.connection.execute("SELECT data FROM files WHERE path = ?", (key,)).fetchone()
        return None if row is None else json.loads(row[0])

    def get(self, key, default=None):
        value = self.read(key)
        return default if value is None else value

    def __iter__(self):
        return (key for key, in self.connection.execute("SELECT path FROM files"))


def load_index(path):
    if path.suffix != ".sqlite":
        return json.loads(path.read_bytes())
    db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    index = json.loads(db.execute("SELECT data FROM metadata").fetchone()[0])
    index["files"] = FileDependencies(db)
    return index


def convert(source, destination):
    index = json.loads(source.read_bytes())
    files = index.pop("files")
    with sqlite3.connect(destination) as db:
        db.execute("CREATE TABLE metadata(data TEXT NOT NULL)")
        db.execute("INSERT INTO metadata VALUES (?)", (json.dumps(index),))
        db.execute("CREATE TABLE files(path TEXT PRIMARY KEY, data TEXT NOT NULL) WITHOUT ROWID")
        db.executemany("INSERT INTO files VALUES (?, ?)", (
            (path, json.dumps(data, separators=(",", ":"))) for path, data in files.items()
        ))
    db.close()


if __name__ == "__main__":
    convert(Path(sys.argv[1]), Path(sys.argv[2]))
