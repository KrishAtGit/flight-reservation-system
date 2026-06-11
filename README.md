# Flight Seat Reservation System

A concurrent seat booking engine built in Python — demonstrating pessimistic locking, hold/expiry lifecycle management, and race condition prevention under real concurrent load.

---

## What this is

A backend system that solves the hardest problem in ticket booking: **two users clicking "book" on the same seat at the same millisecond**. The system guarantees exactly one booking wins, every time, without sacrificing throughput.

Built as a portfolio project targeting backend/cloud engineering internships. Every design decision was made to mirror real booking infrastructure — not to look impressive in a README.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     FastAPI REST API                    │
│          /flights  /holds  /bookings  /health           │
└─────────────────────┬───────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────┐
│                  BookingService                         │
│                                                         │
│  hold_seat()        SELECT FOR UPDATE → HELD            │
│  confirm_booking()  HELD → BOOKED                       │
│  release_hold()     HELD → AVAILABLE                    │
│  cancel_booking()   BOOKED → AVAILABLE                  │
└──────────────┬──────────────────────┬───────────────────┘
               │                      │
┌──────────────▼────── ┐  ┌────────────▼──────────────────┐
│   PostgreSQL         │  │   ExpiryWorker                │
│                      │  │                               │
│   Seats (with        │  │   HoldExpiryHeap (min-heap)   │
│   version column)    │  │   APScheduler (60s interval)  │
│   SeatHolds          │  │   Bulk DB release on tick     │
│   Bookings           │  │                               │
└──────────────────────┘  └───────────────────────────────┘
```

---

## Key engineering decisions

### 1. Pessimistic locking with SELECT FOR UPDATE

When a user requests a seat, the system issues:

```sql
SELECT * FROM seats
WHERE id = $1 AND flight_id = $2
FOR UPDATE;
```

This acquires a row-level lock for the duration of the transaction. PostgreSQL queues all concurrent requests for the same row. The second transaction doesn't even read the row until the first commits — at which point it sees `status = held` and raises `SeatNotAvailable`.

**Why not optimistic locking?** The version column exists as a fallback, but optimistic locking causes retry storms under high contention. For a seat booking scenario where contention is the entire problem, pessimistic locking is the correct choice.

**Why not application-level locking (Redis)?** Adds a dependency and a failure mode. PostgreSQL row locks are transactional — they automatically release on commit or rollback. A Redis lock requires TTL management and can leave phantom locks if the process crashes.

### 2. SeatHold as a separate table

Holds are not a column on the `seats` table. They're a first-class entity with their own table. This enables:

- The expiry worker to query `WHERE expires_at < now() AND is_active = true` on a dedicated indexed table instead of scanning all seats
- Full audit trail of hold history per seat
- Querying "how many holds has this user created today" without touching seats

### 3. Min-heap for O(log n) expiry eviction

The expiry worker maintains a min-heap ordered by `expires_at`:

```python
heapq.heappush(heap, HoldEntry(expires_at, hold_id, seat_id, user_id))
```

On each scheduler tick, it pops all entries where `expires_at <= now()` in O(k log n) where k is the number of expired holds. This is contrasted with the naive approach — a full table scan every N seconds — which is O(n) regardless of how many holds are actually expired.

The DB sweep still runs as a safety net for holds created before a server restart (when the heap is empty).

### 4. Physical seat layout

Seats are modelled with `row_number`, `column_letter`, `is_window`, `is_aisle`, `is_exit_row`, and `seat_class`. This enables:

- Range queries: "find available window seats in rows 10–20"
- Indexes on `(flight_id, row_number, status)` support row-range lookups efficiently
- A segment tree could be layered on this index for bulk availability queries across row ranges

---

## Concurrency proof

50 concurrent asyncio coroutines all racing to hold the same seat simultaneously:

```
============================================================
  CONCURRENCY TEST — 50 simultaneous requests
