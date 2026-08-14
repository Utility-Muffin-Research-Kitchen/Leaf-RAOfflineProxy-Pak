#!/usr/bin/env python3
"""Assemble the inert RAOfflineProxy compatibility-floor pak."""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import zipfile


ROOT = Path(__file__).resolve().parents[1]
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def version(value: str) -> str:
    if not VERSION_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("must be an exact MAJOR.MINOR.PATCH")
    return value


def copy_file(source: Path, target: Path) -> None:
    if not source.is_file() or source.is_symlink():
        raise SystemExit(f"required regular file missing: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def write_zip(package_dir: Path, archive_path: Path) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.unlink(missing_ok=True)
    with zipfile.ZipFile(
        archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as output:
        for path in sorted(package_dir.rglob("*")):
            if not path.is_file():
                continue
            relative = PurePosixPath(package_dir.name) / path.relative_to(package_dir)
            mode = 0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644
            info = zipfile.ZipInfo(str(relative), date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.create_system = 3
            output.writestr(
                info,
                path.read_bytes(),
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pak-version", type=version, required=True)
    parser.add_argument("--min-leaf-version", type=version, required=True)
    parser.add_argument("--min-jawaka-version", type=version, required=True)
    args = parser.parse_args()

    lock = json.loads((ROOT / "release-lock.json").read_text(encoding="utf-8"))
    if lock["floor_version"] != args.pak_version:
        raise SystemExit("release-lock floor_version disagrees with package argument")

    floor_binary = ROOT / "build" / "mlp1" / "bin" / "raofflineproxy-floor"
    if not floor_binary.is_file():
        raise SystemExit(f"missing floor UI binary: {floor_binary}")

    package_dir = ROOT / "build" / "mlp1" / "floor" / "package" / "RAOfflineProxy.pak"
    archive_path = ROOT / "build" / "mlp1" / "floor" / "RAOfflineProxy.mlp1.pak.zip"
    shutil.rmtree(package_dir.parent, ignore_errors=True)
    (package_dir / "bin").mkdir(parents=True)
    (package_dir / "lib").mkdir()
    (package_dir / "requirements").mkdir()
    (package_dir / "licenses").mkdir()

    copy_file(floor_binary, package_dir / "bin" / floor_binary.name)
    copy_file(ROOT / "floor" / "launch.sh", package_dir / "launch.sh")
    copy_file(ROOT / "lib" / "leaf-version-gate.sh", package_dir / "lib" / "leaf-version-gate.sh")
    copy_file(ROOT / "floor" / "requirements" / "min-leaf-version", package_dir / "requirements" / "min-leaf-version")
    copy_file(ROOT / "floor" / "requirements" / "min-jawaka-version", package_dir / "requirements" / "min-jawaka-version")
    copy_file(ROOT / "locks" / "upstream.lock.json", package_dir / "licenses" / "upstream-lock.json")
    copy_file(ROOT / "locks" / "runtime.lock.json", package_dir / "licenses" / "runtime-lock.json")

    manifest = (ROOT / "floor" / "pak.json.in").read_text(encoding="utf-8")
    (package_dir / "pak.json").write_text(
        manifest.replace("@PAK_VERSION@", args.pak_version), encoding="utf-8"
    )
    for executable in (
        package_dir / "launch.sh",
        package_dir / "bin" / floor_binary.name,
        package_dir / "lib" / "leaf-version-gate.sh",
    ):
        executable.chmod(0o755)
    write_zip(package_dir, archive_path)
    installed = sum(path.stat().st_size for path in package_dir.rglob("*") if path.is_file())
    print(f"package={package_dir}")
    print(f"archive={archive_path}")
    print(f"installed_bytes={installed}")


if __name__ == "__main__":
    main()
