"""Populate a Supabase project with a world worth demonstrating.

    uv run python -m scripts.seed

Idempotent: carriers upsert on their phone number and the order upserts on its reference, so
running it between demo takes resets the world without duplicating it. It writes through
``store/`` rather than issuing SQL, so the same code path the API uses is the one that proves
the database is reachable -- a seed that talks to Postgres directly can succeed while the
application still cannot.

The demurrage clock is computed from today, not stored in the file. A seed with a frozen last
free day is expired by the second day of a build, and a countdown reading "-4 days" teaches a
judge nothing.

Phone numbers come from SEED_CARRIER_PHONE_1..3 and fall back to placeholders. A live demo
needs three real handsets; committing them would let any checkout dial real people.

OWNER: Track C.
"""

import asyncio
import os
import sys
from datetime import UTC, date, datetime, timedelta

from app.config import get_settings
from app.domain import Carrier, Order, OrderStatus
from app.store import StoreUnavailable, SupabaseStore

#: The order deliberately carries no mandate. A human grants the ceiling through
#: POST /api/orders/{id}/mandate, and that step being separate and attributable is the shape
#: of the whole system -- seeding a cap would skip the only moment authority enters.
REFERENCE = "OP-MZO-0001"


def _phone(slot: int, fallback: str) -> str:
    return os.environ.get(f"SEED_CARRIER_PHONE_{slot}", fallback)


def _carriers() -> list[Carrier]:
    """Three who disagree, and one who is not on file.

    Three carriers quoting the same number prove nothing: the comparison has to choose and a
    human has to see why. The fourth exists so the refusal is demonstrable -- it says the
    right things, and the agent still declines, because Volta onboards nobody by phone.
    """
    return [
        Carrier(
            id="",
            name="Fletes del Pacifico",
            phone=_phone(1, "+523141000001"),
            contact_name="Luis Ramirez",
            email="luis@fletespacifico.test",
            persona=(
                "Cheap and slow. Quotes near 9,800 MXN and needs 48 hours notice. Wins on "
                "price, loses the window when the last free day is close."
            ),
        ),
        Carrier(
            id="",
            name="Autolineas Manzanillo",
            phone=_phone(2, "+523141000003"),
            contact_name="Jorge Mendoza",
            email="jorge@autolineasmzo.test",
            persona=(
                "Fast and expensive. Quotes near 12,400 MXN and can be at the terminal in 12 "
                "hours. The right answer only when demurrage costs more than the difference."
            ),
        ),
        Carrier(
            id="",
            name="Transportes Colima",
            phone=_phone(3, "+523141000002"),
            contact_name="Ana Beltran",
            email="ana@transportescolima.test",
            persona=(
                "Does not answer. Reliable when reached and quotes near 10,600 MXN, but the "
                "RFQ usually times out here. Three dials and two answers is the normal case."
            ),
        ),
        Carrier(
            id="",
            name="Transportes Fantasma",
            phone="+523141000009",
            contact_name="Unknown",
            is_on_file=False,
            persona=(
                "Not on file. Says the right things and may quote a very good number. The "
                "agent must decline to quote and escalate."
            ),
        ),
    ]


def _order(today: date, now: datetime) -> Order:
    return Order(
        id="",
        reference=REFERENCE,
        status=OrderStatus.RECEIVED,
        origin="Contecon Manzanillo",
        destination="Av. Lopez Mateos 1200, Guadalajara, Jalisco",
        cargo="Textiles",
        equipment="40-foot container chassis",
        weight="18400 kg",
        container_number="MSCU1234566",
        discharged_at=now - timedelta(days=2),
        free_days=5,
        last_free_day=today + timedelta(days=3),
        delivery_deadline=now + timedelta(days=4),
        payload={
            "bill_of_lading": "MEDUMZ0099231",
            "vessel": "MSC Rania",
            "voyage": "FT534A",
            "ocean_carrier": "MSC",
            "packages": 620,
            "destination_postal_code": "44940",
        },
    )


async def seed() -> None:
    store = SupabaseStore(get_settings())
    now = datetime.now(UTC)

    for carrier in _carriers():
        carrier_id = await store.save_carrier(carrier)
        on_file = "on file" if carrier.is_on_file else "NOT on file"
        print(f"  carrier  {carrier.name:<24} {carrier.phone:<16} {on_file:<11} {carrier_id}")

    order_id = await store.save_order(_order(now.date(), now))
    stored = await store.order(order_id)
    assert stored is not None
    remaining = (stored.last_free_day - now.date()).days if stored.last_free_day else None
    print(f"  order    {stored.reference:<24} {stored.container_number:<16} {order_id}")
    print(f"           last free day {stored.last_free_day} ({remaining} days remaining)")
    print(f"           mandate_version {stored.mandate_version} - nothing is authorized yet")

    assert stored.cap is None, "the seed must not grant a mandate; a human does that"


def main() -> int:
    try:
        asyncio.run(seed())
    except StoreUnavailable as exc:
        print(f"seed: {exc}", file=sys.stderr)
        print("set SUPABASE_URL and SUPABASE_SECRET_KEY in backend/.env", file=sys.stderr)
        return 1
    print("\nseeded. Next: set a mandate, then open the market.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
