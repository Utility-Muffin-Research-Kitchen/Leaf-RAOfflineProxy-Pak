#!/usr/bin/env bash
# Build libraproxy_rchash.so for aarch64 from the locked rcheevos source.
#
# Same discipline as the CPython runtime: the archive is verified against
# locks/runtime.lock.json before anything is extracted, and the compile happens
# inside the pinned mlp1-toolchain container. Output:
# build/mlp1/rchash/libraproxy_rchash.so
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
LOCK="${RUNTIME_LOCK:-$ROOT/locks/runtime.lock.json}"
SOURCES_DIR="${RUNTIME_SOURCES_DIR:-$ROOT/workdir/sources}"
OUT_DIR="${1:-$ROOT/build/mlp1/rchash}"
PYTHON="${PYTHON:-python3}"

read -r filename expected_sha version < <("$PYTHON" - "$LOCK" <<'PY'
import json, sys
lock = json.load(open(sys.argv[1], encoding="utf-8"))
for item in lock["source_inputs"]:
    if item.get("bucket") == "rcheevos":
        print(item["filename"], item["sha256"], item["version"])
        break
else:
    raise SystemExit("runtime lock has no rcheevos source input")
PY
)

archive="$SOURCES_DIR/$filename"
[ -f "$archive" ] || { echo "missing $archive (run scripts/fetch-sources.sh)" >&2; exit 1; }
if command -v sha256sum >/dev/null 2>&1; then
  actual="$(sha256sum "$archive" | awk '{print $1}')"
else
  actual="$(shasum -a 256 "$archive" | awk '{print $1}')"
fi
[ "$actual" = "$expected_sha" ] || {
  echo "rcheevos archive hash mismatch" >&2
  echo "expected $expected_sha" >&2
  echo "actual   $actual" >&2
  exit 1
}

IMAGE="${MLP1_TOOLCHAIN_IMAGE:-$("$PYTHON" -c 'import json,sys;print(json.load(open(sys.argv[1]))["mlp1_toolchain_image"])' "$ROOT/release-lock.json")}"
WORKSPACE_ROOT="$(CDPATH= cd -- "$ROOT/.." && pwd)"
REPO_REL="$(basename "$ROOT")"

work="$ROOT/build/mlp1/rchash/work"
rm -rf "$OUT_DIR"
mkdir -p "$work"
tar xzf "$archive" -C "$work" --strip-components=1

docker run --rm \
  -v "$WORKSPACE_ROOT:/workspace" \
  -w "/workspace/$REPO_REL" \
  "$IMAGE" \
  sh -euc '
    CC=/opt/mlp1-toolchain/bin/aarch64-buildroot-linux-gnu-gcc
    W=build/mlp1/rchash/work
    # 3DS support (hash_encrypted.c, aes.c) is excluded: MLP1 has no 3DS and it
    # pulls in AES for nothing.
    SRC="$W/src/rhash/hash.c $W/src/rhash/hash_rom.c $W/src/rhash/hash_zip.c
         $W/src/rhash/hash_disc.c $W/src/rhash/cdreader.c $W/src/rhash/md5.c
         $W/src/rc_compat.c"
    $CC -shared -fPIC -O2 -DNDEBUG -DRC_HASH_NO_ENCRYPTED \
        -ffunction-sections -fdata-sections -Wl,--gc-sections \
        -o build/mlp1/rchash/libraproxy_rchash.so \
        src/rchash/rchash_glue.c $SRC \
        -I"$W/include" -I"$W/src" -I"$W/src/rhash"
  '

so="$OUT_DIR/libraproxy_rchash.so"
[ -f "$so" ] || { echo "build produced no shared object" >&2; exit 1; }

# The ABI the pak's hashing module binds, and the dependency boundary. Only
# libc is acceptable: anything else would have to be bundled or proven present
# on stock MLP1, the same rule the CPython runtime follows.
NM=/opt/mlp1-toolchain/bin/aarch64-buildroot-linux-gnu-nm
READELF=/opt/mlp1-toolchain/bin/aarch64-buildroot-linux-gnu-readelf
docker run --rm -v "$WORKSPACE_ROOT:/workspace" -w "/workspace/$REPO_REL" "$IMAGE" \
  sh -euc "
    missing=0
    for sym in raproxy_hash_file raproxy_hash_buffer raproxy_rchash_version; do
      $NM -D --defined-only build/mlp1/rchash/libraproxy_rchash.so | grep -q \" T \$sym\$\" || {
        echo \"missing exported symbol: \$sym\" >&2; missing=1; }
    done
    [ \$missing -eq 0 ] || exit 1
    $READELF -d build/mlp1/rchash/libraproxy_rchash.so | grep NEEDED |
      grep -vE 'libc\.so\.6|ld-linux-aarch64\.so\.1' && {
        echo 'unexpected shared-library dependency' >&2; exit 1; }
    exit 0
  "

rm -rf "$work"
echo "built $so ($(wc -c < "$so" | tr -d ' ') bytes, rcheevos $version)"
