from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="include reliability and failure benchmarks")
    args = parser.parse_args()
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    expected_version = pyproject["project"]["version"]
    commands = [
        ("version", [sys.executable, "-c", (
            "import tengate_v5,sys;sys.exit(0 if tengate_v5.__version__=="
            f"{expected_version!r} else 1)"
        )]),
        ("unit", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"]),
        ("quickstart", [sys.executable, "examples/quickstart.py"]),
        ("compile", [sys.executable, "-m", "compileall", "-q", "src", "tests", "examples", "tools"]),
        ("dependencies", [sys.executable, "-m", "pip", "check"]),
    ]
    if args.full:
        commands.extend((name, [sys.executable, path]) for name, path in (
            ("relay", "tools/relay_bench.py"),
            ("github_bridge", "tools/github_inbox_bench.py"),
            ("service", "tools/service_bench.py"),
            ("multiprocess", "tools/multiprocess_soak.py"),
            ("failure_injection", "tools/failure_injection.py"),
        ))
    results: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory() as pycache:
        environment = dict(os.environ)
        environment.update({
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": pycache,
        })
        for name, command in commands:
            completed = subprocess.run(
                command, cwd=ROOT, env=environment, text=True, capture_output=True, timeout=60
            )
            output = (completed.stdout + completed.stderr).strip().splitlines()
            results.append({
                "name": name,
                "status": "passed" if completed.returncode == 0 else "failed",
                "last_line": output[-1] if output else "",
            })
            if completed.returncode != 0:
                sys.stderr.write(completed.stdout)
                sys.stderr.write(completed.stderr)
                print(json.dumps({"schema": "tengate.release-validation.v1", "results": results}))
                return completed.returncode
    print(json.dumps({
        "full": args.full,
        "python": sys.version.split()[0],
        "results": results,
        "schema": "tengate.release-validation.v1",
        "status": "passed",
        "version": expected_version,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
