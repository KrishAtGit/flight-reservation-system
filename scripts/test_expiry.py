"""
Expiry system demo.
Run: python scripts/test_expiry.py
"""

import asyncio
import logging
import sys
import os
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select
from app.core.config import settings
from app.models.models import Flight, Seat, User, SeatHold, SeatStatus
from app.services.booking import BookingService
from app.services.expiry import ExpiryWorker
from datetime import timedelta, datetime, timezone


async def main():
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)

    seat_id = None

    async with SessionLocal() as db:
        # ── Setup ────────────────────────────────────────────────────
        flight = (await db.execute(select(Flight).where(Flight.flight_number == "QF401"))).scalar_one()
        seat = (await db.execute(
            select(Seat)
            .where(Seat.flight_id == flight.id)
            .where(Seat.status == SeatStatus.AVAILABLE)
            .limit(1)
        )).scalar_one()
        seat_id = seat.id

        user = (await db.execute(select(User).where(User.email == "expiry_test@example.com"))).scalar_one_or_none()
        if not user:
            user = User(id=uuid.uuid4(), email="expiry_test@example.com", full_name="Expiry Test", hashed_password="x")
            db.add(user)
            await db.commit()

        print(f"\n── Flight:  {flight.flight_number} {flight.origin}→{flight.destination}")
        print(f"── Seat:    {seat.seat_label} [{seat.seat_class.value}]")
        print(f"── Status:  {seat.status.value}")

        # ── Create hold with 15s TTL ─────────────────────────────────
        short_expiry = datetime.now(timezone.utc) + timedelta(seconds=15)

        async with db.begin_nested():
            seat_row = (await db.execute(
                select(Seat).where(Seat.id == seat.id).with_for_update()
            )).scalar_one()
            seat_row.status = SeatStatus.HELD
            seat_row.version += 1

            hold = SeatHold(
                id=uuid.uuid4(),
                seat_id=seat.id,
                user_id=user.id,
                flight_id=flight.id,
                expires_at=short_expiry,
                is_active=True,
            )
            db.add(hold)

        await db.commit()

        print(f"\n[+] Hold created: {hold.id}")
        print(f"    Expires at:    {hold.expires_at.strftime('%H:%M:%S')} (15 seconds from now)")

        # ── Register with heap ───────────────────────────────────────
        worker = ExpiryWorker(SessionLocal, interval_seconds=60)
        worker.register_hold(hold.id, seat.id, user.id, hold.expires_at)

        print(f"\n[+] Heap state: {worker.heap}")
        print(f"    Next expiry: {worker.heap.peek_next().expires_at.strftime('%H:%M:%S')}")
        print(f"\n── Seat status now: held  (should be 'held')")

        # ── Wait ─────────────────────────────────────────────────────
        print("\n[~] Waiting 20 seconds for hold to expire...")
        for i in range(20, 0, -5):
            await asyncio.sleep(5)
            print(f"    {i - 5}s remaining...")

        # ── Fire worker ──────────────────────────────────────────────
        print("\n[+] Firing expiry worker tick...")
        expired = worker.heap.pop_expired()
        print(f"    Heap popped {len(expired)} expired entries")
        for e in expired:
            print(f"    → hold={e.hold_id} seat={e.seat_id}")

        await worker.run_once()

    # ── Verify in a fresh session ────────────────────────────────────
    async with SessionLocal() as fresh_db:
        fresh_seat = (await fresh_db.execute(
            select(Seat).where(Seat.id == seat_id)
        )).scalar_one()
        print(f"\n── Seat status now: {fresh_seat.status.value}  (should be 'available')")

        if fresh_seat.status == SeatStatus.AVAILABLE:
            print("\n✓ Expiry system works correctly.\n")
        else:
            print("\n✗ Seat was not released — check logs.\n")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())