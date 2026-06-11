"""
Seed script — creates one flight and generates its full seat map.

Run: python scripts/seed.py

Seat layout for a narrow-body like a 737-800:
  Rows 1-4:    First class     (A B _ C D)       4 seats/row
  Rows 5-10:   Business        (A B _ C D E F)   6 seats/row  (exit row: 10)
  Rows 11-30:  Economy         (A B C _ D E F)   6 seats/row  (exit rows: 15, 20)

Columns: A=window, B=middle, C=aisle, D=aisle, E=middle, F=window
"""

import asyncio
import random
import string
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.core.config import settings
from app.core.database import Base
from app.models.models import Flight, Seat, SeatClass, SeatStatus
from datetime import datetime, timezone, timedelta
import uuid


async def seed():
    engine = create_async_engine(settings.DATABASE_URL, echo=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with SessionLocal() as session:
        # Create flight
        flight = Flight(
            id=uuid.uuid4(),
            flight_number="QF401",
            origin="MEL",
            destination="SYD",
            departure_time=datetime.now(timezone.utc) + timedelta(days=7),
            arrival_time=datetime.now(timezone.utc) + timedelta(days=7, hours=1, minutes=25),
            total_rows=30,
            columns_per_row=6,
        )
        session.add(flight)
        await session.flush()

        seats = []

        LAYOUT = {
            # row_range: (seat_class, columns, exit_rows, prices)
            "first":    {"rows": range(1, 5),   "cols": ["A", "B", "C", "D"],             "price": 850.00},
            "business": {"rows": range(5, 11),  "cols": ["A", "B", "C", "D", "E", "F"],   "price": 450.00},
            "economy":  {"rows": range(11, 31), "cols": ["A", "B", "C", "D", "E", "F"],   "price": 189.00},
        }

        EXIT_ROWS = {10, 15, 20}

        # column position metadata
        WINDOW_COLS = {"A", "F"}
        AISLE_COLS = {"C", "D"}
        MIDDLE_COLS = {"B", "E"}

        for zone, cfg in LAYOUT.items():
            if zone == "first":
                seat_class = SeatClass.FIRST
            elif zone == "business":
                seat_class = SeatClass.BUSINESS
            else:
                seat_class = SeatClass.ECONOMY

            for row in cfg["rows"]:
                for col in cfg["cols"]:
                    seat = Seat(
                        id=uuid.uuid4(),
                        flight_id=flight.id,
                        row_number=row,
                        column_letter=col,
                        seat_class=seat_class,
                        is_window=(col in WINDOW_COLS),
                        is_aisle=(col in AISLE_COLS),
                        is_middle=(col in MIDDLE_COLS),
                        is_exit_row=(row in EXIT_ROWS),
                        status=SeatStatus.AVAILABLE,
                        version=0,
                        price=cfg["price"],
                    )
                    seats.append(seat)

        session.add_all(seats)
        await session.commit()

        total = len(seats)
        print(f"\n✓ Flight {flight.flight_number} created: {flight.id}")
        print(f"✓ {total} seats generated")
        print(f"  First:    {sum(1 for s in seats if s.seat_class == SeatClass.FIRST)}")
        print(f"  Business: {sum(1 for s in seats if s.seat_class == SeatClass.BUSINESS)}")
        print(f"  Economy:  {sum(1 for s in seats if s.seat_class == SeatClass.ECONOMY)}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())