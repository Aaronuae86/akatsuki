"""Worker scheduling with the judge's exact millisecond cost model."""

WORKER_COST = 2_000


def cost(workers, durations):
    if not workers:
        return 0
    return max(sum(durations[number] for number in group) for group in workers) + WORKER_COST * len(workers)


def greedy(items, k):
    workers = [[] for _ in range(k)]
    loads = [0] * k
    for number, duration in items:
        worker = min(range(k), key=lambda i: (loads[i], len(workers[i]), i))
        loads[worker] += duration
        workers[worker].append(number)
    return workers, loads


def pack(numbers, durations):
    if not numbers:
        return []
    items = sorted(((number, durations[number]) for number in numbers), key=lambda item: (-item[1], item[0]))
    largest = items[0][1]
    total = sum(duration for _, duration in items)
    best = [[number for number, _ in items]]
    best_cost = total + WORKER_COST
    for k in range(2, min(8, len(items)) + 1):
        lower = max(largest, (total + k - 1) // k)
        if lower + WORKER_COST * k >= best_cost:
            continue
        workers, loads = greedy(items, k)
        # Keep local search bounded on very large candidate sets.
        if max(loads) > lower and len(items) <= 96:
            improve(workers, loads, durations, lower)
        candidate = max(loads) + WORKER_COST * k
        if candidate < best_cost:
            best, best_cost = workers, candidate
    return best


def improve(workers, loads, durations, lower):
    for _ in range(3):
        largest = max(loads)
        if largest == lower or loads.count(largest) > 1:
            return
        source = loads.index(largest)
        best = largest
        move = None
        for target in sorted(range(len(loads)), key=loads.__getitem__):
            if target == source or loads[target] >= best:
                continue
            other_max = max((load for i, load in enumerate(loads) if i not in (source, target)), default=0)
            for first in workers[source]:
                for second in [None, *workers[target]]:
                    if second is None and len(workers[source]) == 1:
                        continue
                    delta = durations[first] - (durations[second] if second is not None else 0)
                    candidate = max(other_max, largest - delta, loads[target] + delta)
                    if candidate < best:
                        best, move = candidate, (target, first, second, delta)
        if move is None:
            return
        target, first, second, delta = move
        workers[source].remove(first)
        workers[target].append(first)
        if second is not None:
            workers[target].remove(second)
            workers[source].append(second)
        loads[source] -= delta
        loads[target] += delta
