#!/usr/bin/env bash
# Rebuild from the corresponding source alone and compare with this build.
#
# Extracts `make dist-source` output into a fresh directory with no git
# metadata and no other checkout, then:
#   - every locked input is present and verifies, with downloads disabled
#     (a curl that fails on use is first on PATH);
#   - the Catastrophe tree passes the same lock check a checkout does;
#   - the assembled app tree is byte-identical to this checkout's;
#   - with DIST_SOURCE_REBUILD=1 (CI sets it), both packages are rebuilt in
#     the pinned toolchain and their ZIPs must match this checkout's build.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
version="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["pak_version"])' "$ROOT/release-lock.json")"
archive="$ROOT/build/dist/raofflineproxy-$version-source.tar.gz"
[ -f "$archive" ] || { echo "missing $archive (make dist-source)" >&2; exit 1; }

# Under build/, not $TMPDIR: the rebuild bind-mounts this directory into the
# toolchain container, and build/ is a path Docker can always share.
check="$ROOT/build/dist-check"
rm -rf "$check"
mkdir -p "$check/bin"
trap 'rm -rf "$check"' EXIT
tar -xzf "$archive" -C "$check"
top="$check/raofflineproxy-$version-source"
repo="$top/Leaf-RAOfflineProxy-Pak"

cat >"$check/bin/curl" <<'EOF'
#!/bin/sh
echo "dist-source-test: network use attempted: curl $*" >&2
exit 97
EOF
chmod +x "$check/bin/curl"

[ ! -e "$repo/.git" ] || { echo "dist carries git metadata" >&2; exit 1; }
"$PYTHON" - "$top/corresponding-source.json" "$ROOT" <<'PY'
import json
import pathlib
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
root = pathlib.Path(sys.argv[2])
runtime = json.load(open(root / "locks/runtime.lock.json", encoding="utf-8"))
upstream = json.load(open(root / "locks/upstream.lock.json", encoding="utf-8"))
names = {item["filename"] for item in manifest["inputs"]}
wanted = {item["filename"] for item in runtime["source_inputs"]} | {upstream["archive"]["filename"]}
if names != wanted:
    raise SystemExit(f"dist inputs {sorted(names)} != locked inputs {sorted(wanted)}")
print(f"dist lists all {len(wanted)} locked inputs")
PY

PATH="$check/bin:$PATH" "$repo/scripts/fetch-sources.sh" >/dev/null
echo "locked inputs verify from the dist with downloads disabled"
"$repo/scripts/verify-catastrophe.sh" "$top/Catastrophe"

"$repo/scripts/assemble-app.sh" "$check/app" >/dev/null
diff -r "$check/app/raofflineproxy" "$ROOT/build/mlp1/app/raofflineproxy" >/dev/null || {
    echo "assembled app from the dist differs from this checkout's" >&2
    exit 1
}
echo "assembled app tree from the dist matches this checkout's"

if [ "${DIST_SOURCE_REBUILD:-0}" = 1 ]; then
    PATH="$check/bin:$PATH" make -C "$repo" package-mlp1 package-floor-mlp1 >"$check/rebuild.log" 2>&1 || {
        tail -40 "$check/rebuild.log" >&2
        exit 1
    }
    for zip in RAOfflineProxy.mlp1.pak.zip floor/RAOfflineProxy.mlp1.pak.zip; do
        a="$("$PYTHON" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$repo/build/mlp1/$zip")"
        b="$("$PYTHON" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$ROOT/build/mlp1/$zip")"
        [ "$a" = "$b" ] || { echo "rebuilt $zip $a != $b" >&2; exit 1; }
        echo "rebuilt $zip from the dist: $a (matches)"
    done
fi
echo "dist-source-test: ok"
