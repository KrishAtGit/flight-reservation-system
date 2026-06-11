"""
BookingService — the heart of the reservation system.

All seat state transitions happen here and nowhere else.
Every method owns its full transaction lifecycle:
    await db.commit()   on success
    await db.rollback() on failure

This is required for SELECT FOR UPDATE to work correctly.
A savepoint (begin_nested) releases the row lock at savepoint commit,
not at transaction commit — meaning concurrent transactions can all
acquire the lock before any of them commits. Using full transactions
forces PostgreSQL to serialise them correctly.

Flow:
    hold_seat()        → AVAILABLE  → HELD      (creates SeatHold)
    confirm_booking()  → HELD       → BOOKED    (creates Booking)
    release_hold()     → HELD       → AVAILABLE (deletes SeatHold)
    cancel_booking()   → BOOKED     → AVAILABLE (marks Booking cancelled)
"""

import random
import string
import uuid
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.models import (
    Seat, SeatHold, Booking, Flight,
    SeatStatus, BookingStatus
)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class SeatNotAvailable(Exception):
    pass

class HoldNotFound(Exception):
    pass

class HoldExpired(Exception):
    pass

class FlightNotFound(Exception):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_booking_reference() -> str:
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# BookingService
# ---------------------------------------------------------------------------

class BookingService:

    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # 1. hold_seat
    # ------------------------------------------------------------------

    async def hold_seat(self, flight_id: uuid.UUID, seat_id: uuid.UUID, user_id: uuid.UUID) -> SeatHold:
        """
        Attempt to place a temporary hold on a seat.

        SELECT FOR UPDATE locks the seat row for the duration of the
        entire transaction. PostgreSQL queues concurrent requests:
          - Transaction 1: locks row, sees AVAILABLE, sets HELD, commits → releases lock
          - Transaction 2: was waiting, now unblocks, sees HELD, raises SeatNotAvailable

        This is why we use a full transaction (commit/rollback) and NOT
        begin_nested() — savepoints release the row lock at savepoint
        commit, before the outer transaction commits, breaking serialisation.
        """
        try:
            result = await self.db.execute(
                select(Seat)
                .where(Seat.id == seat_id)
                .where(Seat.flight_id == flight_id)
                .with_for_update()
            )
            seat = result.scalar_one_or_none()

            if seat is None:
                await self.db.rollback()
                raise SeatNotAvailable("Seat does not exist on this flight.")

            if seat.status != SeatStatus.AVAILABLE:
                status_val = seat.status.value
                await self.db.rollback()
                raise SeatNotAvailable(
                    f"Seat is currently {status_val}."
                )

            seat.status = SeatStatus.HELD
            seat.version += 1

            expires_at = _utcnow() + timedelta(minutes=settings.SEAT_HOLD_MINUTES)
            hold = SeatHold(
                id=uuid.uuid4(),
                seat_id=seat.id,
                user_id=user_id,
                flight_id=flight_id,
                expires_at=expires_at,
                is_active=True,
            )
            self.db.add(hold)
            await self.db.commit()
            await self.db.refresh(hold)
            return hold

        except SeatNotAvailable:
            raise
        except Exception:
            await self.db.rollback()
            raise

    # ------------------------------------------------------------------
    # 2. confirm_booking
    # ------------------------------------------------------------------

    async def confirm_booking(self, hold_id: uuid.UUID, user_id: uuid.UUID) -> Booking:
        try:
            result = await self.db.execute(
                select(SeatHold)
                .where(SeatHold.id == hold_id)
                .where(SeatHold.user_id == user_id)
                .where(SeatHold.is_active == True)
                .with_for_update()
            )
            hold = result.scalar_one_or_none()

            if hold is None:
                await self.db.rollback()
                raise HoldNotFound("Hold not found or does not belong to this user.")

            if hold.is_expired:
                await self.db.rollback()
                raise HoldExpired("Hold has expired. Please select the seat again.")

            seat_result = await self.db.execute(
                select(Seat)
                .where(Seat.id == hold.seat_id)
                .with_for_update()
            )
            seat = seat_result.scalar_one()

            if seat.status != SeatStatus.HELD:
                await self.db.rollback()
                raise SeatNotAvailable(f"Seat is no longer held.")

            seat.status = SeatStatus.BOOKED
            seat.version += 1
            hold.is_active = False

            booking = Booking(
                id=uuid.uuid4(),
                seat_id=seat.id,
                user_id=user_id,
                flight_id=hold.flight_id,
                hold_id=hold.id,
                status=BookingStatus.CONFIRMED,
                amount_paid=seat.price,
                booking_reference=_generate_booking_reference(),
            )
            self.db.add(booking)
            await self.db.commit()
            await self.db.refresh(booking)
            return booking

        except (HoldNotFound, HoldExpired, SeatNotAvailable):
            raise
        except Exception:
            await self.db.rollback()
            raise

    # ------------------------------------------------------------------
    # 3. release_hold
    # ------------------------------------------------------------------

    async def release_hold(self, hold_id: uuid.UUID, user_id: uuid.UUID) -> None:
        try:
            result = await self.db.execute(
                select(SeatHold)
                .where(SeatHold.id == hold_id)
                .where(SeatHold.user_id == user_id)
                .where(SeatHold.is_active == True)
                .with_for_update()
            )
            hold = result.scalar_one_or_none()

            if hold is None:
                await self.db.rollback()
                raise HoldNotFound("Hold not found or already released.")

            seat_result = await self.db.execute(
                select(Seat)
                .where(Seat.id == hold.seat_id)
                .with_for_update()
            )
            seat = seat_result.scalar_one()

            seat.status = SeatStatus.AVAILABLE
            seat.version += 1
            hold.is_active = False
            await self.db.commit()

        except HoldNotFound:
            raise
        except Exception:
            await self.db.rollback()
            raise

    # ------------------------------------------------------------------
    # 4. cancel_booking
    # ------------------------------------------------------------------

    async def cancel_booking(self, booking_id: uuid.UUID, user_id: uuid.UUID) -> Booking:
        try:
            result = await self.db.execute(
                select(Booking)
                .where(Booking.id == booking_id)
                .where(Booking.user_id == user_id)
                .where(Booking.status == BookingStatus.CONFIRMED)
                .with_for_update()
            )
            booking = result.scalar_one_or_none()

            if booking is None:
                await self.db.rollback()
                raise HoldNotFound("Booking not found or already cancelled.")

            seat_result = await self.db.execute(
                select(Seat)
                .where(Seat.id == booking.seat_id)
                .with_for_update()
            )
            seat = seat_result.scalar_one()

            seat.status = SeatStatus.AVAILABLE
            seat.version += 1
            booking.status = BookingStatus.CANCELLED
            booking.cancelled_at = _utcnow()
            await self.db.commit()
            await self.db.refresh(booking)
            return booking

        except HoldNotFound:
            raise
        except Exception:
            await self.db.rollback()
            raise

    # ------------------------------------------------------------------
    # 5. get_seat_map
    # ------------------------------------------------------------------

    async def get_seat_map(self, flight_id: uuid.UUID) -> dict:
        flight_result = await self.db.execute(
            select(Flight).where(Flight.id == flight_id)
        )
        flight = flight_result.scalar_one_or_none()
        if flight is None:
            raise FlightNotFound(f"Flight {flight_id} not found.")

        seats_result = await self.db.execute(
            select(Seat)
            .where(Seat.flight_id == flight_id)
            .order_by(Seat.row_number, Seat.column_letter)
        )
        seats = seats_result.scalars().all()

        rows: dict[int, list] = {}
        summary = {"available": 0, "held": 0, "booked": 0, "blocked": 0}

        for seat in seats:
            row = seat.row_number
            if row not in rows:
                rows[row] = []
            rows[row].append({
                "seat_id": str(seat.id),
                "label": seat.seat_label,
                "row": seat.row_number,
                "column": seat.column_letter,
                "class": seat.seat_class.value,
                "status": seat.status.value,
                "is_window": seat.is_window,
                "is_aisle": seat.is_aisle,
                "is_middle": seat.is_middle,
                "is_exit_row": seat.is_exit_row,
                "price": float(seat.price),
            })
            summary[seat.status.value] += 1

        return {
            "flight_id": str(flight_id),
            "flight_number": flight.flight_number,
            "origin": flight.origin,
            "destination": flight.destination,
            "rows": rows,
            "summary": summary,
        }

    # ------------------------------------------------------------------
    # 6. expire_stale_holds  (called by background worker inside db.begin())
    # ------------------------------------------------------------------

    async def expire_stale_holds(self) -> int:
        """
        Release all holds where expires_at < now() and is_active = True.
        Caller is responsible for the transaction (db.begin() in expiry worker).
        """
        result = await self.db.execute(
            select(SeatHold)
            .where(SeatHold.is_active == True)
            .where(SeatHold.expires_at < _utcnow())
            .with_for_update(skip_locked=True)
        )
        expired_holds = result.scalars().all()

        if not expired_holds:
            return 0

        seat_ids = [h.seat_id for h in expired_holds]
        hold_ids = [h.id for h in expired_holds]

        await self.db.execute(
            update(Seat)
            .where(Seat.id.in_(seat_ids))
            .where(Seat.status == SeatStatus.HELD)
            .values(status=SeatStatus.AVAILABLE, version=Seat.version + 1)
        )

        await self.db.execute(
            update(SeatHold)
            .where(SeatHold.id.in_(hold_ids))
            .values(is_active=False)
        )

        return len(expired_holds)