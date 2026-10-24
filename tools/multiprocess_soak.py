from __future__ import annotations

import json
import multiprocessing
import tempfile
import time
from pathlib import Path

from tengate_v5.errors import RevisionConflict
from tengate_v5.identity import HmacSealAuthority
from tengate_v5.kernel import GateKernel


PROCESSES = 8
TASKS = 200
KEY = b"multiprocess-soak-key-material-123456789"


def compete(database: str, process_id: int, start: object, results: object) -> None:
    authority = HmacSealAuthority(KEY, issuer="multiprocess-soak")
    governor = authority.issue("soak-governor", roles=frozenset({"governor"}))
    kernel = GateKernel(database, authority)
    committed = 0
    conflicts = 0
    errors: list[str] = []
    start.wait()
    try:
        for number in range(TASKS):
            try:
                kernel.transition(
                    f"TG-SOAK-{number}",
                    "assign",
                    governor,
                    expected_revision=0,
                    event_id=f"soak-{process_id}-{number}",
                    details={"lane": "soak-lane"},
                )
                committed += 1
            except RevisionConflict:
                conflicts += 1
            except Exception as exc:
                errors.append(type(exc).__name__)
    finally:
        kernel.close()
    results.put({"committed": committed, "conflicts": conflicts, "errors": errors})


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "multiprocess-soak.db"
        authority = HmacSealAuthority(KEY, issuer="multiprocess-soak")
        governor = authority.issue("soak-governor", roles=frozenset({"governor"}))
        setup = GateKernel(database, authority)
        for number in range(TASKS):
            setup.create_task(f"TG-SOAK-{number}", governor, event_id=f"create-{number}")
        setup.close()

        context = multiprocessing.get_context("spawn")
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(target=compete, args=(str(database), process_id, start, results))
            for process_id in range(PROCESSES)
        ]
        for process in processes:
            process.start()
        began = time.perf_counter()
        start.set()
        for process in processes:
            process.join(30)
            if process.is_alive():
                process.terminate()
                process.join()
                raise RuntimeError("multiprocess soak timed out")
            if process.exitcode != 0:
                raise RuntimeError(f"soak process exited with {process.exitcode}")
        elapsed = time.perf_counter() - began
        outcomes = [results.get(timeout=2) for _ in processes]
        committed = sum(item["committed"] for item in outcomes)
        conflicts = sum(item["conflicts"] for item in outcomes)
        errors = [error for item in outcomes for error in item["errors"]]

        verified = GateKernel(database, authority)
        valid = all(
            verified.get_task(f"TG-SOAK-{number}").revision == 1
            and verified.verify_chain(f"TG-SOAK-{number}")
            for number in range(TASKS)
        )
        verified.close()
        expected_conflicts = TASKS * (PROCESSES - 1)
        if committed != TASKS or conflicts != expected_conflicts or errors or not valid:
            raise RuntimeError("multiprocess soak invariant failed")
        print(json.dumps({
            "committed": committed,
            "conflicts": conflicts,
            "elapsed_ms": round(elapsed * 1000, 4),
            "errors": len(errors),
            "processes": PROCESSES,
            "schema": "tengate.multiprocess-soak.v1",
            "tasks": TASKS,
            "verified_chains": TASKS if valid else 0,
        }, sort_keys=True))


if __name__ == "__main__":
    main()
