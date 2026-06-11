"""
Locust load test — Phase 6

Simulates realistic concurrent booking flows against the live API.

Run:
    locust -f scripts/locustfile.py --host http://localhost:8000

Then open http://localhost:8089 and set:
    - Users: 20
    - Spawn rate: 5
    - Run for 60 seconds

What it tests:
    - GET /flights/ — list flights
    - GET /flights/{id}/seats — seat map under load
    - POST /holds — concurrent hold attempts (races for same seats)
    - POST /bookings — confirm holds
    - POST /bookings/{id}/cancel — cancellations returning seats to pool
"""

import random
import uuid
from locust import HttpUser, task, between, events
from locust.runners import MasterRunner


# ---------------------------------------------------------------------------
# Shared state — flight ID and user pool loaded once at test start
# ---------------------------------------------------------------------------

FLIGHT_ID = None
USER_IDS = []
N_USERS = 30  # pre-created users in DB


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    """
    Runs once before load test starts.
    Fetches the flight ID and creates test users via the API.
    """
    import requests

    base = environment.host
    global FLIGHT_ID, USER_IDS

    # Get flight
    resp = requests.get(f"{base}/flights/")
    if resp.status_code != 200 or not resp.json():
        print("ERROR: No flights found. Run seed.py first.")
        return

    FLIGHT_ID = resp.json()[0]["id"]
    print(f"\n[locust] Using flight: {FLIGHT_ID}")

    # Create test users
    print(f"[locust] Creating {N_USERS} test users...")
    for i in range(N_USERS):
        payload = {
            "email": f"locust_user_{i}_{uuid.uuid4().hex[:6]}@test.com",
            "full_name": f"Locust User {i}",
            "password": "test123",
        }
        r = requests.post(f"{base}/users", json=payload)
        if r.status_code == 201:
            USER_IDS.append(r.json()["id"])

    print(f"[locust] {len(USER_IDS)} users ready.\n")


# ---------------------------------------------------------------------------
# BookingUser — simulates a single user session
# ---------------------------------------------------------------------------

class BookingUser(HttpUser):
    """
    Each simulated user:
    1. Views the seat map
    2. Tries to hold a random available seat
    3. If hold succeeds, confirms the booking
    4. Occasionally cancels the booking (returning seat to pool)
    """

    wait_time = between(0.5, 2)  # think time between tasks

    def on_start(self):
        """Pick a user ID from the pool for this simulated session."""
        self.user_id = random.choice(USER_IDS) if USER_IDS else str(uuid.uuid4())
        self.active_hold_id = None
        self.active_booking_id = None

    # ── Task 1: View seat map (weight=3, runs most often) ────────────
    @task(3)
    def view_seat_map(self):
        if not FLIGHT_ID:
            return
        with self.client.get(
            f"/flights/{FLIGHT_ID}/seats",
            name="/flights/{id}/seats",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"Status {resp.status_code}")

    # ── Task 2: Try to hold a seat (weight=4, core task) ────────────
    @task(4)
    def hold_seat(self):
        if not FLIGHT_ID or not USER_IDS:
            return

        # Get current seat map to find an available seat
        resp = self.client.get(
            f"/flights/{FLIGHT_ID}/seats",
            name="/flights/{id}/seats [hold-lookup]",
        )
        if resp.status_code != 200:
            return

        seat_map = resp.json()
        available_seats = [
            seat
            for row in seat_map["rows"].values()
            for seat in row
            if seat["status"] == "available"
        ]

        if not available_seats:
            return  # all seats taken — valid scenario

        # Pick a random available seat and race for it
        seat = random.choice(available_seats)

        with self.client.post(
            "/holds",
            json={
                "flight_id": FLIGHT_ID,
                "seat_id": seat["seat_id"],
                "user_id": self.user_id,
            },
            name="/holds [create]",
            catch_response=True,
        ) as resp:
            if resp.status_code == 201:
                self.active_hold_id = resp.json()["hold_id"]
                resp.success()
            elif resp.status_code == 409:
                # Seat taken by another user — expected under concurrent load
                resp.success()  # not a failure, it's correct behaviour
            else:
                resp.failure(f"Unexpected status {resp.status_code}: {resp.text}")

    # ── Task 3: Confirm booking (weight=2) ───────────────────────────
    @task(2)
    def confirm_booking(self):
        if not self.active_hold_id:
            return

        with self.client.post(
            "/bookings",
            json={
                "hold_id": self.active_hold_id,
                "user_id": self.user_id,
            },
            name="/bookings [confirm]",
            catch_response=True,
        ) as resp:
            if resp.status_code == 201:
                self.active_booking_id = resp.json()["booking_id"]
                self.active_hold_id = None
                resp.success()
            elif resp.status_code in (404, 410):
                # Hold expired or not found — valid under load
                self.active_hold_id = None
                resp.success()
            else:
                resp.failure(f"Unexpected status {resp.status_code}: {resp.text}")

    # ── Task 4: Cancel booking (weight=1, least frequent) ───────────
    @task(1)
    def cancel_booking(self):
        if not self.active_booking_id:
            return

        with self.client.post(
            f"/bookings/{self.active_booking_id}/cancel",
            json={"user_id": self.user_id},
            name="/bookings/{id}/cancel",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                self.active_booking_id = None
                resp.success()
            elif resp.status_code == 404:
                self.active_booking_id = None
                resp.success()
            else:
                resp.failure(f"Unexpected status {resp.status_code}: {resp.text}")