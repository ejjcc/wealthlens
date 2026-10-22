#!/usr/bin/env python3
"""Static IBKR API client IDs and local process-slot locks.

Order clients are deliberately stable: IBKR requires the same ``clientId``
that placed an API order to modify or cancel it.  Read-only jobs also use named
IDs so a new cron/script cannot silently collide with an order owner.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import time
from pathlib import Path
from typing import Dict, Iterator, Optional


CLIENT_IDS: Dict[str, int] = {
    "live_fills": 70,
    "live_positions": 71,
    "live_account": 72,
    "check_leverage": 75,
    "option_risk": 76,
    "price_alert": 77,
    "option_roll": 79,
    "option_chain": 80,
    # Fixed order owners: never allocate these dynamically.
    "option_order": 90,
    "stock_order": 91,
    # Read-only clients moved away from the order-owner IDs.
    "stock_status": 92,
    "quote_option": 93,
    "stock_quote": 94,
}

ORDER_OWNER_IDS = {"option_order": 90, "stock_order": 91}
RETIRED_CLIENT_IDS = {97}  # retired one-shot script; do not reuse
DEFAULT_LOCK_DIR = Path.home() / "ibc" / "run"


class ClientIdConfigurationError(RuntimeError):
    """The static role map contains a duplicate or unsafe assignment."""


class ProcessSlotBusy(RuntimeError):
    """Another local process still owns the requested API/job slot."""


def validate_client_ids(mapping: Optional[Dict[str, int]] = None) -> Dict[str, int]:
    mapping = dict(CLIENT_IDS if mapping is None else mapping)
    reverse: Dict[int, list[str]] = {}
    for role, value in mapping.items():
        if not isinstance(value, int) or value < 0:
            raise ClientIdConfigurationError(f"invalid clientId for {role}: {value!r}")
        reverse.setdefault(value, []).append(role)
    duplicates = {value: roles for value, roles in reverse.items() if len(roles) > 1}
    if duplicates:
        raise ClientIdConfigurationError(f"duplicate IBKR clientId assignments: {duplicates}")
    retired = sorted(set(reverse).intersection(RETIRED_CLIENT_IDS))
    if retired:
        raise ClientIdConfigurationError(f"retired IBKR clientId(s) reused: {retired}")
    for role, fixed_id in ORDER_OWNER_IDS.items():
        if mapping.get(role) != fixed_id:
            raise ClientIdConfigurationError(
                f"order owner {role} must remain on clientId {fixed_id}"
            )
    return mapping


validate_client_ids()


def client_id_for(role: str) -> int:
    try:
        return CLIENT_IDS[role]
    except KeyError as exc:
        raise ClientIdConfigurationError(f"unknown IBKR client role: {role}") from exc


def _safe_lock_name(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-.")
    if not value:
        raise ValueError("lock name is empty")
    return value


@contextlib.contextmanager
def process_slot(
    name: str,
    *,
    wait_seconds: float = 0.0,
    lock_dir: Optional[os.PathLike] = None,
    poll_seconds: float = 0.05,
) -> Iterator[Path]:
    """Hold a crash-safe local ``flock`` for the complete job lifecycle."""

    directory = Path(lock_dir or DEFAULT_LOCK_DIR).expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{_safe_lock_name(name)}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(fd, "r+")
    deadline = time.monotonic() + max(0.0, wait_seconds)
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise ProcessSlotBusy(f"process slot is busy: {name}") from exc
                time.sleep(max(0.01, poll_seconds))
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} acquired={int(time.time())}\n")
        handle.flush()
        yield path
    finally:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def api_client_slot(
    client_id: int, *, wait_seconds: float = 0.0, lock_dir: Optional[os.PathLike] = None
):
    return process_slot(
        f"api-client-{int(client_id)}",
        wait_seconds=wait_seconds,
        lock_dir=lock_dir,
    )


__all__ = [
    "CLIENT_IDS",
    "ClientIdConfigurationError",
    "ORDER_OWNER_IDS",
    "ProcessSlotBusy",
    "RETIRED_CLIENT_IDS",
    "api_client_slot",
    "client_id_for",
    "process_slot",
    "validate_client_ids",
]
