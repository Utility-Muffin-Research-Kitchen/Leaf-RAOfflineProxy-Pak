#!/usr/bin/env bash
# Build the RAOfflineProxy-owned private CPython runtime for MLP1.
#
# Recipe shape follows the proven PortMaster-mlp1 ui-runtime lane, with the
# Python version threaded through from locks/runtime.lock.json instead of a
# hardcoded 3.10, an RAOfflineProxy-owned prefix (/raofflineproxy), and no
# OpenSSL rpath. The runtime relocates at run time via PYTHONHOME; the CPython
# configure prefix is not an install contract.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
LOCK="${RUNTIME_LOCK:-$ROOT/locks/runtime.lock.json}"
RELEASE_LOCK="${RELEASE_LOCK:-$ROOT/release-lock.json}"
OUT_DIR="${1:-$ROOT/build/mlp1/runtime}"
SOURCES_DIR="${RUNTIME_SOURCES_DIR:-$ROOT/workdir/sources}"
PYTHON="${PYTHON:-python3}"
IMAGE="${MLP1_TOOLCHAIN_IMAGE:-$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["mlp1_toolchain_image"])' "$RELEASE_LOCK")}"

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing required tool: $1" >&2
    exit 1
  }
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

size_of() {
  if stat -f %z "$1" >/dev/null 2>&1; then
    stat -f %z "$1"
  else
    stat -c %s "$1"
  fi
}

default_jobs() {
  if [ -n "${BUILD_JOBS:-}" ]; then
    printf '%s\n' "$BUILD_JOBS"
    return
  fi
  # nproc first, and every probe tolerant of failure. Linux HAS sysctl but has
  # no hw.ncpu, so probing for the command and then trusting the query aborted
  # the entire build under set -e -- on the host every CI runner uses, while
  # working perfectly on the macOS dev machine.
  if command -v nproc >/dev/null 2>&1 && jobs_value="$(nproc 2>/dev/null)" &&
     [ -n "$jobs_value" ]; then
    printf '%s\n' "$jobs_value"
    return
  fi
  if command -v sysctl >/dev/null 2>&1 &&
     jobs_value="$(sysctl -n hw.ncpu 2>/dev/null)" && [ -n "$jobs_value" ]; then
    printf '%s\n' "$jobs_value"
    return
  fi
  printf '4\n'
}

