import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.models.models import User, Booking, SeatHold, BookingStatus
from app.services.booking import (
    BookingService,
    SeatNotAvailable,
    HoldNotFound,
    HoldExpired,
)
from app.api.schemas import (
    HoldRequest, HoldResponse,
    BookingRequest, BookingResponse,
    CancelRequest,
)

router = APIRouter(tags=["bookings"])


# ---------------------------------------------------------------------------
# Holds
# ---------------------------------------------------------------------------

@router.post("/holds", response_model=HoldResponse, status_code=201)
async def create_hold(body: HoldRequest, db: AsyncSession = Depends(get_db)):
    """
    Place a temporary hold on a seat.

    The seat is locked for SEAT_HOLD_MINUTES (default 10).
    Only one active hold per seat is allowed at a time.
    Concurrent requests for the same seat are serialised by SELECT FOR UPDATE.
    """
    # Verify user exists
    user = (await db.execute(select(User).where(User.id == body.user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    service = BookingService(db)
    try:
        hold = await service.hold_seat(body.flight_id, body.seat_id, body.user_id)
        return HoldResponse(
            hold_id=hold.id,
            seat_id=hold.seat_id,
            user_id=hold.user_id,
            flight_id=hold.flight_id,
            expires_at=hold.expires_at,
            is_active=hold.is_active,
        )
    except SeatNotAvailable as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.delete("/holds/{hold_id}", status_code=204)
async def release_hold(hold_id: str, user_id: str, db: AsyncSession = Depends(get_db)):
    """Release a hold early, returning the seat to available."""
    try:
        hid = uuid.UUID(hold_id)
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid ID format.")

    service = BookingService(db)
    try:
        await service.release_hold(hid, uid)
    except HoldNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------

@router.post("/bookings", response_model=BookingResponse, status_code=201)
async def confirm_booking(body: BookingRequest, db: AsyncSession = Depends(get_db)):
    """
    Convert an active hold into a confirmed booking.
    The hold must be active and not expired.
    """
    service = BookingService(db)
    try:
        booking = await service.confirm_booking(body.hold_id, body.user_id)
        return BookingResponse(
            booking_id=booking.id,
            booking_reference=booking.booking_reference,
            seat_id=booking.seat_id,
            user_id=booking.user_id,
            flight_id=booking.flight_id,
            status=booking.status.value,
            amount_paid=booking.amount_paid,
            created_at=booking.created_at,
            cancelled_at=booking.cancelled_at,
        )
    except HoldNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except HoldExpired as e:
        raise HTTPException(status_code=410, detail=str(e))
    except SeatNotAvailable as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/bookings/{booking_id}", response_model=BookingResponse)
async def get_booking(booking_id: str, db: AsyncSession = Depends(get_db)):
    """Get a booking by ID."""
    try:
        bid = uuid.UUID(booking_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid booking ID.")

    result = await db.execute(select(Booking).where(Booking.id == bid))
    booking = result.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found.")

    return BookingResponse(
        booking_id=booking.id,
        booking_reference=booking.booking_reference,
        seat_id=booking.seat_id,
        user_id=booking.user_id,
        flight_id=booking.flight_id,
        status=booking.status.value,
        amount_paid=booking.amount_paid,
        created_at=booking.created_at,
        cancelled_at=booking.cancelled_at,
    )


@router.post("/bookings/{booking_id}/cancel", response_model=BookingResponse)
async def cancel_booking(booking_id: str, body: CancelRequest, db: AsyncSession = Depends(get_db)):
    """Cancel a confirmed booking and return the seat to available."""
    try:
        bid = uuid.UUID(booking_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid booking ID.")

    service = BookingService(db)
    try:
        booking = await service.cancel_booking(bid, body.user_id)
        return BookingResponse(
            booking_id=booking.id,
            booking_reference=booking.booking_reference,
            seat_id=booking.seat_id,
            user_id=booking.user_id,
            flight_id=booking.flight_id,
            status=booking.status.value,
            amount_paid=booking.amount_paid,
            created_at=booking.created_at,
            cancelled_at=booking.cancelled_at,
        )
    except HoldNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@router.post("/users", status_code=201)
async def create_user(body: dict, db: AsyncSession = Depends(get_db)):
    """Create a user for testing. In production this would include auth."""
    import hashlib
    user = User(
        id=uuid.uuid4(),
        email=body["email"],
        full_name=body["full_name"],
        hashed_password=hashlib.sha256(body["password"].encode()).hexdigest(),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return {"id": str(user.id), "email": user.email, "full_name": user.full_name}