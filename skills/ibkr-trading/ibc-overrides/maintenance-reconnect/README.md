# IBC maintenance reconnect override

IBC 3.23.0 treats every Gateway dialog beginning with `Connection to server
failed` as a cold restart. During IBKR's APAC maintenance reset, Gateway can produce
the narrower transport error:

`SSLHandshakeException: Remote host terminated the handshake`

at about 05:00 China time while Gateway is already authenticated. A cold
restart discards the weekly authenticated session and causes a fresh 2FA prompt
when monitoring starts Gateway again that evening.

This override changes only that exact signature, and only when all three gates
hold:

1. IBC's login state is already `LOGGED_IN`;
2. local time is inside `GatewayReconnectWithoutColdRestartWindows`;
3. after whitespace normalization, the dialog is exactly the transient TLS
   signature above, optionally followed only by the observed `IBKR GATEWAY`
   footer.

The handler then clicks `OK` and leaves Gateway running so its native reconnect
can recover the existing session. If the button cannot be clicked, it falls
back to cold restart. Invalid or partly invalid configuration, a fresh login,
token expiry (`Soft token=0`), appended error text, any other message, and any
time outside the configured windows all retain upstream cold-restart behaviour.

Build and test without touching the deployed jar:

```bash
skills/ibkr-trading/ibc-overrides/maintenance-reconnect/build.sh \
  /path/to/unmodified/IBC-3.23.0/IBC.jar \
  /tmp/IBC-patched.jar
```

The first argument must be the pinned, unmodified IBC 3.23.0 jar (the deployed
`~/ibc/IBC.jar` is already patched and is intentionally rejected).

Deployment is an atomic replacement of `~/ibc/IBC.jar` after a timestamped
backup. It does not require restarting the currently running Gateway: its open
jar inode remains unchanged, and the patched class is picked up by the next
normal 23:45 soft restart. Reapply and retest this override after any IBC
upgrade. The builder accepts only the pinned, unmodified IBC 3.23.0 jar hash and
refuses an upgraded, unknown, or already-patched input.

Reference: upstream IBC issue 142 records that dismissing the maintenance-time
connection dialog lets TWS retry and reconnect without a cold restart:
https://github.com/IbcAlpha/IBC/issues/142
