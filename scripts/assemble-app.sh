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

read -r archive_filename content_sha content_scope < <("$PYTHON" - "$UPSTREAM_LOCK" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fp:
    lock = json.load(fp)
archive = lock["archive"]
print(archive["filename"], archive["content_sha256"], archive["content_scope"])
PY
)

archive="$SOURCES_DIR/$archive_filename"
if [ ! -f "$archive" ]; then
  echo "missing upstream archive: $archive (run scripts/fetch-sources.sh)" >&2
  exit 1
fi

work="$(mktemp -d "${TMPDIR:-/tmp}/raop-assemble.XXXXXX")"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/src"
tar -xzf "$archive" -C "$work/src" --strip-components=1

# Authoritative input check. The commit-tarball byte receipt is not a gate
# because GitHub re-encodes tarballs; verify the extracted shipped subtree
# instead. Patches below also apply with -F 0, so a content change cannot slip
# through unnoticed.
actual_content_sha="$("$PYTHON" - "$work/src/$content_scope" <<'PY'
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
if [ "$actual_content_sha" != "$content_sha" ]; then
  echo "upstream content hash mismatch for $content_scope" >&2
  echo "expected $content_sha" >&2
  echo "actual   $actual_content_sha" >&2
  exit 1
fi

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

# Leaf patch series, applied over the locked upstream archive at the tarball
# root. Zero fuzz: a hunk that only applies by guessing context is an
# unreviewed rebase -- the default 2-line fuzz once placed the probe timeout
# where upstream had rewritten the function around it.
for patch_file in "$PATCHES_DIR"/*.patch; do
  [ -e "$patch_file" ] || continue
  patch -s -F 0 -d "$work/src" -p1 <"$patch_file"
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

# Every sibling module a shipped file imports must itself be shipped. The
# forbidden list only rejects modules we already know to prune, so it cannot
# see a module upstream invents between two tags: at v1.11.1, proxy_service
# began importing a new .boot, which is neither allow-listed nor forbidden.
# Byte-compilation does not resolve imports, so without this the pak assembles
# clean and dies with ImportError on the device the first time it starts.
shipped = {p.stem for p in package.glob("*.py")}

for path in sorted(package.glob("*.py")):
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            # `from . import a, b` carries the names in aliases, not in module.
            targets = [node.module] if node.module else [a.name for a in node.names]
            for target in targets:
                if target in forbidden_modules:
                    raise SystemExit(f"forbidden import in {path.name}: .{target}")
                if target not in shipped:
                    raise SystemExit(
                        f"unshipped import in {path.name}: .{target} is neither "
                        "allow-listed nor pruned -- decide which it is"
                    )
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in forbidden_modules:
                    raise SystemExit(f"forbidden import in {path.name}: {alias.name}")
    for name in forbidden_names:
        if name in text:
            raise SystemExit(f"forbidden symbol {name!r} in {path.name}")

# Distro detection and other programs' config readers (RetroArch, PPSSPP,
# Dolphin, batocera/knulli, ROCKNIX, spruce/Onion/Allium/muOS paths) are
# stripped from config.py by patches/app/config.py.patch. Checked here on the
# identifiers and string constants of every shipped module, not on comments,
# so a reader that upstream adds or renames fails the build instead of riding
# along unused -- or used: proxy_port()'s default once probed for spruce.
import re

reader_name = re.compile(
    r"^(running_on_\w+|spruce_\w+|_retroarch_cfg_lookup|detect_(retroarch_cfg|"
    r"batocera_conf|rocknix_system_cfg|darkos_retroarch32_cfg|ppsspp_ini|"
    r"dolphin_config_dir|dolphin_ini)|(DEFAULT_)?(ONION|ALLIUM|MUOS|BATOCERA|"
    r"KNULLI|ROCKNIX|DARKOS|SPRUCE)_\w+|SDCARD_RETROARCH_CFG_CANDIDATES|"
    r"MAGICX_MARKER|CPUINFO_PATH|OS_RELEASE_PATH)$"
)
reader_paths = ("/mnt/SDCARD", "/userdata/system", "/opt/muos", "/run/muos",
                "/storage/.config", "/home/ark", "retroarch.cfg", "ppsspp.ini",
                "batocera.conf", "knulli.conf", "/etc/os-release", "/proc/cpuinfo")
for path in sorted(package.glob("*.py")):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        names = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, ast.alias):
            names.append(node.name.split(".")[-1])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            hit = next((p for p in reader_paths if p in node.value), None)
            if hit:
                raise SystemExit(f"config reader path {hit!r} in {path.name}:{node.lineno}")
        for name in names:
            if reader_name.match(name):
                raise SystemExit(
                    f"config reader {name!r} in {path.name}:"
                    f"{getattr(node, 'lineno', '?')} (strip it in config.py.patch)"
                )

forcache = list(package.glob("**/__pycache__"))
import shutil
for cache in forcache:
    shutil.rmtree(cache)
print("assemble-qualify ok")
PY

echo "assembled $OUT_DIR/raofflineproxy"