============================================================
  Flight:  QF401 MEL→SYD
  Seat:    1A [first] $850.00
  Target:  1 success, 49 blocked
============================================================

  RESULTS
  Total requests:    50
  Succeeded:         1
  Blocked:           49
  Unexpected errors: 0

  Winner: User 0 (180.3ms)

  Timing (ms):
    Min:    180.3
    Median: 1634.9
    Max:    1654.6

  ✓ PASSED — Zero double bookings under 50 concurrent requests
  ✓ SELECT FOR UPDATE serialised all 50 transactions correctly
============================================================
```

The timing distribution is itself evidence of correctness. The winner acquires the lock in ~180ms. Everyone else waits ~1.6 seconds — the duration of the winning transaction holding the lock. That 1.4-second gap is PostgreSQL's queue.

---

## Load test results (Locust)

20 concurrent users, 60 second run, realistic booking flows:

| Endpoint | Requests | Failures | Median (ms) | 99th pct (ms) |
|----------|----------|----------|-------------|---------------|
| POST /holds | 203 | 0 | 7 | 44 |
| POST /bookings | 83 | 0 | 9 | 65 |
| POST /bookings/cancel | 31 | 0 | 9 | 36 |
| GET /flights/{id}/seats | 219 | 0 | 9 | 2100 |
| **Aggregated** | **836** | **0** | **9** | **2100** |

**836 requests, 0 failures.** The 99th percentile spike on seat map is payload size (172 seats as JSON) — a caching concern, not a correctness concern.

---

## Tech stack

| Layer | Technology | Why |
|-------|-----------|-----|
| API | FastAPI + uvicorn | Async-native, auto-generates OpenAPI docs |
| ORM | SQLAlchemy 2.0 async | Full async support, explicit transaction control |
| Database | PostgreSQL | Row-level locking, ACID transactions |
| Driver | asyncpg | Fastest async PostgreSQL driver for Python |
| Scheduling | APScheduler | Async-compatible background job scheduler |
| Load testing | Locust | Scriptable, realistic user flow simulation |

---

## Project structure

```
FRsystem/
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── api/
│   │   ├── __init__.py
│   │   ├── schemas.py
│   │   ├── flights.py
│   │   └── bookings.py
│   ├── core/
│   │   ├── config.py
│   │   └── database.py
│   ├── models/
│   │   ├── __init__.py
│   │   └── models.py
│   └── services/
│       ├── booking.py
│       └── expiry.py
├── migrations/
│   └── env.py
├── scripts/
│   ├── seed.py
│   ├── test_booking.py
│   ├── test_concurrency.py
│   ├── test_expiry.py
│   └── locustfile.py
├── tests/
├── .env
├── .env.example
├── requirements.txt
└── README.md
```

---

## Running locally

```bash
# 1. Clone and install
git clone <repo>
cd flight_booking
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env with your PostgreSQL credentials

# 3. Create database
psql -U postgres -c "CREATE DATABASE flight_booking;"

# 4. Seed
python scripts/seed.py

# 5. Start API
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 6. Open docs
# http://localhost:8000/docs
```

### Run the tests

```bash
# Smoke test
python scripts/test_booking.py

# Concurrency proof
python scripts/test_concurrency.py

# Expiry system
python scripts/test_expiry.py

# Load test (API must be running)
locust -f scripts/locustfile.py --host http://localhost:8000
# Open http://localhost:8089
```

---

## Some interesting features to add

- **JWT authentication** — user_id currently passed in request body; in production it comes from a decoded token in the Authorization header
- **Redis distributed locking** — for horizontal scaling across multiple API instances where a single PostgreSQL lock isn't sufficient
- **Seat map caching** — Redis cache for `GET /flights/{id}/seats` with cache invalidation on status change; solves the 99th percentile latency spike
- **Kubernetes deployment** — HPA on the API pods, single ExpiryWorker pod to avoid duplicate expiry runs
- **Idempotency keys** — prevent duplicate bookings from network retries
