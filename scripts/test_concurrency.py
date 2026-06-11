"""
Concurrency proof — Phase 4

Fires N concurrent coroutines all trying to hold the same seat simultaneously.
Uses asyncio.gather() to launch them truly in parallel against the DB.

Expected result:
  - Exactly 1 hold succeeds
  - N-1 get SeatNotAvailable
  - Zero double bookings — ever

This is your proof of correctness under concurrent load.
Run: python scripts/test_concurrency.py
"""

import asyncio
import sys
import os
import uuid
import time
from dataclasses import dataclass
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select, func
from app.core.config import settings
from app.models.models import Flight, Seat, User, SeatHold, SeatStatus
from app.services.booking import BookingService, SeatNotAvailable


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class AttemptResult:
    user_index: int
    success: bool
    hold_id: Optional[uuid.UUID] = None
    error: Optional[str] = None
    duration_ms: float = 0.0


# ---------------------------------------------------------------------------
# Single booking attempt
# ---------------------------------------------------------------------------

async def attempt_hold(
    session_factory: async_sessionmaker,
    flight_id: uuid.UUID,
    seat_id: uuid.UUID,
    user_id: uuid.UUID,
    user_index: int,
) -> AttemptResult:
    """One concurrent attempt to hold the seat."""
    start = time.perf_counter()

    async with session_factory() as db:
        service = BookingService(db)
        try:
            hold = await service.hold_seat(flight_id, seat_id, user_id)
            duration = (time.perf_counter() - start) * 1000
            return AttemptResult(
                user_index=user_index,
                success=True,
                hold_id=hold.id,
                duration_ms=duration,
            )
        except SeatNotAvailable as e:
            duration = (time.perf_counter() - start) * 1000
            return AttemptResult(
                user_index=user_index,
                success=False,
                error=str(e),
                duration_ms=duration,
            )
        except Exception as e:
            duration = (time.perf_counter() - start) * 1000
            return AttemptResult(
                user_index=user_index,
                success=False,
                error=f"UNEXPECTED: {type(e).__name__}: {e}",
                duration_ms=duration,
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_concurrency_test(n_concurrent: int = 50):
    engine = create_async_engine(
        settings.DATABASE_URL,
        pool_size=n_concurrent,
        max_overflow=10,
        echo=False,
    )
    SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with SessionLocal() as db:
        # ── Get flight ───────────────────────────────────────────────
        flight = (await db.execute(
            select(Flight).where(Flight.flight_number == "QF401")
        )).scalar_one()

        # ── Pick one available seat ──────────────────────────────────
        seat = (await db.execute(
            select(Seat)
            .where(Seat.flight_id == flight.id)
            .where(Seat.status == SeatStatus.AVAILABLE)
            .limit(1)
        )).scalar_one()

        print(f"\n{'='*60}")
        print(f"  CONCURRENCY TEST — {n_concurrent} simultaneous requests")
        print(f"{'='*60}")
        print(f"  Flight:  {flight.flight_number} {flight.origin}→{flight.destination}")
        print(f"  Seat:    {seat.seat_label} [{seat.seat_class.value}] ${seat.price}")
        print(f"  Target:  1 success, {n_concurrent - 1} blocked")
        print(f"{'='*60}\n")

        # ── Create N test users ──────────────────────────────────────
        print(f"[+] Creating {n_concurrent} test users...")
        users = []
        for i in range(n_concurrent):
            email = f"concurrent_user_{i}@test.com"
            existing = (await db.execute(
                select(User).where(User.email == email)
            )).scalar_one_or_none()

            if existing:
                users.append(existing)
            else:
                user = User(
                    id=uuid.uuid4(),
                    email=email,
                    full_name=f"Concurrent User {i}",
                    hashed_password="x",
                )
                db.add(user)
                users.append(user)

        await db.commit()
        print(f"    ✓ {len(users)} users ready\n")

        flight_id = flight.id
        seat_id = seat.id

    # ── Fire all N requests simultaneously ──────────────────────────
    print(f"[+] Firing {n_concurrent} concurrent hold requests...")
    print(f"    All targeting seat {seat.seat_label} at the same time\n")

    start_total = time.perf_counter()

    tasks = [
        attempt_hold(SessionLocal, flight_id, seat_id, users[i].id, i)
        for i in range(n_concurrent)
    ]

    # asyncio.gather runs all coroutines concurrently
    results: list[AttemptResult] = await asyncio.gather(*tasks)

    total_duration = (time.perf_counter() - start_total) * 1000

    # ── Analyse results ──────────────────────────────────────────────
    successes = [r for r in results if r.success]
    failures = [r for r in results if not r.success]
    unexpected = [r for r in failures if r.error and r.error.startswith("UNEXPECTED")]

    print(f"{'='*60}")
    print(f"  RESULTS")
    print(f"{'='*60}")
    print(f"  Total requests:    {n_concurrent}")
    print(f"  Succeeded:         {len(successes)}")
    print(f"  Blocked:           {len(failures)}")
    print(f"  Unexpected errors: {len(unexpected)}")
    print(f"  Total time:        {total_duration:.1f}ms")
    print(f"{'='*60}\n")

    # ── Show the winner ──────────────────────────────────────────────
    if successes:
        winner = successes[0]
        print(f"  Winner: User {winner.user_index} ({winner.duration_ms:.1f}ms)")
        print(f"  Hold:   {winner.hold_id}\n")

    # ── Show timing distribution ─────────────────────────────────────
    durations = sorted([r.duration_ms for r in results])
    print(f"  Timing (ms):")
    print(f"    Min:    {durations[0]:.1f}")
    print(f"    Median: {durations[len(durations)//2]:.1f}")
    print(f"    Max:    {durations[-1]:.1f}\n")

    # ── Verify DB state ──────────────────────────────────────────────
    print(f"[+] Verifying DB state...")
    async with SessionLocal() as db:
        active_holds = (await db.execute(
            select(func.count(SeatHold.id))
            .where(SeatHold.seat_id == seat_id)
            .where(SeatHold.is_active == True)
        )).scalar()

        seat_status = (await db.execute(
            select(Seat.status).where(Seat.id == seat_id)
        )).scalar()

    print(f"    Active holds on seat: {active_holds}  (should be 1)")
    print(f"    Seat status:          {seat_status.value}  (should be 'held')\n")

    # ── Verdict ──────────────────────────────────────────────────────
    print(f"{'='*60}")
    passed = (
        len(successes) == 1 and
        len(unexpected) == 0 and
        active_holds == 1 and
        seat_status == SeatStatus.HELD
    )

    if passed:
        print(f"  ✓ PASSED — Zero double bookings under {n_concurrent} concurrent requests")
        print(f"  ✓ SELECT FOR UPDATE serialised all {n_concurrent} transactions correctly")
    else:
        print(f"  ✗ FAILED")
        if len(successes) != 1:
            print(f"    Expected 1 success, got {len(successes)}")
        if unexpected:
            print(f"    Unexpected errors:")
            for r in unexpected:
                print(f"      User {r.user_index}: {r.error}")
        if active_holds != 1:
            print(f"    Expected 1 active hold, got {active_holds}")

    print(f"{'='*60}\n")

    await engine.dispose()
    return passed


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    passed = asyncio.run(run_concurrency_test(n))
    sys.exit(0 if passed else 1)