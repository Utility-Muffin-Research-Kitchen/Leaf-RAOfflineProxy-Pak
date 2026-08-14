#!/usr/bin/env python3
"""Assemble the real RAOfflineProxy MLP1 pak and deterministic ZIP."""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "build" / "mlp1" / "package" / "RAOfflineProxy.pak"
ARCHIVE_PATH = ROOT / "build" / "mlp1" / "RAOfflineProxy.mlp1.pak.zip"
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def version(value: str) -> str:
    if not VERSION_RE.fullmatch(value) or any(
        int(component) > 9999 for component in value.split(".")
    ):
        raise argparse.ArgumentTypeError("must be an exact MAJOR.MINOR.PATCH")
    return value


def copy_file(source: Path, target: Path) -> None:
    if not source.is_file() or source.is_symlink():
        raise SystemExit(f"required regular file missing: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def copy_tree(source: Path, target: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        raise SystemExit(f"required regular directory missing: {source}")
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        destination = target / relative
        if path.is_symlink():
            raise SystemExit(f"symlink is not FAT32-safe: {path}")
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            copy_file(path, destination)
        else:
            raise SystemExit(f"unsupported package entry: {path}")


def copy_tar_member(archive_path: Path, suffix: str, target: Path) -> None:
    with tarfile.open(archive_path, "r:*") as archive:
        matches = [member for member in archive.getmembers() if member.name.endswith(suffix)]
        if len(matches) != 1 or not matches[0].isreg():
            raise SystemExit(f"expected one regular {suffix} member in {archive_path}")
        source = archive.extractfile(matches[0])
        if source is None:
            raise SystemExit(f"could not read {matches[0].name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with source, target.open("wb") as output:
            shutil.copyfileobj(source, output)


def assert_fat32_safe(package_dir: Path) -> None:
    seen: dict[str, str] = {}
    for path in package_dir.rglob("*"):
        if path.is_symlink():
            raise SystemExit(f"built pak contains symlink: {path}")
        if not path.is_file():
            continue
        relative = PurePosixPath(path.relative_to(package_dir).as_posix())
        folded = str(relative).casefold()
        previous = seen.get(folded)
        if previous is not None and previous != str(relative):
            raise SystemExit(f"case-colliding package names: {previous}, {relative}")
        seen[folded] = str(relative)


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
    runtime_lock = json.loads(
        (ROOT / "locks" / "runtime.lock.json").read_text(encoding="utf-8")
    )
    upstream_lock = json.loads(
        (ROOT / "locks" / "upstream.lock.json").read_text(encoding="utf-8")
    )
    if lock["pak_version"] != args.pak_version:
        raise SystemExit("release-lock pak_version disagrees with package argument")
    if lock["min_leaf_version"] != args.min_leaf_version:
        raise SystemExit("release-lock min_leaf_version disagrees with package argument")
    if lock["min_jawaka_version"] != args.min_jawaka_version:
        raise SystemExit("release-lock min_jawaka_version disagrees with package argument")

    app_source = ROOT / "build" / "mlp1" / "app" / "raofflineproxy"
    runtime_source = ROOT / "build" / "mlp1" / "runtime" / "root" / "raofflineproxy"
    ui = ROOT / "build" / "mlp1" / "bin" / "raofflineproxy-ui"
    floor = ROOT / "build" / "mlp1" / "bin" / "raofflineproxy-floor"
    for required in (app_source, runtime_source, ui, floor):
        if not required.exists():
            raise SystemExit(f"missing assembled package input: {required}")

    shutil.rmtree(PACKAGE_DIR.parent, ignore_errors=True)
    (PACKAGE_DIR / "bin").mkdir(parents=True)
    (PACKAGE_DIR / "app").mkdir()
    (PACKAGE_DIR / "runtime").mkdir()
    (PACKAGE_DIR / "lib").mkdir()
    (PACKAGE_DIR / "res").mkdir()
    (PACKAGE_DIR / "licenses").mkdir()

    copy_file(ROOT / "launch.sh", PACKAGE_DIR / "launch.sh")
    copy_file(ROOT / "src" / "service-run", PACKAGE_DIR / "bin" / "service-run")
    copy_file(ui, PACKAGE_DIR / "bin" / ui.name)
    copy_file(floor, PACKAGE_DIR / "bin" / floor.name)
    copy_tree(app_source, PACKAGE_DIR / "app" / "raofflineproxy")
    copy_tree(runtime_source, PACKAGE_DIR / "runtime")
    copy_file(ROOT / "lib" / "leaf-version-gate.sh", PACKAGE_DIR / "lib" / "leaf-version-gate.sh")
    copy_file(ROOT / "res" / "icon.png", PACKAGE_DIR / "res" / "icon.png")

    manifest = json.loads((ROOT / "pak.json").read_text(encoding="utf-8"))
    manifest["pak_version"] = args.pak_version
    manifest["min_leaf_version"] = args.min_leaf_version
    manifest["min_jawaka_version"] = args.min_jawaka_version
    (PACKAGE_DIR / "pak.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    upstream_archive = ROOT / "workdir" / "sources" / upstream_lock["archive"]["filename"]
    copy_tar_member(upstream_archive, "/LICENSE", PACKAGE_DIR / "licenses" / "RAOfflineProxy-GPL-3.0.txt")
    copy_tar_member(
        ROOT / "workdir" / "sources" / "Python-3.13.15.tar.xz",
        "/LICENSE",
        PACKAGE_DIR / "licenses" / "Python-PSF-2.0.txt",
    )
    copy_tar_member(
        ROOT / "workdir" / "sources" / "xz-5.8.2.tar.xz",
        "/COPYING.0BSD",
        PACKAGE_DIR / "licenses" / "XZ-0BSD.txt",
    )
    copy_file(
        ROOT / "workdir" / "sources" / "cacert.pem",
        PACKAGE_DIR / "licenses" / "CA-cacert.pem",
    )
    copy_file(ROOT / "release-lock.json", PACKAGE_DIR / "licenses" / "release-lock.json")
    copy_file(ROOT / "locks" / "runtime.lock.json", PACKAGE_DIR / "licenses" / "runtime-lock.json")
    copy_file(ROOT / "locks" / "upstream.lock.json", PACKAGE_DIR / "licenses" / "upstream-lock.json")
    copy_file(ROOT / "Catastrophe-LICENSE.txt", PACKAGE_DIR / "licenses" / "Catastrophe-LICENSE.txt")
    # The pak's own terms travel with it: GPL-3.0 obliges us to convey the
    # license with the binary, and NOTICE is where the source offer and the
    # third-party inventory live.
    copy_file(ROOT / "LICENSE", PACKAGE_DIR / "licenses" / "LICENSE.txt")
    copy_file(ROOT / "NOTICE", PACKAGE_DIR / "licenses" / "NOTICE.txt")

    for executable in (
        PACKAGE_DIR / "launch.sh",
        PACKAGE_DIR / "bin" / "service-run",
        PACKAGE_DIR / "bin" / "raofflineproxy-ui",
        PACKAGE_DIR / "bin" / "raofflineproxy-floor",
        PACKAGE_DIR / "lib" / "leaf-version-gate.sh",
    ):
        executable.chmod(0o755)

    assert_fat32_safe(PACKAGE_DIR)
    write_zip(PACKAGE_DIR, ARCHIVE_PATH)
    installed = sum(path.stat().st_size for path in PACKAGE_DIR.rglob("*") if path.is_file())
    print(f"package={PACKAGE_DIR}")
    print(f"archive={ARCHIVE_PATH}")
    print(f"installed_bytes={installed}")


if __name__ == "__main__":
    main()
