"""Leaf supervised foreground entry point for RAOfflineProxy.

Runs as ``<python> -m raofflineproxy.leaf_service`` under the Leaf service
supervisor (`bin/service-run` in the pak sets up the environment; this module
only consumes it). Deliberately small: validation, signals, and delegation to
the patched ``run_proxy_service()``. No PID files, no daemonization, no
RetroArch config access, no boot hooks — those belong to Jawaka/Leaf.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading

from .config import configure_logging
from .proxy_service import run_proxy_service

# Listener, origin, and image-cache policy are fixed in v1; the pak does not
# read a user-editable config file for any of them.
FIXED_CONFIG = {
    "proxy_host": "127.0.0.1",
    "proxy_port": 8080,
    "upstream_host": "https://retroachievements.org",
    "cache_images": False,
}

REQUIRED_ENV = ("RAOFFLINEPROXY_CONFIG_DIR",)

# Escape hatch for host-side spike runs outside the supervisor. Never set by
# the pak.
SKIP_LEASE_ENV = "RAOFFLINEPROXY_TEST_SKIP_LEASE"

LOGGER = logging.getLogger("raofflineproxy")


def _validate_lease_fd() -> int | None:
    """Validate UMKR_SERVICE_LEASE_FD and retain the descriptor.

    The supervisor exports the lease fd (3) to supervised services; refusing
    to run without it prevents an accidentally manual start from binding the
    port outside supervision. Returns the fd to keep it referenced (and thus
    open) for the process lifetime.
    """
    raw = os.environ.get("UMRK_SERVICE_LEASE_FD")
    if not raw:
        if os.environ.get(SKIP_LEASE_ENV):
            return None
        raise RuntimeError(
            "UMRK_SERVICE_LEASE_FD is not set; this entry point must run under "
            "the Leaf service supervisor"
        )
    try:
        fd = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"invalid UMRK_SERVICE_LEASE_FD: {raw!r}") from exc
    try:
        os.fstat(fd)
    except OSError as exc:
        raise RuntimeError(
            f"UMRK_SERVICE_LEASE_FD={fd} is not an open descriptor"
        ) from exc
    return fd


def main() -> int:
    missing = [key for key in REQUIRED_ENV if not os.environ.get(key)]
    if missing:
        print(
            "missing required environment: " + ", ".join(missing),
            file=sys.stderr,
        )
        return 2

    try:
        lease_fd = _validate_lease_fd()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    stop_event = threading.Event()

    def handle_signal(_signum: int, _frame: object) -> None:
        stop_event.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, handle_signal)

    configure_logging()
    LOGGER.info(
        "Leaf service starting pid=%s lease_fd=%s config_dir=%s",
        os.getpid(),
        lease_fd if lease_fd is not None else "(skipped)",
        os.environ.get("RAOFFLINEPROXY_CONFIG_DIR"),
    )

    try:
        run_proxy_service(FIXED_CONFIG, stop_event)
    except Exception:
        LOGGER.exception("Proxy service terminated with an exception")
        return 1

    LOGGER.info("Leaf service stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
