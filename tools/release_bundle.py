"""Build a validated, deterministic local release-review bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid
import zipfile


ROOT = Path(__file__).resolve().parents[1]
FIXED_ZIP_TIME = (2026, 10, 24, 0, 0, 0)
EXCLUDED_PARTS = {".git", "__pycache__", "build"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_files() -> list[Path]:
    files = []
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if not path.is_file() or EXCLUDED_PARTS.intersection(relative.parts):
            continue
        if any(part.endswith(".egg-info") for part in relative.parts) or path.suffix == ".pyc":
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(ROOT).as_posix())


def write_source_zip(destination: Path, version: str) -> None:
    prefix = f"zeostudio-tengate-v5-{version}/"
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in source_files():
            info = zipfile.ZipInfo(prefix + path.relative_to(ROOT).as_posix(), FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())


def write_sbom(destination: Path, version: str, source_archive: Path) -> None:
    checksums = [{"algorithm": "SHA256", "checksumValue": sha256(source_archive)}]
    document = {
        "SPDXID": "SPDXRef-DOCUMENT",
        "creationInfo": {"created": "2026-10-24T00:00:00Z", "creators": ["Organization: ZeoStudio"]},
        "dataLicense": "CC0-1.0",
        "documentNamespace": f"https://zeostudio.example/spdx/tengate-v5/{version}/{uuid.uuid5(uuid.NAMESPACE_URL, version)}",
        "name": f"ZeoStudio-TenGate-V5-{version}",
        "packages": [{
            "SPDXID": "SPDXRef-Package",
            "checksums": checksums,
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "Apache-2.0",
            "licenseDeclared": "Apache-2.0",
            "name": "zeostudio-tengate-v5",
            "supplier": "Organization: ZeoStudio",
            "versionInfo": version,
        }],
        "relationships": [{
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": "SPDXRef-Package",
        }],
        "spdxVersion": "SPDX-2.3",
    }
    destination.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skip-validation", action="store_true")
    args = parser.parse_args()
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    version = metadata["project"]["version"]

    if not args.skip_validation:
        subprocess.run([sys.executable, ROOT / "tools/release_validate.py", "--full"], cwd=ROOT, check=True)

    args.output.mkdir(parents=True, exist_ok=True)
    source_zip = args.output / f"zeostudio-tengate-v5-{version}-source.zip"
    write_source_zip(source_zip, version)
    with tempfile.TemporaryDirectory() as temporary:
        environment = os.environ.copy()
        environment["SOURCE_DATE_EPOCH"] = "1792800000"
        subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--wheel-dir", temporary, "."],
            cwd=ROOT,
            env=environment,
            check=True,
        )
        built = next(Path(temporary).glob("*.whl"))
        wheel = args.output / built.name
        shutil.copyfile(built, wheel)

    sbom = args.output / "SBOM.spdx.json"
    write_sbom(sbom, version, source_zip)
    artifacts = [sbom, source_zip, wheel]
    sums = args.output / "SHA256SUMS"
    sums.write_text("".join(f"{sha256(path)}  {path.name}\n" for path in sorted(artifacts)))
    manifest = {
        "artifacts": {path.name: sha256(path) for path in sorted([*artifacts, sums])},
        "schema": "tengate.release-bundle.v1",
        "status": "ready_for_review",
        "version": version,
    }
    (args.output / "RELEASE_MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
