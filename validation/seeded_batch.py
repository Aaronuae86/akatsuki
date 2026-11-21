"""Sample unique patches by content hash, independently of scores and failure labels."""

import argparse
from hashlib import sha256
import json
from pathlib import Path


def assemble(batches, destination, count, seed):
    inventory = None
    patches = {}
    for batch in batches:
        tests = json.loads((batch / "tests.json").read_bytes())
        if inventory is not None and inventory != tests:
            raise ValueError(f"Different inventory in {batch}")
        inventory = tests
        labels = json.loads((batch / "answers.json").read_bytes())["commits"]
        for path in (batch / "commits").glob("*.patch"):
            data = path.read_bytes().replace(b"\r\n", b"\n")
            digest = sha256(data).hexdigest()
            answer = labels[path.stem]
            if digest in patches and set(patches[digest][1]["failing"]) != set(answer["failing"]):
                raise ValueError(f"Conflicting labels for {path}")
            patches[digest] = data, answer
    if not 0 < count <= len(patches):
        raise ValueError(f"Requested {count} of {len(patches)} distinct patches")
    chosen = sorted(patches, key=lambda digest: sha256(f"{seed}/{digest}".encode()).digest())[:count]
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "commits").mkdir()
    (destination / "tests.json").write_text(json.dumps(inventory))
    answers = {}
    for digest in chosen:
        data, answers[digest] = patches[digest]
        (destination / "commits" / f"{digest}.patch").write_bytes(data)
    (destination / "answers.json").write_text(json.dumps({"commits": answers}))
    print(f"Selected {count} of {len(patches)} distinct patches with seed {seed}")


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("destination", type=Path)
    cli.add_argument("batches", nargs="+", type=Path)
    cli.add_argument("--count", type=int, default=30)
    cli.add_argument("--seed", default="eb11aec4be8cb2122afe713a4816662c3f0fb2fa")
    args = cli.parse_args()
    assemble(args.batches, args.destination, args.count, args.seed)
