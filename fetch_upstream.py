"""Fetch the one SQLGlot tree permitted by the contest build rules."""

from pathlib import Path
import shutil
import sys
import tarfile
from tempfile import TemporaryFile
from urllib.request import urlopen


REVISION = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def fetch(destination):
    archive_url = f"https://codeload.github.com/tobymao/sqlglot/tar.gz/{REVISION}"
    prefix = f"sqlglot-{REVISION}/"
    destination.mkdir(parents=True, exist_ok=True)
    with urlopen(archive_url, timeout=45) as response, TemporaryFile() as download:
        shutil.copyfileobj(response, download)
        download.seek(0)
        with tarfile.open(fileobj=download, mode="r:gz") as archive:
            for member in archive:
                if member.name == prefix[:-1]:
                    continue
                if not member.name.startswith(prefix):
                    raise ValueError("unexpected archive structure")
                member.name = member.name[len(prefix):]
                if member.issym() and member.name == "CLAUDE.md":
                    continue
                if not (member.isfile() or member.isdir()):
                    raise ValueError("unexpected archive entry")
                archive.extract(member, destination, filter="data")


if __name__ == "__main__":
    fetch(Path(sys.argv[1]))
