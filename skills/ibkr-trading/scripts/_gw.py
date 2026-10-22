#!/usr/bin/env python3
"""
IB Gateway 实时连接辅助 - 内部模块（不直接执行）

提供 IB 客户端（经 _ib 选择 ib_async / ib_insync 后端）连接到本地 IB Gateway 的统一封装。
被实时查询脚本（live_*.py）共享使用。

环境变量（可选，默认值通常够用）:
- IBKR_HOST     默认 127.0.0.1
- IBKR_PORT     默认 4001
- IBKR_CLIENT_ID_BASE  仅供旧调用兼容；新代码必须使用命名 role

约束:
- IB Gateway 必须运行在 IBKR_PORT 上（4001 默认 live, 4002 paper）
- readonly=True 避免下单弹窗
"""
import os
import socket
import sys
import asyncio
import json
import subprocess
import time
from typing import Optional

from _client_ids import api_client_slot, client_id_for


def _check_alive(host: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2)
    try:
        sock.connect((host, port))
        return True
    except (ConnectionRefusedError, OSError):
        return False
    finally:
        sock.close()


# --- cron on-demand Gateway autostart (2026-07-20) ---------------------------
# Gateway 无后台自动重启（LaunchAgent 定时触发已移除）；需要 live 数据的 cron
# 通过 ensure_gateway() 按需 kickstart。防 2FA 推送轰炸的三重约束：
#   1) 夜间 02:00-09:00 Asia/Shanghai 不自动启动（静默）
#   2) 40 分钟节流：一次尝试后 40 分钟内其他 cron 不再重复触发
#   3) 单次故障最多自动尝试 2 次，之后退避等人工「启动」；端口恢复后计数复位
_AUTOSTART_STATE = os.path.expanduser("~/ibc/run/cron_autostart.json")
_AUTOSTART_THROTTLE = 40 * 60
_AUTOSTART_MAX_ATTEMPTS = 2
_QUIET_START_HOUR, _QUIET_END_HOUR = 2, 9  # Asia/Shanghai == local time
_SYNC_PROBE_TIMEOUT = 10  # seconds; healthy Gateway answers positions in <1s


def _autostart_state():
    try:
        with open(_AUTOSTART_STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_autostart_state(state):
    os.makedirs(os.path.dirname(_AUTOSTART_STATE), exist_ok=True)
    with open(_AUTOSTART_STATE, "w") as f:
        json.dump(state, f)


def ensure_gateway(wait_timeout: int = 300) -> str:
    """On-demand Gateway start for cron consumers.

    Returns:
      'up'         port is open (possibly after a successful autostart)
      'timeout'    autostart attempted (2FA push likely sent) but port not up yet
      'suppressed' offline and no attempt made (quiet hours / throttle / backoff)
    """
    host, port, _ = get_gateway_config()
    if _check_alive(host, port):
        state = _autostart_state()
        if state.get("failures"):
            _save_autostart_state({})  # healthy again -> reset backoff
        return "up"

    hour = time.localtime().tm_hour
    if _QUIET_START_HOUR <= hour < _QUIET_END_HOUR:
        return "suppressed"

    state = _autostart_state()
    now = time.time()
    if now - state.get("last_attempt", 0) < _AUTOSTART_THROTTLE:
        return "suppressed"
    if state.get("failures", 0) >= _AUTOSTART_MAX_ATTEMPTS:
        return "suppressed"

    state = {"last_attempt": now, "failures": state.get("failures", 0) + 1}
    _save_autostart_state(state)

    kick = ["launchctl", "kickstart", "-k",
            f"gui/{os.getuid()}/com.user.ibc-gateway"]
    deadline = now + wait_timeout
    next_kick = 0.0
    while time.time() < deadline:
        # 周期性重触发：kickstart 对健康/等 2FA 的实例是幂等 no-op，
        # 但当上一实例卡死被回收后，需要第二次 kickstart 才会拉起新实例
        if time.time() >= next_kick:
            subprocess.run(kick, capture_output=True)
            next_kick = time.time() + 90
        if _check_alive(host, port):
            _save_autostart_state({})
            return "up"
        time.sleep(5)
    return "timeout"


def get_gateway_config():
    return (
        os.getenv("IBKR_HOST", "127.0.0.1"),
        int(os.getenv("IBKR_PORT", "4001")),
        int(os.getenv("IBKR_CLIENT_ID_BASE", "70")),
    )


async def connect(
    client_id_offset: int = 0,
    *,
    role: Optional[str] = None,
    client_id: Optional[int] = None,
    slot_wait_seconds: float = 0.0,
    ib_factory=None,
):
    """Connect to IB Gateway. Caller is responsible for disconnecting.

    Prefer ``role=...``. A crash-safe local lock protects the selected client
    ID from overlapping cron/manual processes until ``ib.disconnect()``.

    Returns a connected IB instance from the backend selected by `_ib`
    (ib_async preferred, ib_insync fallback; `IBKR_PYTHON_BACKEND` overrides).
    """
    if ib_factory is None:
        try:
            from _ib import IB as ib_factory
        except ModuleNotFoundError as exc:
            print(f"ERROR: IB client backend unavailable: {exc}", file=sys.stderr)
            sys.exit(1)

    host, port, base_cid = get_gateway_config()
    if role is not None and client_id is not None:
        raise ValueError("use role or client_id, not both")
    selected_client_id = (
        client_id_for(role)
        if role is not None
        else int(client_id) if client_id is not None
        else base_cid + client_id_offset
    )
    if not _check_alive(host, port):
        print(
            f"ERROR: IB Gateway not reachable at {host}:{port}.\n"
            f"Start it through launchd (safe to re-run):\n"
            f"  launchctl kickstart -k gui/$(id -u)/com.user.ibc-gateway\n"
            f"Then complete 2FA on phone. Do NOT run ibcstart.sh directly "
            f"or spawn Gateway from a transient shell.",
            file=sys.stderr,
        )
        sys.exit(1)

    slot = api_client_slot(selected_client_id, wait_seconds=slot_wait_seconds)
    slot.__enter__()
    released = False

    def release_slot(*_args):
        nonlocal released
        if not released:
            released = True
            slot.__exit__(None, None, None)

    ib = ib_factory()
    try:
        await ib.connectAsync(
            host, port, clientId=selected_client_id, readonly=True
        )
    except BaseException:
        release_slot()
        raise
    ib.disconnectedEvent += release_slot
    # Keep callbacks/context alive for the lifetime of the IB object.
    ib._api_client_slot_release = release_slot
    ib._api_client_id = selected_client_id
    # A Gateway that lost its IBKR server session still accepts API sockets;
    # its startup sync only logs timeouts, leaving empty position caches that
    # callers would read as "no positions". Fail loudly instead.
    try:
        await asyncio.wait_for(ib.reqPositionsAsync(), _SYNC_PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        ib.disconnect()
        release_slot()
        raise ConnectionError(
            f"IB Gateway at {host}:{port} accepted the API connection but is not "
            f"synced with IBKR servers (positions request timed out). Restart it: "
            f"pkill -9 -f ibcalpha.ibc.IbcGateway; "
            f"launchctl kickstart -k gui/$(id -u)/com.user.ibc-gateway (phone 2FA)"
        ) from None
    return ib


def run(coro):
    """Synchronous wrapper for async main()."""
    return asyncio.run(coro)
