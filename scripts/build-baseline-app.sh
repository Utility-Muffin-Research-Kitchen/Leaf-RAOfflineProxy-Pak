#!/usr/bin/env bash
# Re-derive the published 0.1.0 app tree, for the state upgrade/rollback test.
#
# The baseline is the tagged release, assembled by the tag's own
# scripts/assemble-app.sh from the tag's own upstream lock and patches -- not a
# copy kept in this tree, which could drift from what shipped. Output:
# build/baseline-<version>/app/raofflineproxy.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
LOCK="$ROOT/locks/state-baseline.lock.json"
SOURCES_DIR="${RUNTIME_SOURCES_DIR:-$ROOT/workdir/sources}"
PYTHON="${PYTHON:-python3}"

field() {
    "$PYTHON" -c 'import json, sys
value = json.load(open(sys.argv[1]))
for key in sys.argv[2].split("."):
    value = value[key]
print(value)' "$LOCK" "$1"
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

version="$(field pak_version)"
tag="$(field tag)"
commit="$(field commit)"
url="$(field upstream.url)"
filename="$(field upstream.filename)"
receipt="$(field upstream.sha256)"
content_sha="$(field upstream.content_sha256)"
content_scope="$(field upstream.content_scope)"
out="$ROOT/build/baseline-$version"

actual_commit="$(git -C "$ROOT" rev-parse --verify --quiet "$tag^{commit}" || true)"
if [ -z "$actual_commit" ]; then
    echo "tag $tag is not in this clone; fetch tags (git fetch --tags)" >&2
    exit 1
fi
if [ "$actual_commit" != "$commit" ]; then
    echo "tag $tag resolves to $actual_commit, the lock says $commit" >&2
    exit 1
fi

mkdir -p "$SOURCES_DIR"
archive="$SOURCES_DIR/$filename"
if [ ! -f "$archive" ]; then
    curl -fsSL --retry 3 -o "$archive.part" "$url"
    mv "$archive.part" "$archive"
fi

work="$(mktemp -d "${TMPDIR:-/tmp}/raop-baseline.XXXXXX")"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/tag" "$work/upstream"
git -C "$ROOT" archive "$commit" | tar -x -C "$work/tag"
tar -xzf "$archive" -C "$work/upstream" --strip-components=1

actual_content="$("$PYTHON" - "$work/upstream/$content_scope" <<'PY'
import hashlib
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
digest = hashlib.sha256()
for path in sorted(p for p in root.rglob("*") if p.is_file()):
    digest.update(path.relative_to(root).as_posix().encode() + b"\0")
    digest.update(hashlib.sha256(path.read_bytes()).digest())
print(digest.hexdigest())
PY
)"
if [ "$actual_content" != "$content_sha" ]; then
    echo "baseline upstream content mismatch for $content_scope" >&2
    echo "expected $content_sha" >&2
    echo "actual   $actual_content" >&2
    exit 1
fi

# The tag's assembly checks the archive's byte hash. The content was just
# verified, so a re-encoded tarball only needs its measured hash substituted;
# nothing else about the tag's inputs changes.
upstream_lock="$work/tag/locks/upstream.lock.json"
actual_receipt="$(sha256_of "$archive")"
if [ "$actual_receipt" != "$receipt" ]; then
    echo "note: baseline tarball re-encoded ($actual_receipt); content verified" >&2
    "$PYTHON" - "$upstream_lock" "$actual_receipt" <<'PY'
import json
import sys

path, digest = sys.argv[1], sys.argv[2]
lock = json.load(open(path, encoding="utf-8"))
lock["archive"]["sha256"] = digest
open(path, "w", encoding="utf-8").write(json.dumps(lock, indent=2) + "\n")
PY
fi

rm -rf "$out"
RUNTIME_SOURCES_DIR="$SOURCES_DIR" UPSTREAM_LOCK="$upstream_lock" PYTHON="$PYTHON" \
    bash "$work/tag/scripts/assemble-app.sh" "$out/app" >/dev/null

# Optional, and run in CI: prove the re-derived tree is the code that shipped,
# byte for byte, against the published release asset.
if [ -n "${BASELINE_PUBLISHED_ZIP:-}" ]; then
    "$PYTHON" - "$BASELINE_PUBLISHED_ZIP" "$(field published_zip_sha256)" "$out/app/raofflineproxy" <<'PY'
import hashlib
import pathlib
import sys
import zipfile

archive, expected, tree = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3])
digest = hashlib.sha256(pathlib.Path(archive).read_bytes()).hexdigest()
if digest != expected:
    raise SystemExit(f"published ZIP is {digest}, the lock says {expected}")
prefix = "RAOfflineProxy.pak/app/raofflineproxy/"
with zipfile.ZipFile(archive) as source:
    shipped = {name[len(prefix):]: source.read(name) for name in source.namelist()
               if name.startswith(prefix)}
built = {p.relative_to(tree).as_posix(): p.read_bytes() for p in tree.rglob("*") if p.is_file()}
if shipped != built:
    differ = sorted(set(shipped) ^ set(built) | {k for k in shipped.keys() & built.keys()
                                                   if shipped[k] != built[k]})
    raise SystemExit(f"baseline differs from the published app tree: {differ}")
print(f"baseline matches the published ZIP's app tree ({len(built)} files)")
PY
fi
echo "baseline $tag ($commit) -> $out/app/raofflineproxy"
