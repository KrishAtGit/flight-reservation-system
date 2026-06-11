"""
Main FastAPI application.

Startup:
  - Initialises the ExpiryWorker (APScheduler)
  - Registers all routers

Run:
  uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.database import AsyncSessionLocal
from app.services.expiry import init_expiry_worker
from app.api import flights, bookings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background workers on startup, stop on shutdown."""
    logger.info("Starting expiry worker...")
    worker = init_expiry_worker(AsyncSessionLocal, interval_seconds=60)
    worker.start()
    logger.info("Expiry worker started.")

    yield  # app runs here

    logger.info("Stopping expiry worker...")
    worker.stop()


app = FastAPI(
    title="Flight Seat Reservation API",
    description=(
        "Concurrent seat booking engine with SELECT FOR UPDATE locking, "
        "hold/expiry system, and race condition prevention."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(flights.router)
app.include_router(bookings.router)


@app.get("/health")
async def health():
    return {"status": "ok"}