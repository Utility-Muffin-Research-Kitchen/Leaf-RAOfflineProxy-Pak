#!/usr/bin/env python3
"""Host fixtures for the pending-award account guard in the assembled app.

The cache keeps one learned login row per user. Pending awards must flush
with their owner's own token, whichever order the accounts signed in, and
never with another account's. Offline:
- a hardcore award is refused, not queued;
- a second user's award is refused while the first user's awards are pending;
- a leaderboard entry is refused and never queued (twice: no UNIQUE clash);
- the owner's next award queues and chains to the previous one.
And a sign-in while the probe says offline is answered from the cached login
without first waiting on upstream (name resolution is unbounded).

All accounts and tokens are synthetic; nothing touches the network.

Run: python3 scripts/account-guard-test.py (after scripts/assemble-app.sh)
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import threading
import types
import urllib.parse
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "build" / "mlp1" / "app" / "raofflineproxy"

if not APP.is_dir():
    sys.exit(f"assembled app missing: {APP} (run scripts/assemble-app.sh)")

WORK = pathlib.Path(tempfile.mkdtemp(prefix="raop-account-guard-"))
os.environ["RAOFFLINEPROXY_CONFIG_DIR"] = str(WORK / "config")
sys.path.insert(0, str(APP.parent))

from raofflineproxy import cache_keys, flusher  # noqa: E402
from raofflineproxy.proxy_service import ProxyRuntimeServer  # noqa: E402
from raofflineproxy.storage import Storage  # noqa: E402

failures = []


def check(ok: bool, what: str) -> None:
    if not ok:
        failures.append(what)
        print(f"FAIL {what}")
    else:
        print(f"ok   {what}")


_db_count = 0


def fresh_storage() -> Storage:
    global _db_count
    _db_count += 1
    path = WORK / f"db{_db_count}"
    path.mkdir(parents=True)
    return Storage(path / "proxy.sqlite3")


def learn_login(storage: Storage, user: str, at: int) -> None:
    body = json.dumps({"Success": True, "User": user, "Token": f"tok-{user.lower()}"})
    storage.upsert_cache(cache_keys.login(user), body, cached_at=at)


def offline_server(storage: Storage) -> types.SimpleNamespace:
    """The real award path of ProxyRuntimeServer, offline, without a socket."""
    server = types.SimpleNamespace(
        storage=storage,
        pending_award_lock=threading.Lock(),
        config_data={},
    )
    server.is_online = lambda: False
    for name in (
        "handle_award_request",
        "queue_offline_award",
        "pending_award_account_mismatch",
        "queue_award",
        "is_already_unlocked_award",
        "build_cached_achievement_game_ids",
        "fetch_cached_score",
    ):
        setattr(server, name, types.MethodType(getattr(ProxyRuntimeServer, name), server))
    return server


def award_body(user: str, achievement_id: int, hardcore: int = 0) -> str:
    return (
        f"r=awardachievement&u={user}&t=tok-{user.lower()}"
        f"&a={achievement_id}&h={hardcore}&m=0123456789abcdef0123456789abcdef"
    )


def status_of(response: bytes) -> int:
    return int(response.split(b" ", 2)[1])


def pending_rows(storage: Storage) -> list[dict]:
    return [
        award
        for award in storage.get_pending_awards()
        if award.get("status", "pending") == "pending"
    ]


# 1. Login lookups: the most recent sign-in, and a user's own row by name.
s = fresh_storage()
check(s.load_login_credentials() is None, "no login cached -> none")
learn_login(s, "Zed", 1000)
learn_login(s, "amy", 2000)
check(s.load_login_credentials()["user"] == "amy",
      "latest login is the newest sign-in, not the first row")
learn_login(s, "Zed", 3000)  # an update keeps Zed's older row id
check(s.load_login_credentials()["user"] == "Zed",
      "re-signing in an older account makes it the latest again")
check(s.load_login_credentials_for("zed")["token"] == "tok-zed",
      "own login found by name, case-insensitively")
check(s.load_login_credentials_for("amy")["token"] == "tok-amy",
      "each account keeps its own row")
check(s.load_login_credentials_for("nobody") is None, "unknown account -> none")

# 2. Flush credentials follow the pending awards' owner in every order.
for order in (["owner", "other"], ["other", "owner"], ["aaa", "zzz"], ["zzz", "aaa"]):
    owner = "owner" if "owner" in order else "zzz"
    for newest_last in (True, False):
        s = fresh_storage()
        times = [1000, 2000] if newest_last else [2000, 1000]
        for user, at in zip(order, times):
            learn_login(s, user, at)
        pending = [{"requestBody": award_body(owner, 1001), "status": "pending"}]
        creds, err = flusher.resolve_leaf_credentials(s, pending)
        check(creds is not None and creds["user"] == owner and err is None,
              f"inserted {order}, newest {'last' if newest_last else 'first'}: "
              f"flush uses {owner}'s own token")

s = fresh_storage()
learn_login(s, "other", 1000)
creds, err = flusher.resolve_leaf_credentials(
    s, [{"requestBody": award_body("owner", 1001), "status": "pending"}])
check(creds is None and err.startswith("account_mismatch:") and "owner" in err,
      "owner's token not cached -> defer, never the other account's")

s = fresh_storage()
learn_login(s, "a", 1000)
learn_login(s, "b", 2000)
creds, err = flusher.resolve_leaf_credentials(s, [
    {"requestBody": award_body("a", 1001), "status": "pending"},
    {"requestBody": award_body("b", 1002), "status": "pending"},
])
check(creds is None and "span multiple users" in err, "two owners -> defer")

s = fresh_storage()
learn_login(s, "a", 1000)
learn_login(s, "b", 2000)
creds, err = flusher.resolve_leaf_credentials(s, [{"requestBody": "r=awardachievement&a=1", "status": "pending"}])
check(creds is not None and creds["user"] == "b", "ownerless award falls back to the latest login")

# 3. Request-level guard, offline, through the real award handler.
s = fresh_storage()
server = offline_server(s)
headers = {"User-Agent": "Flycast/2.7"}
r = server.handle_award_request("/dorequest.php", award_body("synthA", 1001, hardcore=1), headers)
check(status_of(r) == 403 and b"hardcore_not_supported" in r and not pending_rows(s),
      "offline hardcore award refused, nothing queued")
r = server.handle_award_request("/dorequest.php", award_body("synthA", 1001), headers)
rows = pending_rows(s)
check(status_of(r) == 200 and len(rows) == 1 and rows[0]["prevHash"] == "genesis",
      "first casual award queued at the chain's genesis")
r = server.handle_award_request("/dorequest.php", award_body("synthB", 1002), headers)
check(status_of(r) == 409 and b"account_mismatch" in r and b"synth" in r.lower()
      and len(pending_rows(s)) == 1,
      "another account's award refused while synthA's are pending")
for attempt in (1, 2):
    r = server.handle_award_request(
        "/dorequest.php", "r=submitlbentry&u=synthA&t=tok-syntha&i=77&s=12345", headers)
    check(status_of(r) == 503 and b"offline_leaderboard_not_supported" in r
          and len(pending_rows(s)) == 1
          and all(row["achievementId"] != 0 for row in s.get_pending_awards()),
          f"offline leaderboard entry refused, not queued (attempt {attempt})")
r = server.handle_award_request("/dorequest.php", award_body("synthA", 1003), headers)
rows = sorted(pending_rows(s), key=lambda row: row["queuedAt"])
check(status_of(r) == 200 and len(rows) == 2 and rows[1]["prevHash"] == rows[0]["payloadHash"],
      "owner's second award queues and chains to the first")

# 4. End to end: the flush sends only with the owner's token.
learn_login(s, "synthA", 1000)   # owner signed in first...
learn_login(s, "synthB", 2000)   # ...then the device switched accounts
sent = []


def fake_post(url, body, headers=None, **_):
    sent.append(dict(urllib.parse.parse_qsl(body)))
    return 200, "OK", '{"Success":true}'


with mock.patch.object(flusher, "http_post", fake_post), \
     mock.patch.object(flusher, "refresh_and_load_achievement_ids",
                       return_value=({1001, 1003}, {1}, {1001: 1, 1003: 1})):
    outcome = flusher.flush_pending_awards(s, {})
check(outcome.flushed == 2 and outcome.pending_remaining == 0,
      f"both of synthA's awards flushed after the switch (flushed={outcome.flushed})")
check(len(sent) == 2 and all(p.get("u") == "synthA" and p.get("t") == "tok-syntha" for p in sent),
      "every send carried synthA's own user and token")
check(not any(p.get("t") == "tok-synthb" for p in sent), "synthB's token never sent")

s = fresh_storage()
server = offline_server(s)
server.handle_award_request("/dorequest.php", award_body("synthA", 1001), headers)
learn_login(s, "synthB", 2000)   # only the other account is cached
sent.clear()
with mock.patch.object(flusher, "http_post", fake_post), \
     mock.patch.object(flusher, "refresh_and_load_achievement_ids",
                       return_value=({1001}, {1}, {1001: 1})):
    outcome = flusher.flush_pending_awards(s, {})
check(not sent and outcome.flushed == 0 and outcome.pending_remaining == 1
      and "account_mismatch" in (outcome.last_error or ""),
      "owner's token missing: flush defers, sends nothing, keeps the award")

# 5. Sign-in while offline: the cached login answers without forwarding.
s = fresh_storage()
learn_login(s, "synthA", 1000)
forwarded = []


def login_server(online: bool) -> types.SimpleNamespace:
    server = types.SimpleNamespace(storage=s, config_data={})
    server.is_online = lambda: online
    server.forward_to_upstream_result = lambda *a: forwarded.append(a) or ("network_error",)
    server.handle_online_request = lambda *a: b"HTTP/1.1 200 OK\r\n\r\nonline"
    server.handle_offline_request = lambda *a: b"HTTP/1.1 503 x\r\n\r\n"
    for name in ("process_proxy_request", "handle_offline_login", "handle_award_request"):
        setattr(server, name, types.MethodType(getattr(ProxyRuntimeServer, name), server))
    return server


login = "r=login2&u=synthA&t=tok-syntha"
forwarded.clear()
r = login_server(False).process_proxy_request("POST", "/dorequest.php", login, headers)
check(status_of(r) == 200 and b"tok-syntha" in r and not forwarded,
      "offline sign-in answered from the cached login without an upstream attempt")
forwarded.clear()
login_server(False).process_proxy_request("POST", "/dorequest.php", "r=login2&u=synthC&t=x", headers)
check(len(forwarded) == 1, "offline with no cached login still tries upstream")
forwarded.clear()
login_server(True).process_proxy_request("POST", "/dorequest.php", login, headers)
check(len(forwarded) == 1, "online sign-in goes upstream first")

if failures:
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1)
print("\naccount-guard-test: all checks passed")
