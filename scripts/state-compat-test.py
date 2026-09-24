#!/usr/bin/env python3
"""Upgrade from, and roll back to, the published 0.1.0 over one state directory.

The durable state under $USERDATA_PATH/RAOfflineProxy is the SQLite cache and
queue (proxy.sqlite3) plus the award-signing secret (award_secret.key). An
update must keep every cached login, the secret, every queued award and its
owner; a rollback to the published 0.1.0 -- which Pak Rat offers whenever the
installed Leaf is below the new row's floor, or the user reinstalls it -- must
either keep working on the newer state or refuse it explicitly. Losing or
relabelling a queued award is the failure that matters: it is an unlock the
user earned that either vanishes or is submitted as somebody else.

The 0.1.0 code is the tag's own assembly (scripts/build-baseline-app.sh); the
candidate is build/mlp1/app. Each step runs in its own interpreter against a
copy of the state, the way two installed versions would, through the real
Storage, ProxyRuntimeServer request dispatch and flusher. Nothing reaches the
network: upstream is a .invalid host, sockets other than the local listener
are refused, and the upstream calls a flush makes are stubbed.

Scenarios, each seeded by 0.1.0 through its own request handlers:

  switch  A signs in, then B; B queues two casual awards offline; A's award is
          refused while B's are pending (0.1.0's guard).
  single  A signs in and queues two casual awards.
  mixed   A and B both have queued awards: A's through the handler, B's
          through queue_award directly -- a shape 0.1.0's request guard never
          writes, kept to prove neither version flushes a two-owner queue.

For each: upgrade (candidate startup + candidate writes one more award and a
fresh sign-in), compare every old row and the secret, check logins, the award
chain and a stubbed flush; then roll back (0.1.0 startup over the candidate's
state) and require the same decisions 0.1.0 makes over its own state.

Accounts and tokens are synthetic. Tokens are compared, never printed; state
rows are read by an allowlist of columns and bodies are reduced to digests.

Run: python3 scripts/state-compat-test.py (after scripts/assemble-app.sh and
scripts/build-baseline-app.sh)
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent.parent
CANDIDATE = ROOT / "build" / "mlp1" / "app"
BASELINE_LOCK = json.loads(
    (ROOT / "locks" / "state-baseline.lock.json").read_text(encoding="utf-8"))
BASELINE = ROOT / "build" / f"baseline-{BASELINE_LOCK['pak_version']}" / "app"

USERS = ("synthA", "synthB")
UPSTREAM = "https://retroachievements.invalid"


def synthetic_token(user: str) -> str:
    return "tok-" + hashlib.sha256(f"synthetic:{user.lower()}".encode()).hexdigest()[:20]


# ==========================================================================
# Phase side: runs inside one version's interpreter.

def phase_main(app: str, phase: str, scenario: str) -> None:
    sys.path.insert(0, app)
    guard_network()

    from unittest import mock

    from raofflineproxy import cache_keys, flusher, proxy_service  # noqa: E402
    from raofflineproxy.storage import Storage, migrate_user_case_in_cache_keys  # noqa: E402

    # The same startup order run_proxy_service() uses.
    storage = Storage()
    migrate_user_case_in_cache_keys(storage)
    port = free_port()
    server = proxy_service.ProxyRuntimeServer(
        {"proxy_host": "127.0.0.1", "proxy_port": port, "upstream_host": UPSTREAM,
         "cache_images": False},
        storage,
    )
    online = {"value": False}
    server.is_online = lambda: online["value"]
    server.forward_to_upstream_result = fake_forward
    headers = {"User-Agent": "RetroArch/1.21.0 (Linux) synthetic"}
    report: dict = {"phase": phase, "scenario": scenario}

    def sign_in(user: str) -> None:
        online["value"] = True
        body = f"r=login2&u={user}&p=synthetic-password"
        response = server.process_proxy_request("POST", "/dorequest.php", body, headers)
        online["value"] = False
        assert status_of(response) == 200, f"sign-in {user}: {status_of(response)}"

    def award(user: str, achievement_id: int) -> int:
        body = (f"r=awardachievement&u={user}&t={synthetic_token(user)}"
                f"&a={achievement_id}&h=0&m=0123456789abcdef0123456789abcdef")
        return status_of(server.process_proxy_request("POST", "/dorequest.php", body, headers))

    try:
        if phase == "seed":
            report["statuses"] = seed(scenario, sign_in, award, server)
        elif phase == "upgrade":
            report["statuses"] = upgrade(scenario, sign_in, award)
        elif phase == "inspect":
            report.update(inspect(storage, flusher, cache_keys))
        elif phase == "flush":
            report.update(simulate_flush(storage, flusher, mock))
        else:
            raise SystemExit(f"unknown phase {phase}")
    finally:
        server.server_close()
        storage.close()
    print("RESULT " + json.dumps(report, sort_keys=True))


def seed(scenario: str, sign_in, award, server) -> dict:
    statuses = {}
    sign_in("synthA")
    if scenario in ("switch", "mixed"):
        sign_in("synthB")
    if scenario == "switch":
        statuses["B:3001"] = award("synthB", 3001)
        statuses["B:3002"] = award("synthB", 3002)
        statuses["A:1001"] = award("synthA", 1001)
    elif scenario == "single":
        statuses["A:1001"] = award("synthA", 1001)
        statuses["A:1002"] = award("synthA", 1002)
    elif scenario == "mixed":
        statuses["A:1001"] = award("synthA", 1001)
        body = (f"r=awardachievement&u=synthB&t={synthetic_token('synthB')}"
                "&a=2001&h=0&m=0123456789abcdef0123456789abcdef")
        statuses["B:2001(queue_award)"] = 200 if server.queue_award(
            "/dorequest.php", body, {"User-Agent": "synthetic"}) else 500
    return statuses


def upgrade(scenario: str, sign_in, award) -> dict:
    statuses = {}
    if scenario == "switch":
        statuses["B:3003"] = award("synthB", 3003)
        statuses["A:1001"] = award("synthA", 1001)
        sign_in("synthA")  # the device switches back; A is now the latest sign-in
    elif scenario == "single":
        statuses["A:1003"] = award("synthA", 1003)
        sign_in("synthA")
    elif scenario == "mixed":
        sign_in("synthB")
    return statuses


def inspect(storage, flusher, cache_keys) -> dict:
    logins = {}
    for user in USERS:
        entry = storage.get_cache(cache_keys.login(user))
        if entry is None:
            continue
        body = json.loads(entry["responseBody"])
        logins[user] = {"user": body.get("User"),
                        "token_is_synthetic": body.get("Token") == synthetic_token(user)}
    pending = storage.get_pending_awards()
    chain_ok, chain_reason, _ = flusher.verify_chain(pending)
    awaiting = [a for a in pending if a.get("status", "pending") == "pending"]
    credentials, error = flusher.resolve_leaf_credentials(storage, awaiting)
    return {
        "logins": logins,
        "chain_ok": chain_ok,
        "chain_reason": chain_reason,
        "flush_user": credentials["user"] if credentials else None,
        "flush_token_is_owners": bool(credentials) and credentials["token"] == synthetic_token(credentials["user"]),
        "flush_refusal": (error or "").split(":")[0] or None,
    }


def simulate_flush(storage, flusher, mock) -> dict:
    sent = []

    def fake_post(url, body, headers=None, **_):
        params = dict(urllib.parse.parse_qsl(body))
        user = params.get("u", "")
        sent.append({"user": user, "a": int(params.get("a", 0)),
                     "token_is_owners": params.get("t") == synthetic_token(user)})
        return 200, "OK", '{"Success":true}'

    ids = {int(a["achievementId"]) for a in storage.get_pending_awards()}
    with mock.patch.object(flusher, "http_post", fake_post), \
         mock.patch.object(flusher, "refresh_and_load_achievement_ids",
                           return_value=(ids, [1], {i: 1 for i in ids})), \
         mock.patch.object(flusher, "cache_unlocks", lambda *a, **k: None), \
         mock.patch.object(flusher, "cache_session", lambda *a, **k: None), \
         mock.patch.object(flusher.time, "sleep", lambda s: None):
        outcome = flusher.flush_pending_awards(storage, {"upstream_host": UPSTREAM})
    return {
        "sent": sent,
        "flushed": outcome.flushed,
        "pending_remaining": outcome.pending_remaining,
        "deferred_account_mismatch": "account_mismatch" in (outcome.last_error or ""),
    }


def fake_forward(method, path, raw_body, headers):
    params = dict(urllib.parse.parse_qsl(raw_body))
    if params.get("r") == "login2":
        user = params["u"]
        body = json.dumps({"Success": True, "User": user, "Token": synthetic_token(user),
                           "Score": 0, "SoftcoreScore": 0, "Messages": 0,
                           "Permissions": 1, "AccountType": "Registered"})
        return ("success", 200, "OK", body.encode(), "application/json", body)
    return ("network_error", 0, "offline", b"", None, None)


def status_of(response: bytes) -> int:
    return int(response.split(b" ", 2)[1])


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def guard_network() -> None:
    """Refuse every outbound connection; only the local listener may bind."""
    import urllib.request

    def refuse(*_a, **_k):
        raise OSError("network disabled in state-compat-test")

    real_connect = socket.socket.connect

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host in ("127.0.0.1", "::1", "localhost"):
            return real_connect(self, address)
        refuse()

    socket.socket.connect = connect
    socket.create_connection = refuse
    socket.getaddrinfo = refuse
    urllib.request.urlopen = refuse


# ==========================================================================
# Orchestrator side: never imports either app.

failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        failures.append(what)


def run_phase(app: pathlib.Path, phase: str, scenario: str, state: pathlib.Path) -> dict:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("RAOFFLINEPROXY_")}
    env["RAOFFLINEPROXY_CONFIG_DIR"] = str(state)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, str(pathlib.Path(__file__).resolve()),
         "--phase", phase, "--app", str(app), "--scenario", scenario],
        env=env, capture_output=True, text=True, timeout=120,
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("RESULT ")]
    if result.returncode != 0 or not lines:
        # A refusal is a legitimate rollback outcome; report its first line only
        # (stderr can carry request bodies in a traceback, so not the rest).
        first = next((line for line in reversed(result.stderr.splitlines()) if line.strip()), "")
        return {"error": first[:200], "returncode": result.returncode}
    return json.loads(lines[-1][len("RESULT "):])


def owner_of(request_body: str) -> str:
    return dict(urllib.parse.parse_qsl(request_body)).get("u", "")


def fingerprint(state: pathlib.Path) -> dict:
    """Allowlisted columns only; bodies (which carry tokens) become digests."""
    uri = f"file:{state / 'proxy.sqlite3'}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        schema = sorted(row[0] for row in db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"))
        cache = {
            key: {"body": hashlib.sha256(body.encode()).hexdigest(), "cachedAt": cached_at,
                  "firstCachedAt": first}
            for key, body, cached_at, first in db.execute(
                "SELECT cacheKey, responseBody, cachedAt, firstCachedAt FROM api_cache")
        }
        awards = {}
        for row in db.execute(
                "SELECT id, achievementId, queryString, requestBody, userAgent, queuedAt, "
                "retryCount, lastError, status, payloadHash, prevHash, signature, signedAt "
                "FROM pending_awards ORDER BY id"):
            (row_id, achievement_id, query, body, agent, queued, retries, error,
             status, payload_hash, prev_hash, signature, signed_at) = row
            awards[achievement_id] = {
                "id": row_id, "owner": owner_of(body),
                "body": hashlib.sha256(body.encode()).hexdigest(), "query": query,
                "agent": agent, "queuedAt": queued, "retryCount": retries,
                "lastError": error, "status": status, "payloadHash": payload_hash,
                "prevHash": prev_hash, "signature": signature, "signedAt": signed_at,
            }
    secret = (state / "award_secret.key").read_bytes()
    return {"schema": schema, "cache": cache, "awards": awards,
            "secret": hashlib.sha256(secret).hexdigest(), "secret_len": len(secret)}


def copy_state(source: pathlib.Path, target: pathlib.Path) -> pathlib.Path:
    shutil.copytree(source, target)
    return target


def login_keys(fp: dict) -> set[str]:
    return {key for key in fp["cache"] if key.startswith("login2::")}


def orchestrate() -> None:
    for app in (CANDIDATE, BASELINE):
        if not (app / "raofflineproxy" / "storage.py").is_file():
            sys.exit(f"missing {app} (run scripts/assemble-app.sh and "
                     "scripts/build-baseline-app.sh)")

    work = pathlib.Path(tempfile.mkdtemp(prefix="raop-state-compat-"))
    try:
        for scenario in ("switch", "single", "mixed"):
            print(f"-- {scenario}")
            one_scenario(work / scenario, scenario)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def one_scenario(base: pathlib.Path, scenario: str) -> None:
    seeded = base / "seeded"
    seed = run_phase(BASELINE, "seed", scenario, seeded)
    check("error" not in seed, f"0.1.0 seeds the {scenario} state ({seed.get('error', 'ok')})")
    if "error" in seed:
        return
    if scenario == "switch":
        check(seed["statuses"] == {"B:3001": 200, "B:3002": 200, "A:1001": 409},
              f"0.1.0 queued B's awards and refused A's meanwhile {seed['statuses']}")
    before = fingerprint(seeded)
    check(set(before["awards"]) and before["secret_len"] == 32,
          f"seeded {len(before['awards'])} queued award(s), {len(login_keys(before))} "
          "cached login(s) and a 32-byte signing secret")

    # 0.1.0 over its own state: the reference for "same" after a rollback.
    own_inspect = run_phase(BASELINE, "inspect", scenario, copy_state(seeded, base / "own-i"))
    own_flush = run_phase(BASELINE, "flush", scenario, copy_state(seeded, base / "own-f"))

    # ---- Upgrade ---------------------------------------------------------
    upgraded = copy_state(seeded, base / "upgraded")
    up = run_phase(CANDIDATE, "upgrade", scenario, upgraded)
    check("error" not in up, f"candidate starts over 0.1.0 state and writes to it "
                             f"({up.get('error', up.get('statuses'))})")
    after = fingerprint(upgraded)
    check(after["schema"] == before["schema"], "schema unchanged by the candidate")
    check(after["secret"] == before["secret"] and after["secret_len"] == before["secret_len"],
          "award-signing secret unchanged by the candidate")
    kept = all(after["awards"].get(aid) == row for aid, row in before["awards"].items())
    check(kept, "every 0.1.0 award row survives unchanged, owner included")
    new_rows = {aid: row["owner"] for aid, row in after["awards"].items()
                if aid not in before["awards"]}
    expect_new = {"switch": {3003: "synthB"}, "single": {1003: "synthA"}, "mixed": {}}[scenario]
    check(new_rows == expect_new, f"candidate queued exactly {expect_new} ({new_rows})")
    if scenario == "switch":
        check(up.get("statuses", {}).get("A:1001") == 409,
              "candidate still refuses A's award while B's are pending")
    check(login_keys(after) == login_keys(before)
          and all(after["cache"][k]["body"] == before["cache"][k]["body"]
                  for k in login_keys(before)),
          "every cached login survives with the same learned token")

    cand = run_phase(CANDIDATE, "inspect", scenario, copy_state(upgraded, base / "cand-i"))
    check(all(v["token_is_synthetic"] for v in cand.get("logins", {}).values())
          and len(cand.get("logins", {})) == len(login_keys(before)),
          "candidate reads each account's own token")
    check(cand.get("chain_ok") is True,
          f"candidate verifies the whole chain with the 0.1.0 secret ({cand.get('chain_reason')})")

    cand_flush = run_phase(CANDIDATE, "flush", scenario, copy_state(upgraded, base / "cand-f"))
    sent = cand_flush.get("sent", [])
    check(all(item["token_is_owners"] for item in sent),
          "candidate flush sends each award only with its owner's own token")
    expected_sent = {"switch": ("synthB", 3), "single": ("synthA", 3), "mixed": (None, 0)}[scenario]
    owners = {item["user"] for item in sent}
    if expected_sent[1]:
        check(len(sent) == expected_sent[1] and owners == {expected_sent[0]}
              and cand_flush.get("pending_remaining") == 0,
              f"candidate flushes all {expected_sent[1]} of {expected_sent[0]}'s awards "
              f"(sent {len(sent)} as {sorted(owners)})")
    else:
        check(not sent and cand_flush.get("pending_remaining") == len(after["awards"]),
              "candidate defers a two-owner queue and sends nothing")

    # ---- Rollback --------------------------------------------------------
    rolled = copy_state(upgraded, base / "rolled")
    back = run_phase(BASELINE, "inspect", scenario, rolled)
    if "error" in back:
        check(False, f"0.1.0 over the candidate's state: refused ({back['error']}); "
                     "an explicit refusal is acceptable only if it keeps the queue")
        check(fingerprint(rolled)["awards"] == after["awards"], "refusal kept the queue")
        return
    check(True, "0.1.0 opens the candidate's state (no quarantine, no refusal)")
    check(fingerprint(rolled)["awards"] == after["awards"]
          and fingerprint(rolled)["secret"] == before["secret"],
          "0.1.0 startup leaves the candidate's queue and secret as they were")
    check(back.get("chain_ok") is True,
          f"0.1.0 verifies the chain, candidate-signed rows included ({back.get('chain_reason')})")
    check(all(v["token_is_synthetic"] for v in back.get("logins", {}).values()),
          "0.1.0 reads each account's own token")
    check(back.get("flush_user") == own_inspect.get("flush_user")
          and back.get("flush_refusal") == own_inspect.get("flush_refusal"),
          f"0.1.0 picks the same flush account as over its own state "
          f"({back.get('flush_user') or back.get('flush_refusal')})")

    back_flush = run_phase(BASELINE, "flush", scenario, copy_state(upgraded, base / "rolled-f"))
    sent = back_flush.get("sent", [])
    check(all(item["token_is_owners"] for item in sent),
          "0.1.0 flush after rollback never sends an award with another account's token")
    own_sent = own_flush.get("sent", [])
    same_owners = {item["user"] for item in sent} <= {item["user"] for item in own_sent} | (
        {expected_sent[0]} if expected_sent[0] else set())
    check(bool(sent) == bool(own_sent) and same_owners,
          f"0.1.0 flush decision after rollback matches 0.1.0 over its own state "
          f"(sent {len(sent)} vs {len(own_sent)})")
    if not sent:
        rolled_f = fingerprint(base / "rolled-f")
        check(rolled_f["awards"] == after["awards"],
              "a deferred flush keeps every queued award for a later version")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--phase":
        args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
        phase_main(args["--app"], args["--phase"], args["--scenario"])
        return
    orchestrate()
    if failures:
        print(f"\n{len(failures)} failure(s)")
        sys.exit(1)
    print("\nstate-compat-test: all checks passed")


if __name__ == "__main__":
    main()
