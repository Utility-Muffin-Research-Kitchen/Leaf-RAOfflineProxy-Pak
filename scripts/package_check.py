#!/usr/bin/env python3
"""Structural checks for the real and floor RAOfflineProxy paks."""

from __future__ import annotations

import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT / "build" / "mlp1" / "package" / "RAOfflineProxy.pak"
FLOOR = ROOT / "build" / "mlp1" / "floor" / "package" / "RAOfflineProxy.pak"


def files(package: Path) -> list[Path]:
    result = []
    folded: dict[str, str] = {}
    for path in package.rglob("*"):
        if path.is_symlink():
            raise SystemExit(f"symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(package).as_posix()
        key = relative.casefold()
        if key in folded and folded[key] != relative:
            raise SystemExit(f"case collision: {folded[key]} / {relative}")
        folded[key] = relative
        result.append(path)
    return result


def zip_entries(archive: Path) -> list[str]:
    with zipfile.ZipFile(archive) as source:
        names = source.namelist()
        if any(not name.startswith("RAOfflineProxy.pak/") for name in names):
            raise SystemExit(f"zip has an unsafe root: {archive}")
        if any(name.endswith("/") for name in names):
            raise SystemExit(f"zip has directory records: {archive}")
        for info in source.infolist():
            mode = info.external_attr >> 16
            if mode and (mode & 0o170000) != 0o100000:
                raise SystemExit(f"zip entry is not a regular file: {info.filename}")
        return names


def main() -> None:
    if not REAL.is_dir() or not FLOOR.is_dir():
        raise SystemExit("real or floor package directory missing")
    real_manifest = json.loads((REAL / "pak.json").read_text(encoding="utf-8"))
    floor_manifest = json.loads((FLOOR / "pak.json").read_text(encoding="utf-8"))
    if real_manifest.get("id") != "org.umrk.raofflineproxy":
        raise SystemExit("real package id mismatch")
    if real_manifest.get("pak_version") != "0.1.0":
        raise SystemExit("real package version mismatch")
    if real_manifest.get("min_leaf_version") != "0.11.0" or real_manifest.get("min_jawaka_version") != "0.11.0":
        raise SystemExit("real package version-floor pair mismatch")
    service = real_manifest.get("service") or {}
    if service.get("default_enabled") is not False or service.get("run", {}).get("path") != "bin/service-run":
        raise SystemExit("service manifest is not disabled/foreground")
    if service.get("lifecycle", {}).get("game") != "ignore" or service.get("lifecycle", {}).get("stop_on_storage_change") is not False:
        raise SystemExit("service lifecycle policy mismatch")
    if floor_manifest != {
        "id": "org.umrk.raofflineproxy",
        "name": "RAOfflineProxy",
        "platform": "mlp1",
        "pak_version": "0.0.1",
        "author": "Utility Muffin Research Kitchen",
        "repo_url": "https://github.com/Utility-Muffin-Research-Kitchen/Leaf-RAOfflineProxy-Pak",
        "description": "Compatibility notice for Leaf releases that cannot run RAOfflineProxy.",
    }:
        raise SystemExit("floor manifest is not inert or stable-id compatible")

    real_files = files(REAL)
    floor_files = files(FLOOR)
    required_real = {
        "launch.sh",
        "pak.json",
        "bin/service-run",
        "bin/raofflineproxy-ui",
        "bin/raofflineproxy-floor",
        "lib/leaf-version-gate.sh",
        "app/raofflineproxy/leaf_service.py",
        "runtime/bin/python3",
        "runtime/ca-certificates.crt",
        "res/icon.png",
    }
    actual_real = {path.relative_to(REAL).as_posix() for path in real_files}
    if not required_real <= actual_real:
        raise SystemExit(f"real package missing: {sorted(required_real - actual_real)}")
    if any(path.relative_to(REAL).as_posix().startswith("app/raofflineproxy/") and path.name in {
        "auth.py", "main.py", "service.py", "menu_sdl.py", "rom_browser.py", "update.py", "log_uploader.py", "lowerdeck.py"
    } for path in real_files):
        raise SystemExit("excluded upstream module entered the package")
    if any("/Volumes/" in path.read_text(encoding="utf-8", errors="ignore") for path in real_files if path.suffix in {".py", ".sh", ".json", ".txt"}):
        raise SystemExit("host path entered package text")
    if not {"launch.sh", "pak.json", "lib/leaf-version-gate.sh", "bin/raofflineproxy-floor"} <= {
        path.relative_to(FLOOR).as_posix() for path in floor_files
    }:
        raise SystemExit("floor package is incomplete")

    real_zip = ROOT / "build" / "mlp1" / "RAOfflineProxy.mlp1.pak.zip"
    floor_zip = ROOT / "build" / "mlp1" / "floor" / "RAOfflineProxy.mlp1.pak.zip"
    zip_entries(real_zip)
    zip_entries(floor_zip)
    print("package-check: ok")


if __name__ == "__main__":
    main()