container_main() {
  need python3
  need tar
  need make

  local out_dir="${OUT_DIR_IN_CONTAINER:?missing OUT_DIR_IN_CONTAINER}"
  local sources_dir="${SOURCES_DIR_IN_CONTAINER:?missing SOURCES_DIR_IN_CONTAINER}"
  local cpython_filename="${CPYTHON_FILENAME:?missing CPYTHON_FILENAME}"
  local cpython_mm="${CPYTHON_MM:?missing CPYTHON_MM}"
  local expected_sha="${CPYTHON_SHA256:?missing CPYTHON_SHA256}"
  local xz_filename="${XZ_FILENAME:?missing XZ_FILENAME}"
  local xz_expected_sha="${XZ_SHA256:?missing XZ_SHA256}"
  local jobs="${BUILD_JOBS:-$(default_jobs)}"
  local archive="$sources_dir/$cpython_filename"
  local xz_archive="$sources_dir/$xz_filename"
  local prefix="/raofflineproxy"

  if [ ! -f "$archive" ]; then
    echo "missing CPython source archive in container: $archive" >&2
    exit 1
  fi
  local actual_sha
  actual_sha="$(sha256_of "$archive")"
  if [ "$actual_sha" != "$expected_sha" ]; then
    echo "CPython source hash mismatch in container" >&2
    echo "expected $expected_sha" >&2
    echo "actual   $actual_sha" >&2
    exit 1
  fi
  if [ ! -f "$xz_archive" ]; then
    echo "missing XZ source archive in container: $xz_archive" >&2
    exit 1
  fi
  actual_sha="$(sha256_of "$xz_archive")"
  if [ "$actual_sha" != "$xz_expected_sha" ]; then
    echo "XZ source hash mismatch in container" >&2
    echo "expected $xz_expected_sha" >&2
    echo "actual   $actual_sha" >&2
    exit 1
  fi

  rm -rf "$out_dir/deps" "$out_dir/work" "$out_dir/root"
  mkdir -p "$out_dir/deps/work" "$out_dir/deps/root" "$out_dir/work" "$out_dir/root"
  tar -xf "$archive" -C "$out_dir/work"

  local src
  src="$(find "$out_dir/work" -maxdepth 1 -type d -name 'Python-*' | head -n 1)"
  if [ -z "$src" ] || [ ! -f "$src/configure" ]; then
    echo "CPython archive did not extract to Python-*/configure" >&2
    exit 1
  fi

  local build="$out_dir/work/build"
  mkdir -p "$build"

  local tool_cc="${CC:-aarch64-buildroot-linux-gnu-gcc}"
  local tool_cc_bin="${tool_cc%% *}"
  local sysroot="${SYSROOT:-$("$tool_cc_bin" -print-sysroot)}"
  export SYSROOT="$sysroot"
  export CC="$tool_cc --sysroot=$sysroot"
  export CXX="${CXX:-aarch64-buildroot-linux-gnu-g++} --sysroot=$sysroot"
  export AR="${AR:-aarch64-buildroot-linux-gnu-ar}"
  export RANLIB="${RANLIB:-aarch64-buildroot-linux-gnu-ranlib}"
  export READELF="${READELF:-aarch64-buildroot-linux-gnu-readelf}"
  export PKG_CONFIG_SYSROOT_DIR="$sysroot"
  export PKG_CONFIG_PATH="$sysroot/usr/lib/pkgconfig:$sysroot/usr/share/pkgconfig"
  export PKG_CONFIG_ALLOW_CROSS=1

  tar -xf "$xz_archive" -C "$out_dir/deps/work"
  local xz_src
  xz_src="$(find "$out_dir/deps/work" -maxdepth 1 -type d -name 'xz-*' | head -n 1)"
  if [ -z "$xz_src" ] || [ ! -f "$xz_src/configure" ]; then
    echo "XZ archive did not extract to xz-*/configure" >&2
    exit 1
  fi

  local deps_prefix="$out_dir/deps/root$prefix"
  (
    cd "$xz_src"
    CFLAGS="-fPIC ${CFLAGS:-}" ./configure \
      --host=aarch64-buildroot-linux-gnu \
      --prefix="$prefix" \
      --enable-shared \
      --disable-static \
      --disable-xz \
      --disable-xzdec \
      --disable-lzmadec \
      --disable-lzmainfo \
      --disable-scripts \
      --disable-doc \
      --disable-nls
    make -j "$jobs"
    make install DESTDIR="$out_dir/deps/root"
  ) 2>&1 | tee "$out_dir/liblzma-build.log"

  export CPPFLAGS="-I$deps_prefix/include -I$sysroot/usr/include ${CPPFLAGS:-}"
  export LDFLAGS="-L$deps_prefix/lib -L$sysroot/usr/lib -Wl,-rpath-link,$deps_prefix/lib -Wl,-rpath-link,$sysroot/usr/lib ${LDFLAGS:-}"
  export PKG_CONFIG_PATH="$deps_prefix/lib/pkgconfig:$PKG_CONFIG_PATH"

  (
    cd "$build"
    "$src/configure" \
      --prefix="$prefix" \
      --enable-shared \
      --with-openssl="$sysroot/usr" \
      --with-system-expat \
      --with-system-ffi \
      --without-ensurepip

    make -j "$jobs" all _PYTHON_HOST_PLATFORM=linux-aarch64

    export LD_LIBRARY_PATH="$build:$deps_prefix/lib"
    export PYTHONPATH="$build/build/lib.linux-aarch64-$cpython_mm"
    export PYTHONDONTWRITEBYTECODE=1

    # CPython names this binary python.exe only when configure detects a
    # case-insensitive filesystem, where a plain "python" would collide with
    # the Python/ directory. A bind mount from macOS APFS is case-insensitive
    # and a CI runner's ext4 is not, so the same container produces a
    # different name depending on the host the source tree came from.
    interpreter=./python.exe
    [ -x "$interpreter" ] || interpreter=./python
    [ -x "$interpreter" ] || {
      echo "no built interpreter: neither ./python.exe nor ./python" >&2
      exit 1
    }

    "$interpreter" - <<'PY'
import _lzma
import _posixsubprocess
import ctypes
import fcntl
import hashlib
import json
import lzma
import sqlite3
import ssl
import subprocess
import sys
import zlib

print(sys.version)
print(ssl.OPENSSL_VERSION)
print(sqlite3.sqlite_version)
print("build-imports-ok")
PY

    make install DESTDIR="$out_dir/root" ENSUREPIP=no _PYTHON_HOST_PLATFORM=linux-aarch64
  ) 2>&1 | tee "$out_dir/cpython-build.log"

  local runtime="$out_dir/root$prefix"
  local py_bin="$runtime/bin/python$cpython_mm"
  if [ ! -x "$py_bin" ]; then
    echo "install did not produce $py_bin" >&2
    exit 1
  fi
  if [ ! -e "$runtime/bin/python3" ]; then
    cp -f "$py_bin" "$runtime/bin/python3"
  fi
  if [ ! -e "$runtime/bin/python" ]; then
    cp -f "$py_bin" "$runtime/bin/python"
  fi

  # FAT32-safe: flatten every symlink into a plain copy.
  find "$runtime" -type l -print | while IFS= read -r link; do
    tmp="$link.leaf-copy"
    rm -rf "$tmp"
    if [ -f "$link" ]; then
      cp -pL "$link" "$tmp"
    elif [ -d "$link" ]; then
      mkdir -p "$tmp"
      cp -RpL "$link"/. "$tmp"/
    else
      rm -f "$link"
      continue
    fi
    rm -f "$link"
    mv "$tmp" "$link"
  done

  # Install-only runtime: no headers, static libs, test suites, ensurepip, or
  # bytecode caches. No custom stdlib import pruner for v1.
  rm -rf "$runtime/lib/python$cpython_mm/test" \
         "$runtime/lib/python$cpython_mm/idlelib" \
         "$runtime/lib/python$cpython_mm/tkinter" \
         "$runtime/lib/python$cpython_mm/ensurepip" \
         "$runtime/lib/python$cpython_mm/turtledemo" \
         "$runtime/share"
  find "$runtime/lib/python$cpython_mm" -type d \
    \( -name '__pycache__' -o -name test -o -name tests \) -print |
    while IFS= read -r dir; do
      rm -rf "$dir"
    done
  find "$runtime/lib/python$cpython_mm" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
  rm -f "$runtime/bin/2to3" "$runtime/bin/2to3-$cpython_mm" \
        "$runtime/bin/idle3" "$runtime/bin/idle3.$cpython_mm" \
        "$runtime/bin/pydoc3" "$runtime/bin/pydoc3.$cpython_mm" \
        "$runtime/bin/python-config" "$runtime/bin/python3-config" \
        "$runtime/bin/python$cpython_mm-config"
  rm -rf "$runtime/include" "$runtime/lib/pkgconfig" "$runtime/lib/python$cpython_mm/config-"*
  rm -f "$runtime/lib/libpython$cpython_mm.a"
  for lib in "$deps_prefix"/lib/liblzma.so*; do
    [ -e "$lib" ] || continue
    cp -pL "$lib" "$runtime/lib/$(basename "$lib")"
  done
  find "$runtime/lib/python$cpython_mm/lib-dynload" -maxdepth 1 -type f \
    \( -name '_test*.so' -o -name '_xx*.so' -o -name 'xx*.so' \) -delete

  local readelf_tool="${READELF:-aarch64-buildroot-linux-gnu-readelf}"
  local strip_tool="${STRIP:-aarch64-buildroot-linux-gnu-strip}"
  if command -v "$readelf_tool" >/dev/null 2>&1 && command -v "$strip_tool" >/dev/null 2>&1; then
    find "$runtime" -type f -print | while IFS= read -r file; do
      if "$readelf_tool" -h "$file" >/dev/null 2>&1; then
        "$strip_tool" --strip-unneeded "$file" 2>/dev/null || true
      fi
    done
  fi

  for module in _ssl _sqlite3 _ctypes _lzma _posixsubprocess zlib; do
    if ! ls "$runtime/lib/python$cpython_mm/lib-dynload/$module"*.so >/dev/null 2>&1; then
      echo "required CPython extension missing after install: $module" >&2
      exit 1
    fi
  done

  # Relocation proof: run strictly through PYTHONHOME, as the pak will.
  export LD_LIBRARY_PATH="$runtime/lib"
  export PYTHONHOME="$runtime"
  export PYTHONPATH="$runtime/lib/python$cpython_mm:$runtime/lib/python$cpython_mm/site-packages:$runtime/lib"
  export PYTHONDONTWRITEBYTECODE=1
  "$runtime/bin/python3" - <<'PY'
import _lzma
import _posixsubprocess
import ctypes
import fcntl
import hashlib
import json
import lzma
import sqlite3
import ssl
import subprocess
import sys
import zlib

print(sys.version)
print(ssl.OPENSSL_VERSION)
print(sqlite3.sqlite_version)
print("installed-imports-ok")
PY

  find "$runtime/lib/python$cpython_mm" -type d -name '__pycache__' -print |
    while IFS= read -r dir; do
      rm -rf "$dir"
    done
  find "$runtime/lib/python$cpython_mm" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
}

