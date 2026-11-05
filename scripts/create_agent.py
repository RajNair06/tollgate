"""Provision an agent: creates agents + policies rows, prints the API key ONCE.

Usage:
    uv run python scripts/create_agent.py <name> [per_txn] [rolling_24h] [threshold] [ttl_min] [actions...]

Defaults: per_txn=100.00 rolling=500.00 threshold=50.00 ttl=60 actions=refund
Example:
    uv run python scripts/create_agent.py refund-bot 25 100 20 30 refund capture
"""

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.api_keys import generate_api_key  # noqa: E402
from gateway.db import session_scope  # noqa: E402
from gateway.models import Agent, Policy  # noqa: E402


def main() -> None:
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)

    name = args[0]
    per_txn = Decimal(args[1]) if len(args) > 1 else Decimal("100.00")
    rolling = Decimal(args[2]) if len(args) > 2 else Decimal("500.00")
    threshold = Decimal(args[3]) if len(args) > 3 else Decimal("50.00")
    ttl = int(args[4]) if len(args) > 4 else 60
    allowed = args[5:] or ["refund"]

    plaintext, key_hash, prefix = generate_api_key()

    with session_scope() as session:
        agent = Agent(name=name, api_key_hash=key_hash, api_key_prefix=prefix)
        session.add(agent)
        session.flush()  # assign agent.id
        session.add(Policy(
            agent_id=agent.id,
            allowed_actions=allowed,
            per_txn_cap=per_txn,
            rolling_24h_cap=rolling,
            approval_threshold=threshold,
            approval_ttl_minutes=ttl,
        ))
        agent_id = agent.id

    print(f"Agent {name!r} created (id={agent_id}).")
    print(f"Allowed actions: {allowed}")
    print(f"Caps: per_txn={per_txn} rolling_24h={rolling} approval_threshold={threshold} ttl={ttl}min")
    print()
    print("API KEY — store it now, it is never shown or stored again:")
    print(f"  {plaintext}")


if __name__ == "__main__":
    main()
