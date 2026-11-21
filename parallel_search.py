"""Run independent search variants in worker processes and keep the best valid plan.

Variant 0 runs in the calling process, so a failed or missing worker never loses
the single-process result. Workers differ only by their random streams and first
contact order. The official scorer re-validates any worker plan before it wins.
"""
import multiprocessing
import os
import time

from fleetkit.scorer import evaluate


def available_cores():
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def _worker(factory, scenario, kwargs, variant, conn):
    try:
        verified = factory(scenario, variant=variant, **kwargs).run()
        conn.send([(state.plan(scenario.seed), result.score) for state, result in verified])
    except BaseException:
        pass
    finally:
        conn.close()


def run_parallel(factory, scenario, kwargs, workers, hard_stop):
    """Returns the in-process search, its verified plans and the overall best (plan, result)."""
    procs = []
    if workers > 1 and "fork" in multiprocessing.get_all_start_methods():
        ctx = multiprocessing.get_context("fork")
        for variant in range(1, workers):
            try:
                recv, send = ctx.Pipe(duplex=False)
                proc = ctx.Process(target=_worker, args=(factory, scenario, kwargs, variant, send),
                                   daemon=True)
                proc.start()
                send.close()
            except OSError:
                break
            procs.append((variant, proc, recv))
    search = factory(scenario, **kwargs)
    verified = search.run()
    claims = []
    for variant, proc, recv in procs:
        try:
            if recv.poll(max(0.0, hard_stop - time.monotonic())):
                claims += [(score, variant, plan) for plan, score in recv.recv()]
        except (EOFError, OSError):
            pass
        finally:
            proc.terminate()
    best_plan, best_result = verified[0][0].plan(scenario.seed), verified[0][1]
    stats = {"workers": 1 + len(procs), "received": len({v for _, v, _ in claims}),
             "winner": 0, "scores": {0: round(best_result.score, 6)}}
    for score, variant, _ in claims:
        stats["scores"][variant] = round(max(stats["scores"].get(variant, score), score), 6)
    for score, variant, plan in sorted(claims, key=lambda c: -c[0]):
        if score <= best_result.score + 1e-9 or time.monotonic() >= hard_stop:
            break
        result = evaluate(scenario, plan)
        if result.valid:
            best_plan, best_result, stats["winner"] = plan, result, variant
            break
    for _, proc, _ in procs:
        proc.join(0.1)
    return search, verified, best_plan, best_result, stats
