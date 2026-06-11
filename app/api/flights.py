from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.models.models import Flight
from app.services.booking import BookingService, FlightNotFound
from app.api.schemas import FlightResponse, SeatMapResponse

router = APIRouter(prefix="/flights", tags=["flights"])


@router.get("/", response_model=list[FlightResponse])
async def list_flights(db: AsyncSession = Depends(get_db)):
    """List all available flights."""
    result = await db.execute(select(Flight).order_by(Flight.departure_time))
    flights = result.scalars().all()
    return flights


@router.get("/{flight_id}", response_model=FlightResponse)
async def get_flight(flight_id: str, db: AsyncSession = Depends(get_db)):
    """Get a single flight by ID."""
    import uuid
    try:
        fid = uuid.UUID(flight_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid flight ID format.")

    result = await db.execute(select(Flight).where(Flight.id == fid))
    flight = result.scalar_one_or_none()
    if not flight:
        raise HTTPException(status_code=404, detail="Flight not found.")
    return flight


@router.get("/{flight_id}/seats", response_model=SeatMapResponse)
async def get_seat_map(flight_id: str, db: AsyncSession = Depends(get_db)):
    """
    Get the full physical seat map for a flight.
    Returns every seat grouped by row with status and metadata.
    """
    import uuid
    try:
        fid = uuid.UUID(flight_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid flight ID format.")

    service = BookingService(db)
    try:
        seat_map = await service.get_seat_map(fid)
        return seat_map
    except FlightNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))