if [ "${1:-}" = "--container" ]; then
  container_main
  exit 0
fi

need "$PYTHON"
need curl
need docker
need zip

mkdir -p "$OUT_DIR" "$SOURCES_DIR"

"$ROOT/scripts/fetch-sources.sh" "$SOURCES_DIR"

# Resolve both source plans from the lock.
plan="$OUT_DIR/.runtime-plan.tsv"
"$PYTHON" - "$LOCK" >"$plan" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fp:
    lock = json.load(fp)

rows = {}
for item in lock.get("source_inputs", []):
    bucket = item.get("bucket")
    if bucket in {"cpython", "xz", "cacert"}:
        rows[bucket] = item

for bucket in ("cpython", "xz", "cacert"):
    item = rows.get(bucket)
    if item is None:
        raise SystemExit(f"runtime lock has no {bucket} input")
    print("|".join([
        bucket,
        item["version"],
        item.get("python_major_minor", "-"),
        item["filename"],
        str(item["size"]),
        item["sha256"],
        item["url"],
        item.get("license", ""),
    ]))
PY

cpython_version="" cpython_mm="" cpython_filename="" cpython_sha="" cacert_filename=""
xz_filename="" xz_sha=""
while IFS='|' read -r bucket version mm filename size sha url license; do
  case "$bucket" in
    cpython)
      cpython_version="$version"; cpython_mm="$mm"
      cpython_filename="$filename"; cpython_sha="$sha" ;;
    xz)
      xz_filename="$filename"; xz_sha="$sha" ;;
    cacert)
      cacert_filename="$filename" ;;
  esac
