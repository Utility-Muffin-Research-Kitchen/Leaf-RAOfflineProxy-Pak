#!/usr/bin/env python3
"""Build a disposable two-version Pak Rat feed for R4 catalog qualification.

Renders pakrat.json.in twice -- once ungated for the inert floor, once gated
for the real package -- and merges them with Leaf's own local-feed generator so
the resulting storefront carries one stable app id whose legacy fields point at
the floor and whose descending versions[] is [real, floor]. That is the shape a
pre-gating client reads as "just the floor" and a gate-aware client reads as
"real if you are new enough, else floor".

Writes only under this repo's build/ and Leaf/build/pakrat-local. It never
touches leaf-docs or the production catalog; the production storefront is read
only to prove this app id is not already published.

  python3 scripts/build-catalog-fixture.py                 # disposable 99.99.99
  python3 scripts/build-catalog-fixture.py --min-leaf-version 0.10.0 --allow-real-minimum
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
APP_ID = "org.umrk.raofflineproxy"
INSTALL_NAME = "RAOfflineProxy.pak"
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


def version(value: str) -> str:
    if not VERSION_RE.fullmatch(value) or any(
        int(part) > 9999 for part in value.split(".")
    ):
        raise argparse.ArgumentTypeError("must be an exact MAJOR.MINOR.PATCH")
    return value


def run(command: list[str], cwd: Path) -> None:
    print(f"+ (cd {cwd} && {' '.join(command)})")
    subprocess.run(command, cwd=cwd, check=True)


def write_metadata(app_dir: Path, package_version: str, minimum: str | None) -> None:
    """Render pakrat.json.in. minimum=None produces the ungated floor entry."""
    template = (ROOT / "pakrat.json.in").read_text(encoding="utf-8")
    rendered = template.replace("@PAK_VERSION@", package_version).replace(
        "@MIN_LEAF_VERSION@", minimum or "0.0.0"
    )
    metadata = json.loads(rendered)
    package = metadata["leaf"]["packages"][0]
    if minimum is None:
        package.pop("min_leaf_version")
    (app_dir / "pakrat.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )


def align_runtime_manifest(package_dir: Path, package_version: str,
                           minimum: str | None) -> None:
    """Match the copied pak.json to the fixture's catalog entry.

    The feed generator refuses a package whose runtime min_leaf_version
    disagrees with the catalog's -- a check worth satisfying rather than
    bypassing, since a mismatch in production would gate the download one way
    and the runtime the other. Only the manifest differs between a real build
    and its fixture, so patching the copy avoids rebuilding an unchanged
    28 MB runtime (and needing Docker) just to move two strings.
    """
    manifest_path = package_dir / "pak.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pak_version"] = package_version
    if minimum is None:
        manifest.pop("min_leaf_version", None)
    else:
        manifest["min_leaf_version"] = minimum
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def generate(leaf_dir: Path, output: Path, app_dir: Path, base_url: str,
             history: Path | None = None) -> Path:
    shutil.rmtree(output, ignore_errors=True)
    command = [
        sys.executable, str(leaf_dir / "scripts" / "pakrat-local-feed.py"),
        "--output", str(output),
        "--base-url", base_url,
        "--app-dir", str(app_dir),
        "--skip-build",
    ]
    if history is not None:
        command += ["--history", str(history)]
    run(command, leaf_dir)
    return output / "pakrat" / "v1" / "storefront.json"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--floor-version", type=version, default="0.0.1")
    parser.add_argument("--real-version", type=version, default="0.1.0")
    parser.add_argument("--min-leaf-version", type=version, default="99.99.99")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765/pakrat/v1/")
    parser.add_argument(
        "--allow-real-minimum", action="store_true",
        help="permit a shippable minimum; the default guard forces a "
             "conspicuously disposable one so a fixture can never be mistaken "
             "for a release candidate",
    )
    args = parser.parse_args()

    if not args.allow_real_minimum and int(args.min_leaf_version.split(".")[0]) < 90:
        parser.error(
            "fixtures default to a disposable minimum >= 90.0.0; pass "
            "--allow-real-minimum to test the shippable floor deliberately"
        )
    if tuple(map(int, args.real_version.split("."))) <= tuple(
        map(int, args.floor_version.split("."))
    ):
        parser.error("real version must be newer than the floor")

    leaf_dir = WORKSPACE / "Leaf"
    if not (leaf_dir / "scripts" / "pakrat-local-feed.py").is_file():
        raise SystemExit(f"missing Leaf local-feed generator under {leaf_dir}")

    floor_package = ROOT / "build" / "mlp1" / "floor" / "package" / INSTALL_NAME
    real_package = ROOT / "build" / "mlp1" / "package" / INSTALL_NAME
    for path in (floor_package, real_package):
        if not path.is_dir():
            raise SystemExit(f"missing built package: {path}")

    fixture_root = ROOT / "build" / "catalog-fixture"
    app_dir = fixture_root / "app"
    package_dir = app_dir / "build" / "mlp1" / "package" / INSTALL_NAME
    shutil.rmtree(fixture_root, ignore_errors=True)
    package_dir.parent.mkdir(parents=True)

    # Pass 1: the ungated floor becomes the catalog's legacy/base entry.
    shutil.copytree(floor_package, package_dir)
    align_runtime_manifest(package_dir, args.floor_version, None)
    write_metadata(app_dir, args.floor_version, None)
    floor_catalog = generate(
        leaf_dir, leaf_dir / "build" / "pakrat-local" / "raop-floor",
        app_dir, args.base_url)

    # Pass 2: the gated real package, merged over the floor as history.
    shutil.rmtree(package_dir)
    shutil.copytree(real_package, package_dir)
    align_runtime_manifest(package_dir, args.real_version, args.min_leaf_version)
    write_metadata(app_dir, args.real_version, args.min_leaf_version)
    storefront_path = generate(
        leaf_dir, leaf_dir / "build" / "pakrat-local" / "raop",
        app_dir, args.base_url, floor_catalog)

    generated = json.loads(storefront_path.read_text(encoding="utf-8"))
    apps = [app for app in generated["apps"] if app.get("id") == APP_ID]
    if len(apps) != 1:
        raise SystemExit(f"feed did not contain exactly one {APP_ID} app")

    app = apps[0]
    package = app["packages"][0]
    versions = package["versions"]
    if [value["version"] for value in versions] != [args.real_version, args.floor_version]:
        raise SystemExit("versions[] is not strictly real-then-floor")
    if versions[0].get("min_leaf_version") != args.min_leaf_version:
        raise SystemExit("real version does not carry the declared minimum")
    if "min_leaf_version" in versions[1]:
        raise SystemExit("floor version is unexpectedly gated")
    if app["version"] != args.floor_version or package["version"] != args.floor_version:
        raise SystemExit("legacy fields are not pinned to the floor")

    # A pre-gating client reads only the legacy fields and never looks at
    # versions[], so the legacy artifact must BE the floor byte-for-byte. Name
    # equality is not enough -- both entries share a filename -- so compare the
    # digest against the floor entry in versions[].
    legacy_artifact = package.get("artifact") or {}
    floor_artifact = versions[1].get("artifact") or {}
    if not legacy_artifact.get("sha256"):
        raise SystemExit("legacy package has no artifact digest")
    if legacy_artifact.get("sha256") != floor_artifact.get("sha256"):
        raise SystemExit(
            "legacy artifact is not the floor build; a pre-gating client would "
            "download a package it cannot run"
        )
    if legacy_artifact.get("sha256") == versions[0].get("artifact", {}).get("sha256"):
        raise SystemExit("legacy artifact is the gated real build")

    baseline_path = (WORKSPACE / "leaf-docs" / "public" / "pakrat" / "v1"
                     / "storefront.json")
    if baseline_path.is_file():
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        if any(entry.get("id") == APP_ID for entry in baseline["apps"]):
            raise SystemExit(
                "production catalog already publishes this app id; this plan "
                "does not authorize a catalog entry"
            )
        print(f"production baseline clean ({len(baseline['apps'])} apps, no {APP_ID})")

    print(f"\nstorefront: {storefront_path}")
    print(f"  legacy/base version : {app['version']} (ungated floor)")
    for value in versions:
        gate = value.get("min_leaf_version", "ungated")
        print(f"  versions[] {value['version']:<8} min_leaf={gate}")
    print("catalog-fixture: ok")


if __name__ == "__main__":
    main()
