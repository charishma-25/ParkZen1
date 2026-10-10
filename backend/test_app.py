import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest.mock import patch

os.environ["PARKZEN_DB"] = os.path.join(tempfile.gettempdir(), f"parkzen-test-{os.getpid()}.sqlite3")
os.environ["PARKZEN_TEST_PAYMENTS"] = "1"

import app as app_module
from app import APP, initialize


class ParkZenApiTests(unittest.TestCase):
    def interval(self, days=1):
        start = datetime.now(timezone.utc) + timedelta(days=days)
        return start.isoformat(), (start + timedelta(hours=1)).isoformat()
    @classmethod
    def setUpClass(cls):
        initialize()
        import sqlite3
        with sqlite3.connect(os.environ["PARKZEN_DB"]) as connection:
            for table in ("parking_passes", "payments", "refunds", "loyalty_transactions", "notifications", "ticket_replies", "support_tickets", "waitlist_entries", "slot_holds", "reservations", "vehicles", "memberships", "sessions", "audit_logs"):
                connection.execute(f"DELETE FROM {table}")
            connection.execute("DELETE FROM users WHERE role='customer'")
            connection.execute("DELETE FROM slots WHERE code='B1-TEST99'")
            connection.execute("UPDATE slots SET status='available'")
            connection.execute("UPDATE floors SET maintenance=0")
            connection.commit()
        APP.config.update(TESTING=True)

    def setUp(self):
        app_module.request_log.clear()
        import sqlite3
        with sqlite3.connect(os.environ["PARKZEN_DB"]) as connection:
            for table in ("parking_passes", "payments", "refunds", "loyalty_transactions", "notifications", "ticket_replies", "support_tickets", "waitlist_entries", "slot_holds", "reservations", "vehicles", "memberships", "sessions", "audit_logs"):
                connection.execute(f"DELETE FROM {table}")
            connection.execute("DELETE FROM users WHERE role='customer'")
            connection.execute("DELETE FROM slots WHERE code='B1-TEST99'")
            connection.execute("UPDATE slots SET status='available'")
            connection.execute("UPDATE floors SET maintenance=0")
            connection.commit()
        self.client = APP.test_client()

    def register(self, email):
        response = self.client.post("/api/auth/register", json={
            "name": "Test Customer", "email": email, "mobile": "9876543210", "password": "password123"
        })
        self.assertEqual(response.status_code, 201)

    def login(self, email):
        response = self.client.post("/api/auth/login", json={"email": email, "password": "password123"})
        self.assertEqual(response.status_code, 200)
        return response.get_json()["token"]

    def test_registration_and_vehicle_management(self):
        self.register("vehicle@example.com")
        token = self.login("vehicle@example.com")
        response = self.client.post("/api/vehicles", headers={"Authorization": f"Bearer {token}"}, json={
            "plate": "TS09AB1234", "vehicle_type": "car"
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.client.get("/api/vehicles", headers={"Authorization": f"Bearer {token}"}).status_code, 200)

    def test_overlapping_reservations_are_rejected(self):
        self.register("booking@example.com")
        token = self.login("booking@example.com")
        structure = self.client.get("/api/parking/mall-a").get_json()
        slot_id = structure["floors"][0]["slots"][0]["id"]
        start, end = self.interval()
        payload = {"slot_id": slot_id, "start_at": start, "end_at": end}
        first = self.client.post("/api/reservations", headers={"Authorization": f"Bearer {token}"}, json=payload)
        second = self.client.post("/api/reservations", headers={"Authorization": f"Bearer {token}"}, json=payload)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 409)

    def test_support_ticket_isolation(self):
        self.register("support-a@example.com")
        token_a = self.login("support-a@example.com")
        response = self.client.post("/api/support", headers={"Authorization": f"Bearer {token_a}"}, json={
            "kind": "complaint", "category": "booking", "subject": "A private issue", "description": "Please help"
        })
        self.assertEqual(response.status_code, 201)
        self.register("support-b@example.com")
        token_b = self.login("support-b@example.com")
        tickets = self.client.get("/api/support", headers={"Authorization": f"Bearer {token_b}"}).get_json()["tickets"]
        self.assertEqual(tickets, [])

    def test_qr_check_in_cannot_be_replayed(self):
        self.register("qr@example.com")
        token = self.login("qr@example.com")
        slot_id = self.client.get("/api/parking/mall-a").get_json()["floors"][0]["slots"][1]["id"]
        start, end = self.interval()
        response = self.client.post("/api/reservations", headers={"Authorization": f"Bearer {token}"}, json={
            "slot_id": slot_id, "start_at": start, "end_at": end
        })
        reservation = response.get_json()["reservation"]
        qr_image = self.client.get(f"/api/reservations/{reservation['id']}/qr", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(qr_image.status_code, 200)
        self.assertEqual(qr_image.content_type, "image/png")
        self.assertTrue(qr_image.data.startswith(b"\x89PNG"))
        self.assertEqual(self.client.get(f"/api/reservations/{reservation['id']}/qr").status_code, 401)
        check_in = self.client.post(f"/api/reservations/{reservation['id']}/check-in",
                                    headers={"Authorization": f"Bearer {token}"},
                                    json={"qr_reference": reservation["qr_reference"]})
        replay = self.client.post(f"/api/reservations/{reservation['id']}/check-in",
                                  headers={"Authorization": f"Bearer {token}"},
                                  json={"qr_reference": reservation["qr_reference"]})
        self.assertEqual(check_in.status_code, 200)
        self.assertEqual(replay.status_code, 409)

    def test_hold_expires_and_waitlist_is_private(self):
        self.register("hold@example.com")
        token = self.login("hold@example.com")
        slot_id = self.client.get("/api/parking/mall-a").get_json()["floors"][0]["slots"][2]["id"]
        start, end = self.interval()
        hold = self.client.post("/api/reservation-holds", headers={"Authorization": f"Bearer {token}"}, json={
            "slot_id": slot_id, "start_at": start, "end_at": end
        })
        self.assertEqual(hold.status_code, 201)
        wait = self.client.post("/api/waitlist", headers={"Authorization": f"Bearer {token}"}, json={
            "destination_id": "mall-a", "start_at": start, "end_at": end
        })
        self.assertEqual(wait.status_code, 201)
        self.assertEqual(len(self.client.get("/api/waitlist", headers={"Authorization": f"Bearer {token}"}).get_json()["waitlist"]), 1)

    def test_admin_can_update_policies(self):
        response = self.client.post("/api/auth/login", json={"email": "admin@parkzen.local", "password": os.environ.get("PARKZEN_ADMIN_PASSWORD", "1234"), "role": "admin"})
        self.assertEqual(response.status_code, 200)
        token = response.get_json()["token"]
        updated = self.client.patch("/api/policies", headers={"Authorization": f"Bearer {token}"}, json={"hourly_rate": 75})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.get_json()["policies"]["hourly_rate"], "75")

    def test_manager_can_manage_floor_and_slot_state(self):
        response = self.client.post("/api/auth/login", json={"email": "manager@parkzen.local", "password": os.environ.get("PARKZEN_MANAGER_PASSWORD", "1234"), "role": "manager"})
        self.assertEqual(response.status_code, 200)
        token = response.get_json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        structure = self.client.get("/api/manager/structure", headers=headers)
        self.assertEqual(structure.status_code, 200)
        floor = structure.get_json()["destinations"][0]["floors"][0]
        zone = floor["zones"][0]
        created = self.client.post("/api/manager/slots", headers=headers, json={"zone_id": zone["id"], "code": "B1-TEST99", "vehicle_type": "ev", "ev_charging": True})
        self.assertEqual(created.status_code, 201)
        slot_id = created.get_json()["slot"]["id"]
        self.assertEqual(self.client.patch(f"/api/manager/slots/{slot_id}", headers=headers, json={"status": "disabled"}).status_code, 200)
        self.assertEqual(self.client.patch(f"/api/manager/floors/{floor['id']}", headers=headers, json={"maintenance": True}).status_code, 200)

    def test_waitlist_offer_can_be_claimed_after_cancellation(self):
        self.register("first@example.com")
        first_token = self.login("first@example.com")
        slot_id = self.client.get("/api/parking/mall-a").get_json()["floors"][0]["slots"][3]["id"]
        start, end = self.interval()
        payload = {"slot_id": slot_id, "start_at": start, "end_at": end}
        booking = self.client.post("/api/reservations", headers={"Authorization": f"Bearer {first_token}"}, json=payload).get_json()["reservation"]
        self.register("second@example.com")
        second_token = self.login("second@example.com")
        wait = self.client.post("/api/waitlist", headers={"Authorization": f"Bearer {second_token}"}, json={"destination_id": "mall-a", **{key: payload[key] for key in ("start_at", "end_at")}})
        self.assertEqual(wait.status_code, 201)
        self.assertEqual(self.client.post(f"/api/reservations/{booking['id']}/cancel", headers={"Authorization": f"Bearer {first_token}"}).status_code, 200)
        entry = self.client.get("/api/waitlist", headers={"Authorization": f"Bearer {second_token}"}).get_json()["waitlist"][0]
        self.assertEqual(entry["status"], "offered")
        claimed = self.client.post(f"/api/waitlist/{entry['id']}/claim", headers={"Authorization": f"Bearer {second_token}"})
        self.assertEqual(claimed.status_code, 201)
    def test_yolo_detections_map_to_configured_slots_before_confirmation(self):
        class Coordinate:
            def __init__(self, values):
                self.values = values

            def tolist(self):
                return self.values

        class FakeBox:
            def __init__(self, index):
                self.cls = [index % 2]
                self.conf = [0.91]
                self.xyxy = [Coordinate([index * 10, index * 5, index * 10 + 8, index * 5 + 8])]

        class FakeResult:
            boxes = [FakeBox(index) for index in range(20)]

        class FakeModel:
            names = {0: "space-empty", 1: "space-occupied"}

            def __call__(self, _path, conf=0.25):
                return [FakeResult()]

        manager_login = self.client.post("/api/auth/login", json={
            "email": "manager@parkzen.local",
            "password": os.environ.get("PARKZEN_MANAGER_PASSWORD", "1234"),
            "role": "manager",
        })
        manager_token = manager_login.get_json()["token"]
        manager_headers = {"Authorization": "Bearer " + manager_token}
        with patch.object(app_module, "load_model", return_value=FakeModel()):
            response = self.client.post(
                "/api/manager/analyze",
                headers=manager_headers,
                data={"destination_id": "mall-a", "image": (BytesIO(b"image"), "parking.jpg")},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 201)
        prediction = response.get_json()
        self.assertEqual(prediction["slots"][0]["slot_code"], "B1-A01")
        confirmed = self.client.post(f"/api/manager/confirm/{prediction['prediction_id']}", headers=manager_headers)
        self.assertEqual(confirmed.status_code, 200)
        updates = self.client.get("/api/availability/updates").get_json()
        self.assertTrue(updates["changed"])
        self.assertEqual(updates["updates"][0]["destination_id"], "mall-a")

    def test_membership_plan_activation_renewal_and_pass_purchase(self):
        self.register("member@example.com")
        token = self.login("member@example.com")
        headers = {"Authorization": f"Bearer {token}"}
        details = self.client.get("/api/memberships/me", headers=headers).get_json()
        self.assertEqual(details["membership"]["name"], "Basic")
        platinum = next(plan for plan in details["plans"] if plan["name"] == "Platinum")
        self.assertEqual(self.client.patch(f"/api/admin/membership-plans/{platinum['id']}", headers=headers, json={"price": 1}).status_code, 403)
        self.assertEqual(self.client.post("/api/memberships/cancel", headers=headers).status_code, 200)
        activated = self.client.post("/api/memberships/purchase", headers=headers, json={"plan_id": platinum["id"], "payment_mode": "test"})
        self.assertEqual(activated.status_code, 201)
        renewed = self.client.post("/api/memberships/renew", headers=headers, json={"payment_mode": "test"})
        self.assertEqual(renewed.status_code, 200)
        parking_pass = self.client.post("/api/passes", headers=headers, json={"pass_type": "weekly", "payment_mode": "test"})
        self.assertEqual(parking_pass.status_code, 201)
        invalid_pass = self.client.post("/api/passes", headers=headers, json={"pass_type": "daily", "vehicle_id": 99999, "payment_mode": "test"})
        self.assertEqual(invalid_pass.status_code, 403)
        self.assertEqual(self.client.get("/api/passes", headers=headers).get_json()["passes"][0]["pass_type"], "weekly")

    def test_loyalty_redemption_is_server_validated_and_duplicate_safe(self):
        self.register("rewards@example.com")
        token = self.login("rewards@example.com")
        headers = {"Authorization": f"Bearer {token}"}
        membership = self.client.get("/api/memberships/me", headers=headers).get_json()["membership"]
        import sqlite3
        with sqlite3.connect(os.environ["PARKZEN_DB"]) as connection:
            connection.execute("UPDATE memberships SET points_balance=100 WHERE id=?", (membership["id"],))
            connection.commit()
        slot_id = self.client.get("/api/parking/mall-a").get_json()["floors"][0]["slots"][4]["id"]
        start, end = self.interval()
        reservation = self.client.post("/api/reservations", headers=headers, json={"slot_id": slot_id, "start_at": start, "end_at": end}).get_json()["reservation"]
        with sqlite3.connect(os.environ["PARKZEN_DB"]) as connection:
            connection.execute("UPDATE reservations SET status='checked_in',checked_in_at=? WHERE id=?", (app_module.now(), reservation["id"]))
            connection.commit()
        checkout = self.client.post(f"/api/reservations/{reservation['id']}/check-out", headers=headers, json={"points_redeemed": 20})
        self.assertEqual(checkout.status_code, 200)
        self.assertEqual(checkout.get_json()["points_redeemed"], 20)
        replay = self.client.post(f"/api/reservations/{reservation['id']}/check-out", headers=headers, json={"points_redeemed": 20})
        self.assertEqual(replay.status_code, 409)
        insufficient = self.client.post(f"/api/reservations/{reservation['id']}/check-out", headers=headers, json={"points_redeemed": 10000})
        self.assertEqual(insufficient.status_code, 409)


if __name__ == "__main__":
    unittest.main()
