#!/usr/bin/env python3
"""One truthful version pair across the source, before anything is built.

The packager overwrites the generated manifest from release-lock.json, so a
disagreeing pak.json or floor/requirements never reaches the real ZIP. That is
exactly why drift survived here: 0.1.0 shipped with pak.json saying 0.11.0,
the floor's requirements saying 0.11.0, and the lock (and the published row)
saying 0.10.0. Every copy has to agree, or the next reader believes the wrong
one.

Checks:
- pak.json, release-lock.json and floor/requirements carry the same
  min_leaf_version / min_jawaka_version, and the same pak_version;
- the floor launcher hardcodes no version of its own;
- the catalog smoke reads its versions from the lock, not from literals;
- pak_version is not a version that is already tagged (a published row is
  immutable, so a changed build must carry a new version), unless this commit
  is the tagged one;
- floor_version is below pak_version.

Run: python3 scripts/version-metadata-test.py
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
VERSION = re.compile(r"\b\d+\.\d+\.\d+\b")
# Paths that cannot reach the package: Markdown, docs/, CI and host test scripts.
RELEASE_NEUTRAL = re.compile(r"(.*\.md|docs/.*|\.github/.*|scripts/[^/]*-test\.(py|sh))$")

failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        failures.append(what)


def parse(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


lock = json.loads((ROOT / "release-lock.json").read_text(encoding="utf-8"))
manifest = json.loads((ROOT / "pak.json").read_text(encoding="utf-8"))
floor_leaf = (ROOT / "floor" / "requirements" / "min-leaf-version").read_text().strip()
floor_jawaka = (ROOT / "floor" / "requirements" / "min-jawaka-version").read_text().strip()

check(manifest["pak_version"] == lock["pak_version"],
      f"pak.json pak_version {manifest['pak_version']} == lock {lock['pak_version']}")
for key in ("min_leaf_version", "min_jawaka_version"):
    check(manifest[key] == lock[key], f"pak.json {key} {manifest[key]} == lock {lock[key]}")
check(floor_leaf == lock["min_leaf_version"],
      f"floor/requirements/min-leaf-version {floor_leaf} == lock {lock['min_leaf_version']}")
check(floor_jawaka == lock["min_jawaka_version"],
      f"floor/requirements/min-jawaka-version {floor_jawaka} == lock {lock['min_jawaka_version']}")
check(parse(lock["floor_version"]) < parse(lock["pak_version"]),
      f"floor {lock['floor_version']} is below the real {lock['pak_version']}")

floor_launch = (ROOT / "floor" / "launch.sh").read_text(encoding="utf-8")
check(not VERSION.search(floor_launch), "floor/launch.sh hardcodes no version")

smoke = (ROOT / "scripts" / "catalog-selection-smoke.sh").read_text(encoding="utf-8")
literal_versions = [
    line.strip() for line in smoke.splitlines()
    if re.match(r"\s*(REAL|FLOOR)_VERSION=", line) and VERSION.search(line)
]
check(not literal_versions, f"catalog smoke reads its versions from the lock {literal_versions}")


def git(*args: str) -> str | None:
    try:
        return subprocess.run(["git", "-C", str(ROOT), *args], check=True,
                              capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None


tags = git("tag", "-l", "v*")
if tags is None:
    check(False, "git tags are readable (fetch tags: a published version must be known)")
else:
    tagged = {tag.strip() for tag in tags.splitlines() if tag.strip()}
    here = {tag.strip() for tag in (git("tag", "--points-at", "HEAD") or "").splitlines()}
    wanted = f"v{lock['pak_version']}"
    check("v0.1.0" in tagged, "the published v0.1.0 tag is visible (CI must fetch tags)")
    # A published version is immutable, so anything that can change its package
    # needs a new version. Files that never reach the ZIP can still follow the
    # release: documentation (the README records that it shipped), CI, and the
    # host tests, including this one.
    after_tag = []
    if wanted in tagged and wanted not in here:
        changed = git("diff", "--name-only", f"{wanted}^{{commit}}", "HEAD")
        after_tag = [path for path in (changed if changed is not None else "?").split()
                     if not RELEASE_NEUTRAL.match(path)]
    check(not after_tag,
          f"{wanted} is not already published from another commit "
          f"(only docs, CI and host tests may change after its tag) {after_tag[:5]}")

if failures:
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1)
print("\nversion-metadata-test: all checks passed")