done <"$plan"

if [ -z "$cpython_mm" ]; then
  echo "runtime lock cpython input is missing python_major_minor" >&2
  exit 1
fi

workspace_root="$(cd "$ROOT/.." && pwd)"
case "$OUT_DIR" in
  "$workspace_root"/*) out_dir_container="/workspace/${OUT_DIR#"$workspace_root"/}" ;;
  *) echo "OUT_DIR must be under workspace root: $workspace_root" >&2; exit 1 ;;
esac
case "$SOURCES_DIR" in
  "$workspace_root"/*) sources_dir_container="/workspace/${SOURCES_DIR#"$workspace_root"/}" ;;
  *) echo "SOURCES_DIR must be under workspace root: $workspace_root" >&2; exit 1 ;;
esac
repo_container="/workspace/${ROOT#"$workspace_root"/}"

jobs="$(default_jobs)"
# Run as the invoking user, the way Leaf-Itchio-Pak does. Docker Desktop on
# macOS maps bind-mount ownership to the host user, so a root container looks
# fine there; on Linux the mount keeps the container's uid, and every later
# host-side step -- copying the CA bundle in, packaging build/ up -- hits
# "Permission denied" on a tree it supposedly owns.
docker run --rm \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -e OUT_DIR_IN_CONTAINER="$out_dir_container" \
  -e SOURCES_DIR_IN_CONTAINER="$sources_dir_container" \
  -e CPYTHON_FILENAME="$cpython_filename" \
  -e CPYTHON_MM="$cpython_mm" \
  -e CPYTHON_SHA256="$cpython_sha" \
  -e XZ_FILENAME="$xz_filename" \
  -e XZ_SHA256="$xz_sha" \
  -e BUILD_JOBS="$jobs" \
  -v "$workspace_root":/workspace \
  -w "$repo_container" \
  "$IMAGE" \
  bash scripts/build-runtime-cpython.sh --container

runtime="$OUT_DIR/root/raofflineproxy"
site="$runtime/lib/python$cpython_mm/site-packages"
mkdir -p "$site"

# CA bundle: explicit file selected via SSL_CERT_FILE in the pak environment.
cp "$SOURCES_DIR/$cacert_filename" "$runtime/ca-certificates.crt"

chmod 755 "$runtime/bin/python" "$runtime/bin/python3" "$runtime/bin/python$cpython_mm" 2>/dev/null || true

image_id="$(docker image inspect --format '{{.Id}}' "$IMAGE" 2>/dev/null || true)"
image_digest="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$IMAGE" 2>/dev/null | head -n 1 || true)"
runtime_manifest="$runtime/.leaf-runtime-manifest.json"
"$PYTHON" - "$LOCK" "$SOURCES_DIR" "$runtime_manifest" "$IMAGE" "$image_digest" "$image_id" <<'PY'
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

lock_path, sources_dir, manifest_path, image, image_digest, image_id = sys.argv[1:7]

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

with open(lock_path, "r", encoding="utf-8") as fp:
    lock = json.load(fp)

sources = []
for item in lock.get("source_inputs", []):
    path = os.path.join(sources_dir, item["filename"])
    sources.append({
        "bucket": item["bucket"],
        "type": item["type"],
        "project": item.get("project", ""),
        "version": item["version"],
        "filename": item["filename"],
        "size": os.path.getsize(path),
        "sha256": sha256(path),
        "license": item.get("license", ""),
        "url": item.get("url", ""),
        "production": True,
    })

manifest = {
    "schema": 1,
    "product": lock.get("product"),
    "kind": "cpython-runtime",
    "production": True,
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "target": lock.get("target", {}),
    "build": {
        "toolchain_image": image,
        "toolchain_image_digest": image_digest,
        "toolchain_image_id": image_id,
        "configure": lock["source_inputs"][0]["build"]["configure"],
        "make_platform": "linux-aarch64",
        "runtime_pruned": True,
    },
    "sources": sources,
}
with open(manifest_path, "w", encoding="utf-8") as fp:
    json.dump(manifest, fp, indent=2)
    fp.write("\n")
PY

python_tag="python$(printf '%s' "$cpython_mm" | tr -d .)"
artifact="$OUT_DIR/raofflineproxy-mlp1-runtime-$python_tag-aarch64-cpython-$cpython_version.zip"
manifest="$OUT_DIR/raofflineproxy-mlp1-runtime-$python_tag-aarch64-cpython-$cpython_version.json"
rm -f "$artifact" "$manifest"
(cd "$OUT_DIR/root" && zip -X -qr "$artifact" raofflineproxy)

"$PYTHON" - "$runtime_manifest" "$artifact" "$manifest" "$runtime" <<'PY'
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

runtime_manifest, artifact_path, manifest_path, runtime_root = sys.argv[1:5]

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

with open(runtime_manifest, "r", encoding="utf-8") as fp:
    manifest = json.load(fp)

file_count = 0
installed_size = 0
for dirpath, _, filenames in os.walk(runtime_root):
    for name in filenames:
        path = os.path.join(dirpath, name)
        file_count += 1
        installed_size += os.path.getsize(path)

manifest["artifact"] = {
    "path": artifact_path,
    "filename": os.path.basename(artifact_path),
    "size": os.path.getsize(artifact_path),
    "sha256": sha256(artifact_path),
    "installed_file_count": file_count,
    "installed_size": installed_size,
}
manifest["generated_at"] = datetime.now(timezone.utc).isoformat()

with open(manifest_path, "w", encoding="utf-8") as fp:
    json.dump(manifest, fp, indent=2)
    fp.write("\n")
PY

printf '%s  %s\n' "$(sha256_of "$artifact")" "$artifact"
echo "wrote $manifest"
