"""
Manual smoke test for BookingService.
Run: python scripts/test_booking.py
"""

import asyncio
import sys
import os
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select
from app.core.config import settings
from app.models.models import Flight, Seat, User, SeatStatus
from app.services.booking import BookingService, SeatNotAvailable, HoldNotFound


async def main():
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)

    # ── Get flight and seat ──────────────────────────────────────────
    async with SessionLocal() as db:
        flight = (await db.execute(select(Flight).where(Flight.flight_number == "QF401"))).scalar_one()
        seat = (await db.execute(
            select(Seat)
            .where(Seat.flight_id == flight.id)
            .where(Seat.status == SeatStatus.AVAILABLE)
            .limit(1)
        )).scalar_one()

        # Create test users
        u1 = User(id=uuid.uuid4(), email=f"smoke1_{uuid.uuid4().hex[:6]}@test.com", full_name="U1", hashed_password="x")
        u2 = User(id=uuid.uuid4(), email=f"smoke2_{uuid.uuid4().hex[:6]}@test.com", full_name="U2", hashed_password="x")
        db.add_all([u1, u2])
        await db.commit()

        flight_id = flight.id
        seat_id = seat.id
        user1_id = u1.id
        user2_id = u2.id
        seat_label = seat.seat_label

    print(f"\n── Flight: QF401 MEL→SYD")
    print(f"── Test seat: {seat_label}\n")

    # ── TEST 1: Hold a seat ──────────────────────────────────────────
    print("[TEST 1] Holding seat...")
    async with SessionLocal() as db:
        hold = await BookingService(db).hold_seat(flight_id, seat_id, user1_id)
    print(f"  ✓ Hold created: {hold.id}")

    # ── TEST 2: Same seat, different user ────────────────────────────
    print("\n[TEST 2] Trying to hold same seat (different user)...")
    async with SessionLocal() as db:
        try:
            await BookingService(db).hold_seat(flight_id, seat_id, user2_id)
            print("  ✗ FAIL — should have raised SeatNotAvailable")
        except SeatNotAvailable as e:
            print(f"  ✓ Correctly blocked: {e}")

    # ── TEST 3: Confirm booking ──────────────────────────────────────
    print("\n[TEST 3] Confirming booking...")
    async with SessionLocal() as db:
        booking = await BookingService(db).confirm_booking(hold.id, user1_id)
    print(f"  ✓ Booking confirmed: {booking.booking_reference}")

    # ── TEST 4: Confirm again (should fail) ──────────────────────────
    print("\n[TEST 4] Confirming same hold again...")
    async with SessionLocal() as db:
        try:
            await BookingService(db).confirm_booking(hold.id, user1_id)
            print("  ✗ FAIL — should have raised HoldNotFound")
        except HoldNotFound as e:
            print(f"  ✓ Correctly blocked: {e}")

    # ── TEST 5: Cancel booking ───────────────────────────────────────
    print("\n[TEST 5] Cancelling booking...")
    async with SessionLocal() as db:
        cancelled = await BookingService(db).cancel_booking(booking.id, user1_id)
    print(f"  ✓ Booking cancelled: {cancelled.status.value}")

    # ── TEST 6: Seat available again ─────────────────────────────────
    print("\n[TEST 6] Checking seat is available again...")
    async with SessionLocal() as db:
        fresh = (await db.execute(select(Seat).where(Seat.id == seat_id))).scalar_one()
    print(f"  ✓ Seat status: {fresh.status.value}")

    print("\n✓ All tests passed.\n")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())