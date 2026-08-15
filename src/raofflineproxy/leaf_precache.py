"""Pre-cache RetroAchievements data for games that have not been played online.

Automatic caching only covers games already launched online, so packing the
device for a trip leaves everything unplayed uncached. This job closes that
gap: hash a ROM, ask RetroAchievements for the same responses a real launch
would have produced, and store them under the same cache keys, so a later
offline launch is an ordinary cache hit and the proxy needs no special case.

Three things shape the design, and none is arbitrary:

**Politeness is a correctness constraint, not a nicety.** Pre-caching costs
three API calls per game against a free community service. The qualification
device holds 1,968 games, so a "cache everything" sweep would be over an hour
of continuous requests -- an invitation to throttle or block the pak's user
agent, which would break the feature for every user. The job therefore paces
itself (see PACING) and the *set* is bounded by the caller: Recents and
Favourites, or one game. There is deliberately no "all games" entry point.

**The cache is the resume state.** A run that is interrupted -- the user quits,
the service stops, the battery dies -- leaves every completed game cached.
Re-running skips them by checking for the cache keys rather than by trusting a
progress file, so resumability cannot drift out of sync with reality.

**A wrong hash is worse than no hash.** Everything is keyed on the hash
rcheevos computes inside RetroArch. A hash that disagrees stores data under a
key nothing ever looks up: it fails silently, and only once the user is
offline. That is why hashing goes through the same rc_hash build RetroArch
uses, and why a game whose hash cannot be computed is reported rather than
guessed at.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass, field, asdict

from . import cache_keys
from .config import FALLBACK_USER_AGENT
from .leaf_romhash import RomHasher
from .rom_cache import cache_achievementsets, cache_session, cache_unlocks

LOGGER = logging.getLogger("raofflineproxy")

#: Between games. NextUI paces its RetroAchievements submissions at 500-1500 ms
#: with a longer pause every fifth, and that shape is a reasonable citizen for
#: the same service. Randomised so many devices do not fall into lockstep.
PACING_MIN_SECONDS = 0.5
PACING_MAX_SECONDS = 1.5
PACING_LONG_EVERY = 5
PACING_LONG_SECONDS = 3.0

#: Between the individual calls for one game. Small: three calls back to back
#: is what an ordinary online launch already does.
INTER_CALL_SECONDS = 0.2

#: How long to remember that RetroAchievements has no entry for a ROM. Not
#: permanent, because RetroAchievements adds games; not zero, because without
#: it every re-run re-asks the same question and spends the pacing budget
#: learning nothing.
UNKNOWN_ROM_TTL_SECONDS = 30 * 24 * 3600


@dataclass
class GameOutcome:
    game_id: int
    name: str
    system: str
    #: "cached" | "skipped" (already cached) | "unsupported" | "failed"
    status: str
    detail: str = ""
    rom_hash: str = ""
    ra_game_id: int = 0


@dataclass
class PrecacheProgress:
    state: str = "idle"  # idle | running | done | cancelled | failed
    total: int = 0
    processed: int = 0
    cached: int = 0
    skipped: int = 0
    unsupported: int = 0
    failed: int = 0
    current: str = ""
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    outcomes: list[GameOutcome] = field(default_factory=list)

    def as_dict(self) -> dict:
        data = asdict(self)
        # The UI polls this; the full outcome list grows without bound on a
        # large set and is only useful for the tail.
        data["outcomes"] = [asdict(o) for o in self.outcomes[-25:]]
        return data


class PrecacheJob:
    """One pre-cache run, driven on a background thread.

    Only one runs at a time: the pacing budget is global to the device, so two
    concurrent runs would double the request rate against RetroAchievements
    while each believed it was being polite.
    """

    def __init__(self, server) -> None:
        self._server = server
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._progress = PrecacheProgress()
        self._hasher: RomHasher | None = None

    # -- state -----------------------------------------------------------
    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def snapshot(self) -> dict:
        with self._lock:
            return self._progress.as_dict()

    def cancel(self) -> bool:
        if not self.running:
            return False
        self._cancel.set()
        return True

    # -- control ---------------------------------------------------------
    def start(self, games: list, label: str = "") -> tuple[bool, str]:
        """Begin a run over `games` (leaf_library.LibraryGame values)."""
        with self._lock:
            if self.running:
                return False, "a pre-cache run is already in progress"
            if not games:
                return False, "nothing to prepare"

            credentials = self._server.storage.load_login_credentials()
            if credentials is None:
                return False, (
                    "no cached RetroAchievements token; sign in and launch one "
                    "game online first"
                )
            if self._server.storage.is_token_invalid(credentials["token"]):
                return False, "the cached RetroAchievements token was rejected; play online once to refresh it"

            self._cancel.clear()
            self._progress = PrecacheProgress(
                state="running",
                total=len(games),
                started_at=time.time(),
                current=label,
            )
            self._thread = threading.Thread(
                target=self._run,
                args=(list(games), credentials),
                name="raproxy-precache",
                daemon=True,
            )
            self._thread.start()
            return True, f"preparing {len(games)} game(s)"

    # -- worker ----------------------------------------------------------
    def _run(self, games: list, credentials: dict) -> None:
        user_agent = self._user_agent()
        try:
            for index, game in enumerate(games):
                if self._cancel.is_set():
                    self._finish("cancelled")
                    return

                self._set_current(game.name)
                outcome = self._prepare_one(game, credentials, user_agent)
                self._record(outcome)

                # An auth failure will fail identically for every remaining
                # game, so continuing would be a hundred pointless requests
                # against a service that just told us no.
                if outcome.status == "failed" and outcome.detail.startswith("auth:"):
                    self._finish("failed", outcome.detail)
                    return

                if index + 1 < len(games):
                    self._pace(index + 1)
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.exception("Pre-cache run failed")
            self._finish("failed", str(exc))
            return

        self._finish("cancelled" if self._cancel.is_set() else "done")

    def _prepare_one(self, game, credentials: dict, user_agent: str) -> GameOutcome:
        base = GameOutcome(
            game_id=game.game_id, name=game.name, system=game.system, status="failed"
        )

        hasher = self._ensure_hasher()
        if not hasher.available:
            base.detail = hasher.error or "rc_hash unavailable"
            return base

        if not game.exists:
            base.status = "unsupported"
            base.detail = "ROM file is missing"
            return base

        if hasher.console_id(game.system) is None:
            base.status = "unsupported"
            base.detail = f"{game.system} has no RetroAchievements console"
            return base

        result = hasher.hash_rom(game.rom_path, game.system)
        if result.hash is None:
            base.status = "unsupported"
            base.detail = result.error or "could not hash ROM"
            return base
        base.rom_hash = result.hash

        user = credentials["user"]
        if self._already_cached(result.hash, user):
            base.status = "skipped"
            base.detail = "already cached"
            return base

        if self._known_unknown(result.hash):
            base.status = "unsupported"
            base.detail = "RetroAchievements has no entry for this ROM"
            return base

        # achievementsets does double duty: it carries the achievement
        # definitions offline play needs AND resolves the hash to a game id,
        # so this is a request we would have to make regardless.
        body = cache_achievementsets(
            result.hash, credentials, user_agent, self._server.config_data,
            self._server.storage,
            cache_images=self._cache_images(),
        )
        if body is None:
            # cache_achievementsets returns None for every failure alike, so a
            # ROM RetroAchievements simply does not have looks identical to a
            # dead network. Those need opposite handling -- one is permanent
            # and must stop being retried, the other must be retried -- so
            # classify with one request on the failure path only.
            status, detail = self._classify_failure(result.hash, credentials, user_agent)
            base.status = status
            base.detail = detail
            if status == "unsupported":
                self._mark_unknown(result.hash)
            return base

        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            base.detail = "achievementsets response was not JSON"
            return base

        if payload.get("Error") or payload.get("Success") is False:
            detail = str(payload.get("Error") or "rejected")
            # Distinguish the one error worth aborting the whole run for.
            if "token" in detail.lower() or "credential" in detail.lower():
                base.detail = f"auth: {detail}"
            else:
                base.detail = detail
            return base

        ra_game_id = int(payload.get("GameId") or 0)
        if ra_game_id <= 0:
            # A real answer meaning "RetroAchievements does not know this ROM".
            base.status = "unsupported"
            base.detail = "no RetroAchievements game for this ROM"
            return base
        base.ra_game_id = ra_game_id

        # Store the hash -> id mapping the offline gameid path looks for. This
        # is derived from the response we already have, so it costs no request.
        self._server.storage.upsert_cache(
            cache_keys.game_id(result.hash),
            json.dumps({"Success": True, "GameID": ra_game_id}, separators=(",", ":")),
        )

        time.sleep(INTER_CALL_SECONDS)
        cache_unlocks(
            ra_game_id, credentials, user_agent, self._server.config_data,
            self._server.storage,
        )
        time.sleep(INTER_CALL_SECONDS)
        cache_session(ra_game_id, credentials, self._server.storage)

        base.status = "cached"
        base.detail = f"{len(payload.get('Sets') or [])} set(s)"
        return base

    # -- helpers ---------------------------------------------------------
    def _already_cached(self, rom_hash: str, user: str) -> bool:
        """True when a previous run (or a real launch) already covered this.

        Keyed on the cache itself rather than a progress file, so an
        interrupted run resumes correctly even if the file is lost or stale.
        """
        storage = self._server.storage
        return storage.get_cache(cache_keys.achievementsets(rom_hash, user)) is not None

    def _classify_failure(
        self, rom_hash: str, credentials: dict, user_agent: str
    ) -> tuple[str, str]:
        """Tell "RetroAchievements does not have this ROM" from "no network".

        Costs one request, and only when something already failed. Worth it:
        calling an unknown ROM a failure tells the user something is broken
        when nothing is, and leaves it to be retried on every future run.
        """
        from .config import upstream_host
        from .network import build_api_url, http_get

        url = build_api_url(
            upstream_host(self._server.config_data),
            "achievementsets",
            {"m": rom_hash, "u": credentials["user"], "t": credentials["token"]},
        )
        try:
            http_get(url, user_agent)
        except Exception as exc:
            text = str(exc).lower()
            if "unknown game" in text or "404" in text:
                return "unsupported", "RetroAchievements has no entry for this ROM"
            if "401" in text or "403" in text or "credential" in text or "token" in text:
                return "failed", f"auth: {exc}"
            return "failed", f"request failed: {exc}"
        # Succeeded on the retry: a transient blip, so leave it retryable
        # rather than recording a permanent negative.
        return "failed", "achievementsets request failed transiently; try again"

    def _unknown_key(self, rom_hash: str) -> str:
        return f"leaf:unknown-rom:{cache_keys.normalize_hash(rom_hash)}"

    def _known_unknown(self, rom_hash: str) -> bool:
        """Has RetroAchievements already told us it does not have this ROM?

        Deliberately expires. RetroAchievements gains games over time, so a
        permanent negative would quietly make a ROM uncacheable forever; but
        without any memory, every re-run spends a request re-learning the same
        answer, against the pacing budget that exists to keep us welcome.
        """
        entry = self._server.storage.get_cache(self._unknown_key(rom_hash))
        if entry is None:
            return False
        try:
            marked_at = float(entry["responseBody"])
        except (TypeError, ValueError):
            return False
        return (time.time() - marked_at) < UNKNOWN_ROM_TTL_SECONDS

    def _mark_unknown(self, rom_hash: str) -> None:
        self._server.storage.upsert_cache(self._unknown_key(rom_hash), str(time.time()))

    def _ensure_hasher(self) -> RomHasher:
        if self._hasher is None:
            self._hasher = RomHasher()
        return self._hasher

    def _user_agent(self) -> str:
        # The proxy records the last user agent RetroArch sent under this key.
        # Reusing it keeps pre-cached entries indistinguishable from
        # launch-cached ones, and keeps this pak honest about what it is.
        entry = self._server.storage.get_cache(cache_keys.USER_AGENT)
        if entry and entry.get("responseBody"):
            return entry["responseBody"]
        return FALLBACK_USER_AGENT

    def _cache_images(self) -> bool:
        from .config import image_caching_enabled

        return image_caching_enabled(self._server.config_data)

    def _pace(self, completed: int) -> None:
        delay = random.uniform(PACING_MIN_SECONDS, PACING_MAX_SECONDS)
        if completed % PACING_LONG_EVERY == 0:
            delay += PACING_LONG_SECONDS
        # Wait on the cancel event so a cancel is honoured immediately rather
        # than after the pacing sleep -- otherwise "Stop" appears to hang.
        self._cancel.wait(delay)

    def _set_current(self, name: str) -> None:
        with self._lock:
            self._progress.current = name

    def _record(self, outcome: GameOutcome) -> None:
        with self._lock:
            p = self._progress
            p.processed += 1
            p.outcomes.append(outcome)
            if outcome.status == "cached":
                p.cached += 1
            elif outcome.status == "skipped":
                p.skipped += 1
            elif outcome.status == "unsupported":
                p.unsupported += 1
            else:
                p.failed += 1
        LOGGER.info(
            "Pre-cache %s: %s (%s) %s",
            outcome.status, outcome.name, outcome.system, outcome.detail,
        )

    def _finish(self, state: str, error: str = "") -> None:
        with self._lock:
            self._progress.state = state
            self._progress.error = error
            self._progress.current = ""
            self._progress.finished_at = time.time()
        LOGGER.info(
            "Pre-cache run %s: cached=%d skipped=%d unsupported=%d failed=%d%s",
            state,
            self._progress.cached,
            self._progress.skipped,
            self._progress.unsupported,
            self._progress.failed,
            f" error={error}" if error else "",
        )
