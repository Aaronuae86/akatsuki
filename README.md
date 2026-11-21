# Blast Radius

Selects SQLGlot tests for independent patches, then schedules the selection on
up to eight workers. Build from this directory with the included Dockerfile.
The image reads the judge's `/data/tests.json` and `/data/commits/*.patch`
and emits one JSON line per commit.

## Strategy

1. Profile only SQLGlot at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` during the
   build. Record per-test lines, source and fixture reads, collection and
   shared setup, and coverage from spawned process pools. Record calls to lambdas
   separately from execution of the line declaring them. Give each supported
   factory-created callback a distinct code object during profiling so calls can
   be attributed to the dictionary entry that created the callback.
   Record class configuration reads, selected dictionary values and bounded
   integer operands for comparisons. The selector reads the relevant index
   entries from a read-only SQLite database and translates pytest node IDs using
   the judge's inventory.
2. Validate and independently apply each unified diff to the pinned source.
   Compare Python structure and lexical bindings before using baseline
   coverage. Widen for additions within an existing function and retain the
   whole suite for unsupported, invalid, or collection-sensitive changes.
   Preserve explicit source readers on equivalent ASTs. For a sole supported
   comparison edit, inspect whether identity and equality are equivalent for
   the operand types observed at that site. For an integer threshold edit, keep
   tests with changed or unknown comparison results. If a new local variable
   changes a function's lexical bindings, include callers and tests executing
   nested code. Use whole-suite fallback for unresolved import-time dependencies.
   For a change after an unchanged short-circuit condition, exclude tests only
   when their recorded observations show that evaluation stopped before the
   change. For an edit confined to one arm of an unchanged `if`, use coverage
   at the first statement when it is an unconditional call.
   Literal dispatch-table edits use observed expression or type keys, including
   type lookups that use the default for a missing key. For type mappings,
   ordinary copies into derived tables preserve the key dependency. Whole-table
   uses and unresolved import-time reads retain broader coverage.
3. Choose the worker count by makespan plus 2,000 ms per nonempty worker.
   Try all potentially better worker counts with longest-first assignment
   and bounded moves and swaps.

The submission uses the dependency policy by default. The optional
`--verify` mode applies the complete patch in a temporary tree and runs pytest.
It retains a test if a phase or subtest fails, if completion is uncertain,
or if the verifier runs out of time. A collection error widens the selection.
Baseline profiling, selection, and verification have no access to challenge
labels. The sample and validation inputs are excluded from the image.

## Reproduce

The original contest kit supplies the evaluation data. Extract the kit in a
separate folder, then copy only its `sample/` directory here. Keep the
submission's Dockerfile and selector.

```sh
make sample
python validation/benchmark.py --target 138
```

The benchmark runs the container three times under the sample limits and
adds CPU charged to its cgroup. It reports the median rounded as specified by
the judge. It uses the sample answers on the host to check the container's
output. See [SCORING.md](SCORING.md) for the exact rules.

The previous entry scored 107.2 seconds on the public sample. Its official
practice result included 385.249 simulated seconds, approximately 1.93 CPU
seconds and zero misses. The new dependency policy targets the simulated
cost of broad selections. On the public sample it used 105.919 simulated
seconds with zero misses.

On a diagnostic batch of 21 seeded public patches and nine fresh mutations,
the previous and revised policies used 312.489 and 62.884 simulated seconds,
respectively. Both covered every labeled failure. The nine mutations exercise
known weaknesses in the previous policy. The hidden batch composition is
unknown. Full pytest runs provide the mutation labels. They stay on the
validation host. Use `validation/holdout.py --case NAME --export DIR` to
generate labels and `validation/mixed_batch.py` to assemble the comparison.

The revision at `eceb40b048cc` scored 317.4 seconds with zero misses in
official practice. The added guard analysis reduced two independently labeled
parser changes from 73.634 to 10.908 simulated seconds and retained all seven
failing tests across those changes. Generate the labels with the
`table-sample-guard` and `table-sample-block` cases in
`validation/holdout.py`. The 41-patch validation batch retained its previous
137.798 simulated seconds and zero misses. The guard cases measure the
improvement on that edit pattern. The frequency of those edits in the
hidden batch is unknown.

The selector and verifier regression tests use Python 3.13 and the supplied
dependency lock. Run them from this directory.

```sh
python -m pip install -r requirements.lock
python -m pytest -q validation
```

### Local judge

`validation/corpus.py` freezes independent development and holdout mutations,
then labels them by running the complete upstream suite. `validation/audit.py`
compares local selector output with schedules for the known failing tests.
The corpus records failures reported by pytest, including per-test timeouts
and tests in files that fail to import. Use `label --retry-unscored` to retry
unfinished cases in smaller groups. Cases with missing outcomes remain
unscored. Set `freeze --seed` and `--per-family` to create a fresh set before
benchmarking a new selector.

`validation/optimizer_probe.py` measures replay of the optimization stage for
TPC-DS executor inputs. Its comparisons are diagnostic. They cover the
optimizer output and mutated input, with the execution and DuckDB
comparison phases outside the probe.

To compare candidates locally, build the candidate image, then run the local
judge against the last known good image and one or more labeled batches.

```sh
docker build -t blast-radius-candidate .
python validation/judge.py --baseline blast-radius-v3-final \
  --candidate blast-radius-candidate --batch sample \
  --batch /validation/revision4/holdout --report local-judge.json
```

Each batch needs `tests.json`, `commits/`, and `answers.json`. The judge
passes the patches and test inventory into the container and reads labels on
the host. It uses the image's configured Docker command and measures total
container CPU, including child processes. This repository's images include
Python for the measurement wrapper. Practice mode uses three paired trials
with a 180-second wall limit. `--mode final` uses five paired trials with a
270-second wall limit. A rejected run or any candidate miss fails the
command. With one batch, `--target 130` also checks the strict local score
threshold. The failing-tests-only lower bound explains when the target is
impossible on that batch. The official hidden cases and host CPU timings
remain unknown.

To compare selections against full pytest results on fresh mutations of the
pinned tree, run the held-out validator in the image.

```sh
docker run --rm --network none --cpus 2 --memory 4096m --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --env HOME=/tmp \
  -v "$PWD/sample/tests.json:/data/tests.json:ro" \
  -v "$PWD/validation/holdout.py:/tmp/holdout.py:ro" \
  --entrypoint python blast-radius-entry /tmp/holdout.py
```

## Submit

Push this folder as the root of your team's public GitHub repository. Enter
the repository URL and commit SHA on the track page. Keep the repository
public and each submitted commit reachable until the results are published.
After practice, explicitly select the commit to score in the final. Use a
known-valid submission early if the practice queue and tie-break timing matter.

Baseline coverage and observed run-time types describe the pinned suite.
They are empirical evidence when predicting unseen changes. Local zero-miss
results and CPU measurements apply to the inputs and machines tested.
