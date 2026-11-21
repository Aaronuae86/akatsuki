"""Combine independently labeled diagnostic patches with a seeded public sample."""

import argparse
import json
from pathlib import Path
from random import Random
import shutil


def assemble(sample, labels, destination):
    target = destination / "commits"
    target.mkdir(parents=True)
    shutil.copyfile(sample / "tests.json", destination / "tests.json")
    answers = {}
    for folder in labels:
        for patch in sorted((folder / "commits").glob("*.patch")):
            if patch.stem in answers:
                raise ValueError(f"duplicate commit {patch.stem}")
            failing = json.loads((folder / f"{patch.stem}.json").read_text())
            answers[patch.stem] = {"failing": failing}
            shutil.copyfile(patch, target / patch.name)
    sample_answers = json.loads((sample / "answers.json").read_text())["commits"]
    count = 30 - len(answers)
    for name in Random(20261121).sample(sorted(sample_answers), count):
        answers[name] = sample_answers[name]
        shutil.copyfile(sample / "commits" / (name + ".patch"), target / (name + ".patch"))
    (destination / "answers.json").write_text(json.dumps({"commits": answers}))
    print(f"Assembled {count} public patches and {30 - count} independently labeled patches")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("labels", nargs="+", type=Path)
    args = parser.parse_args()
    assemble(args.sample, args.labels, args.destination)
