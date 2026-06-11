"""
Core ORM models for the flight seat reservation system.

Design decisions worth knowing:
- Seat has row_number + column_letter + seat_class to model physical layout
- Seat.version enables optimistic locking (compare-and-swap fallback)
- SeatHold is a separate table — NOT a status on Seat — so holds are
  queryable, expirable, and auditable independently
- Booking references both seat and hold so we can trace the full lifecycle
- All timestamps are timezone-aware (UTC)
"""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.database import Base


def utcnow():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SeatClass(str, enum.Enum):
    ECONOMY = "economy"
    PREMIUM_ECONOMY = "premium_economy"
    BUSINESS = "business"
    FIRST = "first"


class SeatStatus(str, enum.Enum):
    AVAILABLE = "available"
    HELD = "held"          # temporarily locked by a user
    BOOKED = "booked"      # confirmed reservation
    BLOCKED = "blocked"    # crew / out-of-service


class BookingStatus(str, enum.Enum):
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Flight
# ---------------------------------------------------------------------------

class Flight(Base):
    __tablename__ = "flights"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    flight_number = Column(String(10), nullable=False)   # e.g. QF401
    origin = Column(String(3), nullable=False)            # IATA code e.g. MEL
    destination = Column(String(3), nullable=False)       # e.g. SYD
    departure_time = Column(DateTime(timezone=True), nullable=False)
    arrival_time = Column(DateTime(timezone=True), nullable=False)
    total_rows = Column(Integer, nullable=False)
    columns_per_row = Column(Integer, nullable=False)     # e.g. 6 → A B C _ D E F

    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    seats = relationship("Seat", back_populates="flight", lazy="noload")

    __table_args__ = (
        UniqueConstraint("flight_number", "departure_time", name="uq_flight_departure"),
        CheckConstraint("arrival_time > departure_time", name="ck_flight_times"),
        CheckConstraint("total_rows > 0", name="ck_positive_rows"),
    )

    def __repr__(self):
        return f"<Flight {self.flight_number} {self.origin}→{self.destination}>"


# ---------------------------------------------------------------------------
# Seat
# ---------------------------------------------------------------------------

class Seat(Base):
    """
    Represents a single physical seat on a flight.

    row_number: 1-based integer (row 1 = first row)
    column_letter: A, B, C, D, E, F
    seat_class: maps to the zone this seat belongs to
    status: current availability state
    version: incremented on every status change — used for optimistic locking

    Why version column?
    When we try to book a seat we check:
        UPDATE seats SET status='booked', version=version+1
        WHERE id=? AND version=? AND status='available'
    If 0 rows affected → someone else changed it first → conflict detected.
    This is our fallback when we're not inside a SELECT FOR UPDATE block.
    """

    __tablename__ = "seats"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    flight_id = Column(UUID(as_uuid=True), ForeignKey("flights.id", ondelete="CASCADE"), nullable=False)

    row_number = Column(Integer, nullable=False)          # 1 to total_rows
    column_letter = Column(String(1), nullable=False)     # A B C D E F
    seat_class = Column(Enum(SeatClass), nullable=False)
    is_window = Column(Boolean, nullable=False, default=False)
    is_aisle = Column(Boolean, nullable=False, default=False)
    is_middle = Column(Boolean, nullable=False, default=False)
    is_exit_row = Column(Boolean, nullable=False, default=False)

    status = Column(Enum(SeatStatus), nullable=False, default=SeatStatus.AVAILABLE)

    # Optimistic locking counter
    version = Column(Integer, nullable=False, default=0)

    price = Column(Numeric(10, 2), nullable=False)

    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    flight = relationship("Flight", back_populates="seats")
    holds = relationship("SeatHold", back_populates="seat", lazy="noload")
    bookings = relationship("Booking", back_populates="seat", lazy="noload")

    __table_args__ = (
        # Each seat position is unique per flight
        UniqueConstraint("flight_id", "row_number", "column_letter", name="uq_seat_position"),
        # Fast lookup: all available seats on a flight (most common query)
        Index("ix_seat_flight_status", "flight_id", "status"),
        # Range queries: available seats by class on a flight
        Index("ix_seat_flight_class_status", "flight_id", "seat_class", "status"),
        # Row-range queries (DSA: segment tree maps to this)
        Index("ix_seat_flight_row", "flight_id", "row_number", "status"),
        CheckConstraint("row_number > 0", name="ck_positive_row"),
        CheckConstraint("column_letter IN ('A','B','C','D','E','F','G','H')", name="ck_valid_column"),
    )

    @property
    def seat_label(self) -> str:
        """Human-readable e.g. '14C'"""
        return f"{self.row_number}{self.column_letter}"

    def __repr__(self):
        return f"<Seat {self.seat_label} [{self.seat_class.value}] {self.status.value}>"


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), nullable=False, unique=True)
    full_name = Column(String(255), nullable=False)
    hashed_password = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    holds = relationship("SeatHold", back_populates="user", lazy="noload")
    bookings = relationship("Booking", back_populates="user", lazy="noload")

    __table_args__ = (
        Index("ix_user_email", "email"),
    )

    def __repr__(self):
        return f"<User {self.email}>"


