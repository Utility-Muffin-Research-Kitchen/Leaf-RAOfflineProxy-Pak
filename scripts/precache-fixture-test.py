#!/usr/bin/env python3
"""Host fixtures for the library reader and pre-cache outcome reporting.

- Primary-card rows are stored relative to the launcher card and resolve
  against SDCARD_PATH (or the card library.db lives on); secondary-card rows
  are absolute and stay as they are.
- A missing ROM, or a .gdi/.cue whose track file is gone, reports "missing",
  never "Not supported" and never success.
- A present disc that cannot be hashed stays "unsupported".
- A game RetroAchievements has no data for (404 not_found, or Success with
  GameId 0) is "unsupported", stores nothing a launch would read, and on the
  next run is reported again without a request -- never "already cached".
  No network, a transient failure and a rejected token stay failures.

Run: python3 scripts/precache-fixture-test.py (after scripts/assemble-app.sh)
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
import sys
import tempfile
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "build" / "mlp1" / "app" / "raofflineproxy"

if not APP.is_dir():
    sys.exit(f"assembled app missing: {APP} (run scripts/assemble-app.sh)")

WORK = pathlib.Path(tempfile.mkdtemp(prefix="raop-precache-"))
os.environ["RAOFFLINEPROXY_CONFIG_DIR"] = str(WORK / "config")
os.environ.pop("SDCARD_PATH", None)
sys.path.insert(0, str(APP.parent))

from raofflineproxy import leaf_precache  # noqa: E402
from raofflineproxy.leaf_library import LibraryGame, LibraryReader  # noqa: E402
from raofflineproxy.leaf_romhash import HashResult  # noqa: E402

failures = []


def check(ok: bool, what: str) -> None:
    if not ok:
        failures.append(what)
        print(f"FAIL {what}")
    else:
        print(f"ok   {what}")


def touch(path: pathlib.Path, text: str = "x") -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# A launcher card with a library, and a second card holding one game.
card = WORK / "primary"
other = WORK / "secondary"
dc = card / "Roms" / "DC"
touch(dc / "Primary.gdi", '3\n1 0 4 2352 "Primary (Track 1).bin" 0\n'
      '2 600 0 2352 "Primary (Track 2).raw" 0\n3 45000 4 2352 "Primary (Track 3).bin" 0\n')
for n, ext in ((1, "bin"), (2, "raw"), (3, "bin")):
    touch(dc / f"Primary (Track {n}).{ext}")
touch(dc / "Broken.gdi", "3\n1 0 4 2352 Broken01.bin 0\n2 600 0 2352 Broken02.raw 0\n"
      "3 45000 4 2352 Broken03.bin 0\n")
touch(dc / "Broken01.bin")
touch(dc / "Broken03.bin")
scd = card / "Roms" / "SEGACD"
touch(scd / "Game.cue", 'FILE "Game (Track 1).bin" BINARY\n  TRACK 01 MODE1/2352\n'
      '    INDEX 01 00:00:00\n')
touch(scd / "Game (Track 1).bin")
touch(scd / "NoBin.cue", 'FILE "NoBin.bin" BINARY\n  TRACK 01 MODE1/2352\n')
secondary_rom = touch(other / "Roms" / "DC" / "Secondary.chd")

db_path = card / ".umrk" / "mlp1" / "library.db"
db_path.parent.mkdir(parents=True)
db = sqlite3.connect(db_path)
db.executescript(
    """
    CREATE TABLE games (id INTEGER PRIMARY KEY, name TEXT, system TEXT, rom_path TEXT);
    CREATE TABLE recents (kind TEXT, target_id INTEGER, last_opened INTEGER);
    CREATE TABLE favorites (kind TEXT, target_id INTEGER);
    CREATE TABLE system_settings (system TEXT, key TEXT, value TEXT);
    PRAGMA user_version = 6;
    """
)
db.executemany("INSERT INTO games VALUES (?, ?, ?, ?)", [
    (1, "Primary", "DC", "Roms/DC/Primary.gdi"),
    (2, "Secondary", "DC", str(secondary_rom)),
    (3, "Broken", "DC", "Roms/DC/Broken.gdi"),
    (4, "Game", "SEGACD", "Roms/SEGACD/Game.cue"),
    (5, "NoBin", "SEGACD", "Roms/SEGACD/NoBin.cue"),
    (6, "Gone", "DC", "Roms/DC/Gone.chd"),
])
db.executemany("INSERT INTO recents VALUES ('game', ?, ?)", [(1, 200), (3, 100)])
db.execute("INSERT INTO favorites VALUES ('game', 2)")
db.commit()
db.close()

# 1. Path resolution.
with LibraryReader(db_path, primary_root=card) as lib:
    games = {g.game_id: g for g in lib.all_games()}
check(games[1].rom_path == str(card / "Roms/DC/Primary.gdi") and games[1].exists,
      "primary-card row resolves against the launcher card")
check(games[2].rom_path == str(secondary_rom) and games[2].exists,
      "secondary-card absolute row is unchanged")

os.environ["SDCARD_PATH"] = str(card)
with LibraryReader(db_path) as lib:
    check(lib.game_by_id(1).rom_path == str(card / "Roms/DC/Primary.gdi"),
          "SDCARD_PATH resolves primary rows")
os.environ.pop("SDCARD_PATH")
with LibraryReader(db_path) as lib:
    check(lib.game_by_id(1).exists, "without SDCARD_PATH, the library's own card resolves them")
    recents = lib.recents_and_favourites()
check([g.game_id for g in recents] == [1, 3, 2] and all(g.rom_path.startswith("/") for g in recents),
      "recents and favourites carry resolved paths")

# 2. Disc sheets.
check(leaf_precache.missing_disc_files(str(dc / "Primary.gdi")) == [],
      "complete GDI (quoted names with spaces) has no missing tracks")
check(leaf_precache.missing_disc_files(str(dc / "Broken.gdi")) == ["Broken02.raw"],
      "GDI missing its audio track names that file")
check(leaf_precache.missing_disc_files(str(scd / "Game.cue")) == [], "complete CUE")
check(leaf_precache.missing_disc_files(str(scd / "NoBin.cue")) == ["NoBin.bin"],
      "CUE missing its bin names that file")
check(leaf_precache.missing_disc_files(str(secondary_rom)) == [], "a CHD is not a sheet")

# 3. Outcomes through the real _prepare_one, with a hasher that cannot hash.
class Unhashable:
    available = True
    error = None

    @staticmethod
    def console_id(system):
        return 40

    def hash_rom(self, path, system):
        self.last = path
        return HashResult(None, "rc_hash could not hash it")


server = types.SimpleNamespace(storage=None, config_data={})
job = leaf_precache.PrecacheJob(server)
job._hasher = Unhashable()
creds = {"user": "synth", "token": "tok"}
with LibraryReader(db_path, primary_root=card) as lib:
    outcome = {gid: job._prepare_one(lib.game_by_id(gid), creds, "ua") for gid in (1, 3, 5, 6)}
check(outcome[6].status == "missing" and outcome[6].detail == "ROM file is missing",
      "absent ROM -> missing")
check(outcome[3].status == "missing" and "Broken02.raw" in outcome[3].detail,
      "GDI with an absent track -> missing, naming the track")
check(outcome[5].status == "missing" and "NoBin.bin" in outcome[5].detail,
      "CUE with an absent bin -> missing")
check(outcome[1].status == "unsupported" and job._hasher.last == str(card / "Roms/DC/Primary.gdi"),
      "present but unhashable disc -> unsupported, hashed at its resolved path")


class ConsoleTable(Unhashable):
    @staticmethod
    def console_id(system):
        return None if system == "SATURN" else 40


job._hasher = ConsoleTable()
saturn = LibraryGame(game_id=9, name="Saturn game", system="SATURN",
                     rom_path=str(touch(card / "Roms" / "SS" / "Game.chd")))
not_prepared = job._prepare_one(saturn, creds, "ua")
check(not_prepared.status == "unsupported"
      and not_prepared.detail == "SATURN games are not prepared by this pak",
      "a system Leaf does not run through RetroAchievements says so, not 'no RA console'")
job._hasher = Unhashable()

for o in outcome.values():
    job._record(o)
snap = job.snapshot()
check(snap["missing"] == 3 and snap["unsupported"] == 1 and snap["failed"] == 0,
      f"status counts missing separately (missing={snap['missing']}, "
      f"unsupported={snap['unsupported']}, failed={snap['failed']})")

# 4. Games RetroAchievements has no data for, through _prepare_one with the
# real achievementsets fetch and failure classification: only urlopen is
# replaced, so http_get's own status handling decides what the job sees.
import email.message  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import urllib.error  # noqa: E402
import urllib.parse  # noqa: E402
from unittest import mock  # noqa: E402

from raofflineproxy import cache_keys, network  # noqa: E402
from raofflineproxy.storage import Storage  # noqa: E402

# http_get logs every failed request; these are all deliberate.
logging.getLogger("raofflineproxy").setLevel(logging.CRITICAL)


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode()
        self.status = 200
        self.headers = email.message.Message()
        self.headers["Content-Type"] = "application/json"

    def read(self, *_):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def upstream(*answers):
    """urlopen that plays `answers` in order and records each action asked."""
    queue = list(answers)
    asked = []

    def urlopen(request, timeout=None, context=None):
        asked.append(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(request.full_url).query))["r"])
        answer = queue.pop(0) if queue else ("network",)
        kind = answer[0]
        if kind == "json":
            return FakeResponse(answer[1])
        if kind == "http":
            code, payload = answer[1], answer[2]
            raise urllib.error.HTTPError(request.full_url, code, "error", {},
                                         io.BytesIO(json.dumps(payload).encode()))
        raise urllib.error.URLError("synthetic: network unreachable")

    return urlopen, asked


not_found = ("http", 404, {"Success": False, "Error": "Unknown game.", "Code": "not_found",
                           "Status": 404})
game_id_zero = ("json", {"Success": True, "GameId": 0, "Title": "", "Sets": []})
bad_token = ("http", 401, {"Success": False, "Error": "Invalid token.",
                           "Code": "invalid_credentials", "Status": 401})
known = ("json", {"Success": True, "GameId": 77, "Sets": [{}]})
later_calls = []

net_stubs = [
    mock.patch.object(network, "configured_ssl_context", lambda: None),
    mock.patch.object(network._request_throttle, "wait", lambda *a, **k: None),
    mock.patch.object(leaf_precache.time, "sleep", lambda s: None),
    mock.patch.object(leaf_precache, "cache_unlocks",
                      lambda *a, **k: later_calls.append(("unlocks",))),
    mock.patch.object(leaf_precache, "cache_session",
                      lambda *a, **k: later_calls.append(("session",))),
]
for stub in net_stubs:
    stub.start()


def unknown_rom_job(system_hash: str):
    store = WORK / f"nodata-{system_hash[:6]}-{len(list(WORK.iterdir()))}"
    store.mkdir()
    srv = types.SimpleNamespace(storage=Storage(store / "proxy.sqlite3"), config_data={})
    new_job = leaf_precache.PrecacheJob(srv)

    class OneHash:
        available = True
        error = None

        @staticmethod
        def console_id(system):
            return 40 if system == "DC" else 4

        def hash_rom(self, path, system):
            return HashResult(system_hash)

    new_job._hasher = OneHash()
    return new_job, srv


def prepare(job_, game, *answers):
    urlopen, asked = upstream(*answers)
    later_calls.clear()
    with mock.patch.object(network.urllib.request, "urlopen", urlopen):
        result = job_._prepare_one(game, creds, "ua")
    return result, asked


def no_rows_for(srv, rom_hash: str) -> bool:
    """Nothing a launch reads beyond RA's own answer: no game id mapping."""
    return srv.storage.get_cache(cache_keys.game_id(rom_hash)) is None


