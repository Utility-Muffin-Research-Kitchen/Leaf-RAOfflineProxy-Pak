#!/usr/bin/env bash
# Assemble the Leaf app tree: extract the lock-verified upstream archive,
# prune to the production module allow-list, apply the Leaf patch series, and
# overlay repo-owned modules. Output: build/mlp1/app/raofflineproxy.
#
# The result is re-derived from scratch on every run; build/ is never a
# source of truth.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
UPSTREAM_LOCK="${UPSTREAM_LOCK:-$ROOT/locks/upstream.lock.json}"
SOURCES_DIR="${RUNTIME_SOURCES_DIR:-$ROOT/workdir/sources}"
OUT_DIR="${1:-$ROOT/build/mlp1/app}"
PYTHON="${PYTHON:-python3}"
PATCHES_DIR="$ROOT/patches/app"

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

need "$PYTHON"
need tar
need patch

read -r archive_filename archive_sha < <("$PYTHON" - "$UPSTREAM_LOCK" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fp:
    lock = json.load(fp)
archive = lock["archive"]
print(archive["filename"], archive["sha256"])
PY
)

archive="$SOURCES_DIR/$archive_filename"
if [ ! -f "$archive" ]; then
  echo "missing upstream archive: $archive (run scripts/fetch-sources.sh)" >&2
  exit 1
fi
actual_sha="$(sha256_of "$archive")"
if [ "$actual_sha" != "$archive_sha" ]; then
  echo "upstream archive hash mismatch" >&2
  echo "expected $archive_sha" >&2
  echo "actual   $actual_sha" >&2
  exit 1
fi

work="$(mktemp -d "${TMPDIR:-/tmp}/raop-assemble.XXXXXX")"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/src"
tar -xzf "$archive" -C "$work/src" --strip-components=1

package_src="$work/src/linux/raofflineproxy"
if [ ! -d "$package_src" ]; then
  echo "archive does not contain linux/raofflineproxy" >&2
  exit 1
fi

# Production allow-list. Everything else from upstream (distro integrations,
# SDL menu, manual ROM caching, self-update, daemon launcher, PID machinery)
# stays out of the pak and out of the import graph.
allow_list="__init__.py
award_signing.py
cache_keys.py
config.py
es_export.py
flusher.py
image_cache.py
network.py
proxy_service.py
rom_cache.py
state.py
storage.py
storage_corruption.py
utils.py"

assemble_dir="$work/assemble/raofflineproxy"
mkdir -p "$assemble_dir"
for name in $allow_list; do
  if [ ! -f "$package_src/$name" ]; then
    echo "allow-listed module missing upstream: $name" >&2
    exit 1
  fi
  cp "$package_src/$name" "$assemble_dir/$name"
done

# Leaf patch series, applied over the upstream tag at the tarball root.
for patch_file in "$PATCHES_DIR"/*.patch; do
  [ -e "$patch_file" ] || continue
  patch -s -d "$work/src" -p1 <"$patch_file"
done

for name in $allow_list; do
  cp "$package_src/$name" "$assemble_dir/$name"
done

# Repo-owned modules (the supervised foreground entry point).
cp "$ROOT/src/raofflineproxy/"*.py "$assemble_dir/"

# Qualification: byte-compile everything, and prove no pruned module, banned
# symbol, or forbidden import path survives.
rm -rf "$OUT_DIR"
mkdir -p "$(dirname "$OUT_DIR")"
cp -R "$work/assemble" "$OUT_DIR"

"$PYTHON" - "$OUT_DIR/raofflineproxy" <<'PY'
import ast
import compileall
import pathlib
import sys

package = pathlib.Path(sys.argv[1])
if not compileall.compile_dir(package, quiet=1, force=True):
    raise SystemExit("byte-compilation failed")

forbidden_modules = {
    "auth", "service", "main", "menu_sdl", "menu_input", "ui", "rom_browser",
    "smart_cache", "rom_hashing", "native_codecs", "rvz_datasource", "update",
    "log_uploader", "lowerdeck", "platform", "retroarch_cfg", "ppsspp_cfg",
    "dolphin_cfg", "batocera_conf", "knulli_service", "pending_awards",
}
forbidden_names = {
    "resolve_credentials", "os._exit", "PeriodicRefresh",
    "ensure_ra_proxy_chained", "stop_ra_proxy_chain",
}
bad_files = [p for p in package.iterdir() if p.suffix == ".py" and p.stem in forbidden_modules]
if bad_files:
    raise SystemExit(f"forbidden module present: {[p.name for p in bad_files]}")

for path in sorted(package.glob("*.py")):
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module in forbidden_modules:
            raise SystemExit(f"forbidden import in {path.name}: .{node.module}")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in forbidden_modules:
                    raise SystemExit(f"forbidden import in {path.name}: {alias.name}")
    for name in forbidden_names:
        if name in text:
            raise SystemExit(f"forbidden symbol {name!r} in {path.name}")

forcache = list(package.glob("**/__pycache__"))
import shutil
for cache in forcache:
    shutil.rmtree(cache)
print("assemble-qualify ok")
PY

echo "assembled $OUT_DIR/raofflineproxy"
