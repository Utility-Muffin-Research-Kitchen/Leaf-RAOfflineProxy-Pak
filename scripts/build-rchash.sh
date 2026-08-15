#!/usr/bin/env bash
# Build libraproxy_rchash.so for aarch64 from locked rcheevos + libchdr source.
#
# Same discipline as the CPython runtime: every archive is verified against
# locks/runtime.lock.json before extraction, and the compile happens inside the
# pinned mlp1-toolchain. Output: build/mlp1/rchash/libraproxy_rchash.so
#
# libchdr is here because rcheevos ships no CHD support -- it exposes a
# pluggable rc_hash_cdreader_t and expects the host to supply one. Without it
# the four CD systems on MLP1 (Sega CD, PC Engine CD, PlayStation, Dreamcast)
# cannot be hashed at all.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
LOCK="${RUNTIME_LOCK:-$ROOT/locks/runtime.lock.json}"
SOURCES_DIR="${RUNTIME_SOURCES_DIR:-$ROOT/workdir/sources}"
OUT_DIR="${1:-$ROOT/build/mlp1/rchash}"
PYTHON="${PYTHON:-python3}"

lock_field() {
    "$PYTHON" - "$LOCK" "$1" "$2" <<'PY'
import json, sys
lock = json.load(open(sys.argv[1], encoding="utf-8"))
bucket, field = sys.argv[2], sys.argv[3]
for item in lock["source_inputs"]:
    if item.get("bucket") == bucket:
        print(item[field])
        break
else:
    raise SystemExit("runtime lock has no %s source input" % bucket)
PY
}

verify_archive() {
    local path="$1" expected="$2" label="$3" actual
    [ -f "$path" ] || { echo "missing $path (run scripts/fetch-sources.sh)" >&2; exit 1; }
    if command -v sha256sum >/dev/null 2>&1; then
        actual="$(sha256sum "$path" | awk '{print $1}')"
    else
        actual="$(shasum -a 256 "$path" | awk '{print $1}')"
    fi
    [ "$actual" = "$expected" ] || {
        echo "$label archive hash mismatch" >&2
        echo "expected $expected" >&2
        echo "actual   $actual" >&2
        exit 1
    }
}

rc_filename="$(lock_field rcheevos filename)"
rc_sha="$(lock_field rcheevos sha256)"
rc_version="$(lock_field rcheevos version)"
chd_filename="$(lock_field libchdr filename)"
chd_sha="$(lock_field libchdr sha256)"
chd_version="$(lock_field libchdr version)"

verify_archive "$SOURCES_DIR/$rc_filename" "$rc_sha" rcheevos
verify_archive "$SOURCES_DIR/$chd_filename" "$chd_sha" libchdr

IMAGE="${MLP1_TOOLCHAIN_IMAGE:-$("$PYTHON" -c 'import json,sys;print(json.load(open(sys.argv[1]))["mlp1_toolchain_image"])' "$ROOT/release-lock.json")}"
WORKSPACE_ROOT="$(CDPATH= cd -- "$ROOT/.." && pwd)"
REPO_REL="$(basename "$ROOT")"

rm -rf "$OUT_DIR"
rc_work="$OUT_DIR/work/rcheevos"
chd_work="$OUT_DIR/work/libchdr"
mkdir -p "$rc_work" "$chd_work"
tar xzf "$SOURCES_DIR/$rc_filename" -C "$rc_work" --strip-components=1
tar xzf "$SOURCES_DIR/$chd_filename" -C "$chd_work" --strip-components=1