with LibraryReader(db_path, primary_root=card) as lib:
    nodata_games = (lib.game_by_id(4), lib.game_by_id(1))  # SEGACD, DC

for game in nodata_games:
    label = game.system
    rom_hash = ("ab" if label == "DC" else "cd") + "0123456789abcdef0123456789abcd"

    # Unknown hash, answered 404 not_found (achievementsets, then the one
    # classifying request).
    job_, srv = unknown_rom_job(rom_hash)
    first, asked = prepare(job_, game, not_found, not_found)
    check(first.status == "unsupported"
          and first.detail == "RetroAchievements has no entry for this ROM"
          and asked == ["achievementsets", "achievementsets"] and not later_calls
          and no_rows_for(srv, rom_hash),
          f"{label}: a 404 not_found hash is 'no RA data', classified with one extra "
          f"request, and nothing is cached ({first.status}: {first.detail}; asked {asked})")
    again, asked = prepare(job_, game, not_found)
    check(again.status == "unsupported" and asked == [],
          f"{label}: the next run reports it again without asking (asked {asked})")

    # Unknown hash, answered Success with GameId 0.
    job_, srv = unknown_rom_job(rom_hash)
    first, asked = prepare(job_, game, game_id_zero)
    check(first.status == "unsupported"
          and first.detail == "no RetroAchievements game for this ROM"
          and asked == ["achievementsets"] and not later_calls and no_rows_for(srv, rom_hash),
          f"{label}: GameId 0 is 'no RA data' and stores no game id, unlocks or "
          f"session ({first.status}: {first.detail}; asked {asked})")
    again, asked = prepare(job_, game, game_id_zero)
    check(again.status == "unsupported" and asked == [],
          f"{label}: a GameId 0 game is not 'already cached' on the next run, and is not "
          f"asked again ({again.status}: {again.detail}; asked {asked})")
    snap_job = leaf_precache.PrecacheJob(types.SimpleNamespace(storage=None, config_data={}))
    snap_job._record(first)
    snap_job._record(again)
    snap = snap_job.snapshot()
    check(snap["cached"] == 0 and snap["skipped"] == 0 and snap["unsupported"] == 2,
          f"{label}: neither run counts as prepared (cached={snap['cached']}, "
          f"skipped={snap['skipped']}, unsupported={snap['unsupported']})")

    # No network: failed and retryable, never recorded as unknown.
    job_, srv = unknown_rom_job(rom_hash)
    first, asked = prepare(job_, game, ("network",), ("network",))
    check(first.status == "failed" and first.detail.startswith("request failed")
          and not job_._known_unknown(rom_hash),
          f"{label}: no network is a retryable failure, not 'no RA data' ({first.detail})")
    retry, asked = prepare(job_, game, known)
    check(retry.status == "cached" and asked == ["achievementsets"],
          f"{label}: and the next run asks again and prepares it ({retry.status})")
    third, asked = prepare(job_, game)
    check(third.status == "skipped" and asked == [],
          f"{label}: a game RetroAchievements does know stays 'already cached' "
          f"({third.status}; asked {asked})")

    # A rejected token is an auth failure, which stops the run.
    job_, srv = unknown_rom_job(rom_hash)
    first, asked = prepare(job_, game, bad_token, bad_token)
    check(first.status == "failed" and first.detail.startswith("auth:")
          and not job_._known_unknown(rom_hash),
          f"{label}: a rejected token is an auth failure ({first.detail})")

    # The first request failed but the classifying one succeeds: transient.
    job_, srv = unknown_rom_job(rom_hash)
    first, asked = prepare(job_, game, ("network",), known)
    check(first.status == "failed" and "transiently" in first.detail
          and not job_._known_unknown(rom_hash),
          f"{label}: a blip is retryable, never 'no RA data' ({first.detail})")

for stub in net_stubs:
    stub.stop()

if failures:
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1)
print("\nprecache-fixture-test: all checks passed")
