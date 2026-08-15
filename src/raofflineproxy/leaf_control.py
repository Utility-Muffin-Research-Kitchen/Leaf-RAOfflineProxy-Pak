"""Loopback control endpoints for the pak's own UI.

The pre-cache job lives in the service rather than the UI process, because the
service already owns everything the job needs: the cache, the HTTP stack, the
learned credentials, and a supervised lifetime. That also means a run survives
the user closing the UI, and that two UIs cannot start two runs racing each
other's pacing budget.

These endpoints are strictly for the pak's own front end. They sit behind the
same loopback-only check as the proxy itself and are namespaced under
``/leaf/`` so they can never collide with a RetroAchievements path -- the proxy
forwards anything it does not recognise upstream, and a route that shadowed a
real API path would be a silent, confusing failure.

The patch in ``proxy_service.py`` is deliberately three lines that delegate
here: upstream rewrites that file between releases, and every line of ours in
it is a line to re-resolve at the next bump.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import parse_qs, urlsplit

from .leaf_library import LibraryReader, LibraryUnavailable
from .leaf_precache import PrecacheJob

LOGGER = logging.getLogger("raofflineproxy")

#: Refuse absurd per-game selections outright. The UI sends one game at a time
#: for the per-game action; a large explicit list is either a bug or someone
#: reaching for the "cache everything" the plan rules out.
MAX_EXPLICIT_GAMES = 50


def _json(status: int, payload: dict) -> bytes:
    from .proxy_service import ok_json, response_bytes

    body = json.dumps(payload, separators=(",", ":"))
    if status == 200:
        return ok_json(body)
    return response_bytes(status, body, "Error")


def _job(server) -> PrecacheJob:
    job = getattr(server, "_leaf_precache_job", None)
    if job is None:
        job = PrecacheJob(server)
        server._leaf_precache_job = job
    return job


def handle_precache_request(server, command: str, path: str, body: str) -> bytes:
    route = urlsplit(path).path
    query = parse_qs(urlsplit(path).query)

    try:
        if command == "GET" and route == "/leaf/precache/status":
            return _json(200, _job(server).snapshot())

        if command == "GET" and route == "/leaf/precache/games":
            return _games(query)

        if command == "POST" and route == "/leaf/precache/start":
            return _start(server, body)

        if command == "POST" and route == "/leaf/precache/cancel":
            cancelled = _job(server).cancel()
            return _json(200, {"cancelled": cancelled})
    except LibraryUnavailable as exc:
        # Expected and user-facing: a launcher schema we do not understand, or
        # no library at all. 409 rather than 500 -- nothing is broken here.
        LOGGER.info("Pre-cache library unavailable: %s", exc)
        return _json(409, {"error": str(exc)})
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.exception("Pre-cache control request failed")
        return _json(500, {"error": str(exc)})

    return _json(404, {"error": "unknown control route"})


#: Cap on a single picker page. Large enough for the biggest system on the
#: qualification device (ARCADE, 375) with room to spare, small enough that a
#: broad search cannot hand the UI a list it has to scroll forever.
MAX_PICKER_GAMES = 500


def _games(query: dict) -> bytes:
    scope = (query.get("scope") or ["recents"])[0]
    system = (query.get("system") or [""])[0].strip()
    search = (query.get("q") or [""])[0].strip()

    with LibraryReader() as library:
        if scope == "systems":
            return _json(
                200,
                {
                    "scope": scope,
                    "schema_version": library.schema_version,
                    "total_games": library.game_count(),
                    "systems": [
                        {"system": name, "count": count}
                        for name, count in library.systems()
                    ],
                },
            )
        if scope == "all":
            games = library.all_games(
                system=system or None, query=search or None, limit=MAX_PICKER_GAMES + 1
            )
        elif scope == "recents":
            games = library.recents_and_favourites()
        else:
            return _json(400, {"error": f"unknown scope {scope!r}"})

        truncated = len(games) > MAX_PICKER_GAMES
        if truncated:
            games = games[:MAX_PICKER_GAMES]

        return _json(
            200,
            {
                "scope": scope,
                "system": system,
                "q": search,
                "truncated": truncated,
                "schema_version": library.schema_version,
                "total_games": library.game_count(),
                "games": [
                    {
                        "id": g.game_id,
                        "name": g.name,
                        "system": g.system,
                        "source": g.source,
                    }
                    for g in games
                ],
            },
        )


def _start(server, body: str) -> bytes:
    try:
        request = json.loads(body) if body.strip() else {}
    except ValueError:
        return _json(400, {"error": "request body was not JSON"})

    scope = request.get("scope") or ("games" if request.get("game_ids") else "recents")

    with LibraryReader() as library:
        if scope == "recents":
            games = library.recents_and_favourites()
            label = "Recents and Favourites"
        elif scope == "games":
            ids = request.get("game_ids") or []
            if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
                return _json(400, {"error": "game_ids must be a list of integers"})
            if len(ids) > MAX_EXPLICIT_GAMES:
                return _json(
                    400,
                    {
                        "error": f"at most {MAX_EXPLICIT_GAMES} games per run; "
                        "bulk pre-caching the whole library is not supported"
                    },
                )
            games = [g for g in (library.game_by_id(i) for i in ids) if g is not None]
            missing = len(ids) - len(games)
            label = games[0].name if len(games) == 1 else f"{len(games)} games"
            if missing:
                LOGGER.info("Pre-cache start: %d requested id(s) not in library", missing)
        else:
            return _json(400, {"error": f"unknown scope {scope!r}"})

    started, message = _job(server).start(games, label)
    if not started:
        # 409: a legitimate refusal (already running, nothing to do, no token),
        # not a failure of the request itself.
        return _json(409, {"error": message})
    return _json(200, {"started": True, "message": message, "total": len(games)})