# Run as the invoking user, the way Leaf-Itchio-Pak does. Docker Desktop on
# macOS maps bind-mount ownership to the host user, so a root container looks
# fine there; on Linux the mount keeps the container's uid, and every later
# host-side step -- copying the CA bundle in, packaging build/ up -- hits
# "Permission denied" on a tree it supposedly owns.
docker run --rm \
    --user "$(id -u):$(id -g)" \
    -v "$WORKSPACE_ROOT:/workspace" \
    -w "/workspace/$REPO_REL" \
    "$IMAGE" \
    sh -euc '
        CC=/opt/mlp1-toolchain/bin/aarch64-buildroot-linux-gnu-gcc
        RC=build/mlp1/rchash/work/rcheevos
        CHD=build/mlp1/rchash/work/libchdr
        LZMA=$(ls -d $CHD/deps/lzma-*/ | head -1)
        MINIZ=$(ls -d $CHD/deps/miniz-*/ | head -1)
        ZSTD=$(ls -d $CHD/deps/zstd-*/ | head -1)

        # 3DS support (hash_encrypted.c, aes.c) is excluded: no 3DS on MLP1.
        RC_SRC="$RC/src/rhash/hash.c $RC/src/rhash/hash_rom.c
                $RC/src/rhash/hash_zip.c $RC/src/rhash/hash_disc.c
                $RC/src/rhash/cdreader.c $RC/src/rhash/md5.c $RC/src/rc_compat.c"

        CHD_SRC="$(ls $CHD/src/*.c | tr "\n" " ")
                 $(ls $LZMA/src/*.c | tr "\n" " ")
                 $(ls $MINIZ/*.c | tr "\n" " ")"
        # zstd ships here as a single-file decompressor amalgamation.
        ZSTD_SRC="$ZSTD/zstddeclib.c"

        $CC -shared -fPIC -O2 -DNDEBUG -DRC_HASH_NO_ENCRYPTED -DZSTD_DISABLE_ASM \
            -ffunction-sections -fdata-sections -Wl,--gc-sections \
            -o build/mlp1/rchash/libraproxy_rchash.so \
            src/rchash/rchash_glue.c src/rchash/chd_cdreader.c \
            $RC_SRC $CHD_SRC $ZSTD_SRC \
            -I"$RC/include" -I"$RC/src" -I"$RC/src/rhash" \
            -I"$CHD/include" -I"$LZMA/include" -I"$MINIZ" -I"$ZSTD"
    '

so="$OUT_DIR/libraproxy_rchash.so"
[ -f "$so" ] || { echo "build produced no shared object" >&2; exit 1; }

# The ABI the pak binds, and the dependency boundary. Only libc is acceptable:
# anything else would have to be bundled or proven present on stock MLP1, the
# same rule the CPython runtime follows.
docker run --rm --user "$(id -u):$(id -g)" \
    -v "$WORKSPACE_ROOT:/workspace" -w "/workspace/$REPO_REL" "$IMAGE" \
    sh -euc '
        NM=/opt/mlp1-toolchain/bin/aarch64-buildroot-linux-gnu-nm
        READELF=/opt/mlp1-toolchain/bin/aarch64-buildroot-linux-gnu-readelf
        SO=build/mlp1/rchash/libraproxy_rchash.so
        missing=0
        for sym in raproxy_hash_file raproxy_hash_buffer raproxy_rchash_version; do
            $NM -D --defined-only "$SO" | grep -q " T $sym$" || {
                echo "missing exported symbol: $sym" >&2; missing=1; }
        done
        [ $missing -eq 0 ] || exit 1
        if $READELF -d "$SO" | grep NEEDED |
             grep -vE "libc\.so\.6|ld-linux-aarch64\.so\.1"; then
            echo "unexpected shared-library dependency" >&2
            exit 1
        fi
        # A shared object links happily with undefined symbols, so the checks
        # above would pass a build whose CHD codecs were never compiled in --
        # it would then fail at runtime on the first LZMA-compressed disc.
        # Anything not satisfied by libc is a missing source file.
        if $NM -D --undefined-only "$SO" |
             grep -vE "GLIBC|^ +w |_ITM_|__gmon_start__|__cxa_finalize"; then
            echo "unresolved symbols: a codec source set is missing" >&2
            exit 1
        fi
    '

rm -rf "$OUT_DIR/work"
echo "built $so ($(wc -c < "$so" | tr -d ' ') bytes, rcheevos $rc_version + libchdr $chd_version)"
