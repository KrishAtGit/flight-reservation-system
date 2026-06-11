"""
Pydantic schemas — request bodies and response shapes for all endpoints.
Kept separate from ORM models intentionally: API contracts should not
be coupled to DB internals.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel, EmailStr


# ---------------------------------------------------------------------------
# Flight
# ---------------------------------------------------------------------------

class FlightResponse(BaseModel):
    id: uuid.UUID
    flight_number: str
    origin: str
    destination: str
    departure_time: datetime
    arrival_time: datetime
    total_rows: int
    columns_per_row: int

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Seat
# ---------------------------------------------------------------------------

class SeatResponse(BaseModel):
    seat_id: str
    label: str
    row: int
    column: str
    class_: str
    status: str
    is_window: bool
    is_aisle: bool
    is_middle: bool
    is_exit_row: bool
    price: float

    model_config = {"from_attributes": True, "populate_by_name": True}


class SeatMapResponse(BaseModel):
    flight_id: str
    flight_number: str
    origin: str
    destination: str
    rows: dict[int, list[dict]]
    summary: dict[str, int]


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

class UserCreate(BaseModel):
    email: EmailStr
    full_name: str
    password: str


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    is_active: bool

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Hold
# ---------------------------------------------------------------------------

class HoldRequest(BaseModel):
    flight_id: uuid.UUID
    seat_id: uuid.UUID
    user_id: uuid.UUID   # in a real system this comes from JWT auth


class HoldResponse(BaseModel):
    hold_id: uuid.UUID
    seat_id: uuid.UUID
    user_id: uuid.UUID
    flight_id: uuid.UUID
    expires_at: datetime
    is_active: bool

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------

class BookingRequest(BaseModel):
    hold_id: uuid.UUID
    user_id: uuid.UUID


class CancelRequest(BaseModel):
    user_id: uuid.UUID


class BookingResponse(BaseModel):
    booking_id: uuid.UUID
    booking_reference: str
    seat_id: uuid.UUID
    user_id: uuid.UUID
    flight_id: uuid.UUID
    status: str
    amount_paid: Decimal
    created_at: datetime
    cancelled_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------

class ErrorResponse(BaseModel):
    error: str
    detail: str