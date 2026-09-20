#!/usr/bin/env python3
"""Host network fixtures for the Leaf-probed RAOfflineProxy network module.

Exercises the assembled (locked-upstream + Leaf patches) network.py:
- request-path probes make exactly one urlopen at the Leaf 500 ms timeout;
- forced probes (background monitor/startup) retry up to the upstream count
  with upstream's retry delay, never more than the per-attempt timeout each;
- failed probes are cached, so a dead network is not re-probed before every
  RetroArch request;
- the tethered-interface carrier fallback treats operstate unknown + carrier 1
  as active and rejects carrier-less unknown interfaces;
- probe failure logs carry no query secrets.

Run: python3 scripts/network-fixture-test.py
"""
from __future__ import annotations

import logging
import pathlib
import sys
import types
import urllib.error
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "build" / "mlp1" / "app" / "raofflineproxy"

failures = []


def check(ok: bool, what: str) -> None:
    if not ok:
        failures.append(what)
        print(f"FAIL {what}")
    else:
        print(f"ok   {what}")


if not APP.is_dir():
    sys.exit(f"assembled app missing: {APP} (run scripts/assemble-app.sh)")
sys.path.insert(0, str(APP.parent))

from raofflineproxy import network  # noqa: E402


def fake_urlopen_factory(outcomes: list, record: dict):
    def fake_urlopen(request, timeout=None, context=None):
        record.setdefault("calls", []).append({"timeout": timeout, "url": request.full_url})
        outcome = outcomes[len(record["calls"]) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        status = outcome
        if not (200 <= status < 500):
            raise urllib.error.HTTPError(request.full_url, status, "err", {}, None)

        class Resp:
            def __init__(self, code):
                self.status = code
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
        return Resp(status)
    return fake_urlopen


def fresh_tracker():
    # checked_at == 0.0 is the tracker's "unknown" state, which always allows
    # a probe; tests set explicit marks when they exercise the cache.
    network._reachability_tracker.mark(True, 0.0)


# 1. Request-path probe (force=False): exactly one attempt, 500 ms timeout.
fresh_tracker()
record = {}
outcomes = [urllib.error.URLError("boom")]
with mock.patch.object(network, "has_active_network_interface", return_value=True), \
     mock.patch.object(network.urllib.request, "urlopen",
                       fake_urlopen_factory(outcomes, record)), \
     mock.patch.object(network.time, "sleep") as slept:
    result = network.probe_retroachievements({}, "ua", force=False, now=5.0)
check(result is False, "failed request-path probe reports unreachable")
check(len(record["calls"]) == 1, f"request-path probe makes one attempt (got {len(record['calls'])})")
check(record["calls"][0]["timeout"] == 0.5,
      f"request-path probe timeout is 0.5s (got {record['calls'][0]['timeout']})")
check(not slept.called, "request-path probe never sleeps")

# 2. Forced probe: retries up to PROBE_ATTEMPTS with the retry delay, but only
# in that background shape (called only by the connectivity monitor with
# force=True).
fresh_tracker()
record = {}
outcomes = [urllib.error.URLError("one"), urllib.error.URLError("two"), 200]
with mock.patch.object(network, "has_active_network_interface", return_value=True), \
     mock.patch.object(network.urllib.request, "urlopen",
                       fake_urlopen_factory(outcomes, record)), \
     mock.patch.object(network.time, "sleep") as slept:
    result = network.probe_retroachievements({}, "ua", force=True, now=9.0)
check(result is True, "forced probe recovers on the third attempt")
check(len(record["calls"]) == 3, f"forced probe retried as designed (got {len(record['calls'])})")
check(all(c["timeout"] == 0.5 for c in record["calls"]),
      "every forced attempt is individually bounded to 0.5s")
check(slept.call_count == 2 and all(c.args[0] == 1.0 for c in slept.call_args_list),
      "retry sleeps are upstream's bounded 1.0s, on the background path only")

# 3. Failed probes are cached: should_probe returns False inside the interval
# after a failure, so the emulator never pays the timeout twice in a row.
fresh_tracker()
network.mark_retroachievements_unreachable(100.0)
check(network.should_probe_retroachievements(force=False, now=110.0) is False,
      "failed probe is cached (no retry inside the interval)")
check(network.should_probe_retroachievements(force=False, now=131.0) is True,
      "cached failure expires after the interval")
check(network.should_probe_retroachievements(force=True, now=110.0) is True,
      "forced background probes are exempt from the cache")

# 4. Tethered-interface detection: operstate unknown falls back to carrier.
import tempfile
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    eth0 = root / "eth0"
    eth0.mkdir()
    (eth0 / "operstate").write_text("unknown\n")
    (eth0 / "carrier").write_text("1\n")
    check(network._interface_is_active(eth0) is True,
          "operstate unknown + carrier 1 counts as active (tethered interface)")
    (eth0 / "carrier").write_text("0\n")
    check(network._interface_is_active(eth0) is False,
          "operstate unknown + carrier 0 is inactive")
    (eth0 / "operstate").write_text("up\n")
    check(network._interface_is_active(eth0) is True, "operstate up is active")
    (eth0 / "operstate").write_text("down\n")
    check(network._interface_is_active(eth0) is False, "operstate down is inactive")
    (eth0 / "operstate").unlink()
    check(network._interface_is_active(eth0) is False,
          "unreadable operstate is inactive, not an exception")

    # 5. A device whose only interface is a carrier-less unknown reports no
    # active interface, so the probe short-circuits without any urlopen.
    lo = root / "lo"
    lo.mkdir()
    with mock.patch.object(network, "Path", network.Path) as _p:
        pass  # sanity: Path itself is not touched by this fixture
    with mock.patch.object(network, "Path") as fake_path:
        fake_path.side_effect = lambda p: root if p == "/sys/class/net" else pathlib.Path(p)
        fresh_tracker()
        record = {}
        with mock.patch.object(network.urllib.request, "urlopen",
                               fake_urlopen_factory([200], record)):
            result = network.probe_retroachievements({}, "ua", force=False, now=7.0)
        check(result is False and not record.get("calls"),
              "no active interface short-circuits the probe without urlopen")

# 6. Probe failure logs contain no query secrets (the reason is a transport
# error, and the logged URL elsewhere goes through redact_query_tokens).
from raofflineproxy import utils  # noqa: E402
check("t=<token>" in utils.redact_query_tokens("https://x/dorequest.php?u=a&t=deadbeef"),
      "query token redacted")
check("t=<token>" in utils.redact_query_tokens("POST failed url=https://x/?t=deadbeef&u=a"),
      "log-line token redacted")
check("host=127.0.0.1" in utils.redact_query_tokens("at=1 host=127.0.0.1 port=8080 checked_at=2"),
      "host=/checked_at= are not shredded as token fragments")

if failures:
    print(f"\n{len(failures)} network fixture FAILURE(S)")
    sys.exit(1)
print("\nnetwork-fixture-test: ok")