# ---------------------------------------------------------------------------
# SeatHold  (the concurrency-critical table)
# ---------------------------------------------------------------------------

class SeatHold(Base):
    """
    Temporary reservation of a seat before payment/confirmation.

    Lifecycle:
        AVAILABLE seat → hold created → status set to HELD
        Hold expires OR user cancels → status reset to AVAILABLE
        User confirms → Booking created → status set to BOOKED → hold deleted

    Why a separate table instead of a column on Seat?
    - Holds need their own expiry timestamp (queryable for background cleanup)
    - Multiple holds per seat over time are auditable
    - The background eviction worker queries SeatHold directly for expired rows
    - We can answer "how many holds has this user created today?" easily

    The is_active flag + expires_at gives us two ways to invalidate a hold:
    - Expiry: background worker sets is_active=False when expires_at < now()
    - Manual: user cancels, we set is_active=False immediately
    """

    __tablename__ = "seat_holds"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    seat_id = Column(UUID(as_uuid=True), ForeignKey("seats.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    flight_id = Column(UUID(as_uuid=True), ForeignKey("flights.id", ondelete="CASCADE"), nullable=False)

    expires_at = Column(DateTime(timezone=True), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    seat = relationship("Seat", back_populates="holds")
    user = relationship("User", back_populates="holds")
    booking = relationship("Booking", back_populates="hold", uselist=False, lazy="noload")

    __table_args__ = (
        # Core query for expiry worker: all active expired holds
        Index("ix_hold_expiry", "expires_at", "is_active"),
        # One active hold per seat at a time — enforced at app layer too
        Index("ix_hold_seat_active", "seat_id", "is_active"),
    )

    @property
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at

    def __repr__(self):
        return f"<SeatHold seat={self.seat_id} user={self.user_id} expires={self.expires_at}>"


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------

class Booking(Base):
    """
    Confirmed seat reservation. Immutable once created.

    A booking always originates from a hold — you cannot book without first
    holding. This ensures the seat was properly locked during the transaction.

    hold_id is nullable after the hold is cleaned up, but we keep the reference
    while the hold exists for traceability.
    """

    __tablename__ = "bookings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    seat_id = Column(UUID(as_uuid=True), ForeignKey("seats.id"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    flight_id = Column(UUID(as_uuid=True), ForeignKey("flights.id"), nullable=False)
    hold_id = Column(UUID(as_uuid=True), ForeignKey("seat_holds.id"), nullable=True)

    status = Column(Enum(BookingStatus), nullable=False, default=BookingStatus.CONFIRMED)
    amount_paid = Column(Numeric(10, 2), nullable=False)

    booking_reference = Column(String(8), nullable=False, unique=True)  # e.g. QF4X9Z2A

    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)

    seat = relationship("Seat", back_populates="bookings")
    user = relationship("User", back_populates="bookings")
    hold = relationship("SeatHold", back_populates="booking")

    __table_args__ = (
        Index("ix_booking_user", "user_id", "status"),
        Index("ix_booking_flight", "flight_id", "status"),
        Index("ix_booking_reference", "booking_reference"),
        # A seat can only have one active booking at a time
        Index("ix_booking_seat_confirmed", "seat_id", "status"),
    )

    def __repr__(self):
        return f"<Booking {self.booking_reference} seat={self.seat_id} {self.status.value}>"