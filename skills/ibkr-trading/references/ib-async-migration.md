# IBKR Python Client: ib_insync → ib_async

## Decision

Keep the transport and execution model unchanged — local IB Gateway / TWS
socket API, the same host, port, route, order flags, confirmation card, quota
and post-order reconciliation rules. Only the Python convenience client moved
from the unmaintained `ib_insync` package to pinned `ib_async==2.1.0`, via the
`scripts/_ib.py` adapter:

- prefers `ib_async` when importable, otherwise falls back to `ib_insync`
- `IBKR_PYTHON_BACKEND=ib_insync|ib_async` forces a backend (rollback lever)
- all scripts, including order execution, import IB classes through `_ib.py`;
  never mix backend classes in one process

## Reproducible Python 3.12 environment

Do not reuse macOS `/usr/bin/python3` (Python 3.9). Create an isolated
environment at the repo root (`.venv*/` is git-ignored):

```bash
/opt/homebrew/bin/python3.12 -m venv .venv-ibkr
.venv-ibkr/bin/python -m pip install --upgrade pip
.venv-ibkr/bin/python -m pip install -r skills/ibkr-trading/requirements.txt

IBKR_PYTHON_BACKEND=ib_async \
  .venv-ibkr/bin/python skills/ibkr-trading/scripts/tests/test_ib_async_compat.py
```

Keep the requirement exactly pinned; do not switch to an unreleased/dev or
protobuf branch without rerunning the paper matrix below.

## Known semantic differences

### `qualifyContractsAsync`

This is the first required guard, not a cosmetic type difference:

- `ib_insync` omits unknown or ambiguous contracts from its result.
- `ib_async` 2.x keeps one result slot per input and uses `None` when a
  contract is unknown or ambiguous.
- Therefore `if not qualified: ...; qualified[0]` is unsafe after a naive
  import replacement: `[None]` is truthy.

The adapter's `IB.qualifyContractsAsync()` filters `None` to preserve legacy
behaviour. New order and routing code should use
`qualify_contracts_strict()`, which refuses partial qualification. Existing
qualification call sites still need an explicit review; the adapter cannot
prove that an ignored return value was intended.

`ib_async` may also resolve a single matching security type from an otherwise
ambiguous response, where legacy `ib_insync` omitted the contract. Shadow-test
contract identity (`conId`, exchange, expiry, multiplier), not just list
length.

### Runtime behaviour still requiring live/paper verification

Offline constructor/import tests cannot validate:

- startup fetch timing and account/order state hydration
- event ordering for `Trade`, `Ticker`, fills and open orders
- ownership/binding of orders placed under another client ID
- reconnect and timeout behaviour
- delayed/frozen/live market-data transitions
- SMART outside-RTH and explicit OVERNIGHT exchange routing
- Price Management Algo, combo legs, modify/cancel and partial fills

## Validation performed before cutover

- **Read-only parity**: positions, account summary, portfolio rows, contract
  qualification and option-chain definitions compared field by field across
  both backends against the same live Gateway session — identical except the
  negotiated protocol version (`ib_insync` caps at 176, `ib_async` 2.1.0
  negotiates 178).
- **Paper execution matrix**: `scripts/tests/paper_exec_matrix.py` (refuses any
  non-`DU` account, port 4002 hardcoded) covers qualification semantics
  (including strict partial-refusal), stock LMT place/modify/cancel with
  outsideRth + PMA echo, explicit OVERNIGHT route, single-leg option, 2-leg
  combo and a marketable 1-share SPY fill roundtrip. Reports matched across
  backends after scrubbing orderId and fill-price drift. Paper has no
  real-time subscription, so market-data-dependent cases use delayed type 3.
- Not exercised: partial fills and reconnect during an active order.

Offline migration tests must not open port 4001, start Gateway or submit an
order. Live/paper validation requires explicit human approval and must retain
all confirmation and execution-safety rules.
