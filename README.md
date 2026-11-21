# Blast Radius

Selects SQLGlot tests for independent patches, then schedules the selection on
up to eight workers. Build from this directory with the included Dockerfile.
The image reads the judge's `/data/tests.json` and `/data/commits/*.patch`
and emits one JSON line per commit.

## Strategy

1. Profile only SQLGlot at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` during the
   build. Record per-test lines, source and fixture reads, collection and
   shared setup, and coverage from spawned process pools. Record the receiving
   class for selected configuration attributes and the consumed numeric-escape
   keys. Index by pytest node ID, then translate IDs using the judge's inventory.
2. Validate and independently apply each unified diff to the pinned source.
   Compare Python structure and lexical bindings before using baseline
   coverage. Widen for additions within an existing function and retain the
   whole suite for unsupported, invalid, or collection-sensitive changes.
   Preserve explicit source readers on equivalent ASTs. For a sole supported
   comparison edit, inspect whether identity and equality are equivalent for
   the operand types observed at that site.
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

In local Linux/amd64 trials, the dependency policy's simulated time was
105.935 seconds and its CPU-inclusive score was about 107 seconds, with zero
misses on the 30 supplied patches. The rerun policy reduced simulated time
to 94.373 seconds, but its additional CPU raised the total. That tradeoff
determined the submission default.

The selector and verifier regression tests use Python 3.13 and the supplied
dependency lock. Run them from this directory.

```sh
python -m pip install -r requirements.lock
python -m pytest -q validation
```

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
