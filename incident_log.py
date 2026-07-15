"""In-memory rolling log of risk-control trip events (halts, blocks, alarms).

NOTE: Render's filesystem is ephemeral (same caveat as risk_manager.STATE_PATH),
so this is deliberately NOT persisted to disk -- it's a plain module-level list
that resets on every deploy/restart. That's an accepted tradeoff for a
reporting/visibility feature: "incidents since the last deploy" is useful
enough without adding a second piece of durable state to get wrong. Revisit
if incident history needs to survive restarts.
"""

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

MAX_INCIDENTS = 100

_incidents: list[dict] = []


def record(kind: str, detail: str, **extra) -> None:
    """Append one incident. `kind` is a short machine-readable tag, e.g.
    'book_halt_daily', 'book_halt_monthly', 'underlying_block',
    'crypto_beta_block', 'gap_check_close', 'cash_carry_alarm'. Extra
    keyword args (e.g. symbol=...) are merged into the entry as-is.

    Pure append -- never raises, never touches disk, never blocks a caller;
    this must stay side-effect-free with respect to trading so it's safe to
    call from inside an already-firing risk control."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "detail": detail,
    }
    entry.update(extra)
    _incidents.append(entry)
    del _incidents[: -MAX_INCIDENTS]  # keep only the most recent MAX_INCIDENTS
    log.info("Incident logged: %s — %s", kind, detail)


def get_incidents() -> list[dict]:
    """Recent incidents, newest first."""
    return list(reversed(_incidents))
