#!/bin/sh
# R5 hash agreement check, run ON the device.
#
# The one check that proves pre-caching will actually be used: a hash that
# disagrees with the one rcheevos computes inside RetroArch stores cache under a
# key nothing ever asks for, which looks exactly like success until the user is
# offline with nothing there.
#
# KNOWN_GOOD holds hashes RetroArch really sent through this proxy. Re-run this
# whenever the rcheevos pin or the RetroArch build moves.
#
#   sh scripts/device/r5-hash-agreement.sh
set -eu

P="${RAOP_PAK:-/media/sdcard1/Apps/mlp1/RAOfflineProxy.pak}"
DB="${LEAF_LIBRARY_DB:-/media/sdcard1/.umrk/mlp1/library.db}"
[ -d "$P" ] || { echo "pak not found: $P" >&2; exit 1; }

export PYTHONHOME="$P/runtime" PYTHONPATH="$P/app"
export LD_LIBRARY_PATH="$P/runtime/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export UMRK_RUNTIME_PATH="${UMRK_RUNTIME_PATH:-/tmp/jawaka-runtime}"

"$P/runtime/bin/python3" - "$DB" <<'PY'
import sqlite3, sys, time
from raofflineproxy.leaf_romhash import RomHasher, SYSTEM_CONSOLE_IDS

# Hashes RetroArch itself sent through the proxy, captured during R3 gameplay.
KNOWN_GOOD = [
    ("/mnt/sdcard/Roms/MD/Aladdin (USA).zip", "MD",
     "a6fc42bd5d19e36e5d8a2d8c4f4e9d23"),
    ("/mnt/sdcard/Roms/FC/Super Mario Bros. (World).zip", "FC",
     "8e3630186e35d477231bf8fd50e54cdd"),
]

hasher = RomHasher()
if not hasher.available:
    raise SystemExit("rc_hash unavailable: %s" % hasher.error)
print("rcheevos %s" % hasher.rcheevos_version)

failures = 0
print("\n-- agreement with RetroArch --")
for path, system, expected in KNOWN_GOOD:
    result = hasher.hash_rom(path, system)
    ok = result.hash == expected
    failures += 0 if ok else 1
    print("  %-4s %-9s %s" % (system, "MATCH" if ok else "MISMATCH", result.hash or result.error))

print("\n-- one ROM per system --")
db = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
supported = unsupported = 0
for system, path in db.execute(
        "SELECT system, MIN(rom_path) FROM games GROUP BY system ORDER BY system"):
    start = time.monotonic()
    result = hasher.hash_rom(path, system)
    elapsed = (time.monotonic() - start) * 1000
    if result.hash:
        supported += 1
        print("  %-10s %7.0f ms  %s" % (system, elapsed, result.hash))
    else:
        unsupported += 1
        print("  %-10s %7.0f ms  UNSUPPORTED  %s" % (system, elapsed, (result.error or "")[:52]))

print("\n  supported=%d unsupported=%d" % (supported, unsupported))
if failures:
    raise SystemExit("hash agreement FAILED: pre-cached entries would never be hit")
print("hash-agreement: ok")
PY
