"""
Comprehensive End-to-End & Concurrency Test Suite for AFRC Loyalty & Account System.
Tests:
1. Points policy math & float precision
2. Payment gateway security gate (no points without verified capture; cookie tamper immunity)
3. Guest checkout unclaimed points retention via signed cookies
4. Guest unclaimed points automatic overwrite (no accumulation across guest bills!)
5. Google Sign-In automatic claiming, idempotency, and multi-account race protection
6. Logged-in checkout, point redemption tiers, cooldown enforcement, and voucher application
7. Partial/full refund clawbacks, negative balance Points Debt lock, and voucher restoration
8. Guest session clear & Admin Loyalty/Voucher/Customer management APIs
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

# Configure isolated temporary SQLite database BEFORE importing backend
_TMP_DIR = tempfile.mkdtemp(prefix="afrc_loyalty_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DIR}/test_afrc.db"
os.environ["GOOGLE_CLIENT_ID"] = "test-google-client-id.apps.googleusercontent.com"
os.environ["CUSTOMER_SESSION_SECRET"] = "test_secret_key_for_hmac_signing_32bytes_min!"
os.environ["CURLEC_KEY_ID"] = "rzp_test_mockkey"
os.environ["CURLEC_KEY_SECRET"] = "mock_curlec_secret"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from backend.db import AdminUser, SessionBill, SessionLocal, init_db, next_bill_number
from backend.auth import create_admin_session, hash_password
from backend.loyalty import (
    UNCLAIMED_POINTS_COOKIE,
    compute_points_from_food_subtotal,
    format_debt_message,
    is_redeem_blocked_by_debt,
    is_voucher_use_blocked_by_debt,
    process_verified_paid_bill,
)
from backend.main import _record_captured_payment, app


def _create_open_bill(
    food_amount: float,
    client_token: str = "",
    guest_session_id: str = "",
    customer_user_id: int | None = None,
) -> str:
    db = SessionLocal()
    try:
        sid = str(uuid.uuid4())
        bno = next_bill_number(db)
        items = [{"name": "Chicken", "quantity": 1, "unit_price": food_amount, "subtotal": round(food_amount, 2)}]
        bill = SessionBill(
            id=sid,
            bill_number=bno,
            raw_ai="[]",
            final_bill=json.dumps(items),
            status="pending",
            curlec_order_id=f"order_{sid[:10]}",
            client_token=client_token,
            guest_session_id=guest_session_id,
            customer_user_id=customer_user_id,
        )
        db.add(bill)
        db.commit()
        return sid
    finally:
        db.close()


def _simulate_verified_curlec_capture(session_id: str, payment_id: str | None = None) -> bool:
    db = SessionLocal()
    try:
        bill = db.query(SessionBill).filter(SessionBill.id == session_id).first()
        assert bill is not None
        from backend.main import _amount_sen
        expected_sen = _amount_sen(bill)
        payment = {
            "id": payment_id or f"pay_{session_id[:10]}",
            "status": "captured",
            "currency": "MYR",
            "amount": expected_sen,
            "order_id": bill.curlec_order_id,
            "contact": "+60123456789",
        }
        return _record_captured_payment(bill, payment, db)
    finally:
        db.close()


def run_all_tests():
    init_db()

    print("1. Testing pure points policy & debt rules...")
    assert compute_points_from_food_subtotal(25.90) == 250
    assert compute_points_from_food_subtotal(9.99) == 90
    assert compute_points_from_food_subtotal(1.00) == 10
    assert compute_points_from_food_subtotal(0.1 + 0.2 + 0.7) == 10  # IEEE-754 float safety!
    assert compute_points_from_food_subtotal(0.99) == 0
    assert compute_points_from_food_subtotal(0) == 0
    assert compute_points_from_food_subtotal(-10) == 0
    assert compute_points_from_food_subtotal(float("nan")) == 0
    assert format_debt_message(0) is None
    assert "Points debt: 50" in (format_debt_message(50) or "")
    assert is_redeem_blocked_by_debt(-1) is True
    assert is_redeem_blocked_by_debt(0) is False
    assert is_voucher_use_blocked_by_debt(-5) is True
    print("   [OK] Pure points policy verified.")

    print("2. Testing guest checkout unclaimed points, tamper resistance & automatic OVERWRITE...")
    guest_client = TestClient(app)
    # Initial visit issues signed afrc_guest_loyalty cookie
    r0 = guest_client.get("/api/loyalty/status")
    assert r0.status_code == 200
    assert r0.json()["unclaimed"]["has_unclaimed"] is False

    # Create unpaid bill RM 15.80 -> visiting success page BEFORE gateway capture MUST NOT award points!
    from backend.loyalty import verify_guest_session_cookie
    gsid = verify_guest_session_cookie(guest_client.cookies.get("afrc_guest_loyalty", ""))
    assert gsid, "Expected signed afrc_guest_loyalty cookie"

    bill1_id = _create_open_bill(15.80, client_token="ctok_guest_1", guest_session_id=gsid)
    guest_client.cookies.set("afrc_client_id", "ctok_guest_1")

    r_unpaid = guest_client.get(f"/api/loyalty/status?session_id={bill1_id}")
    assert r_unpaid.json()["unclaimed"]["has_unclaimed"] is False, "Unpaid bill must NEVER yield points!"

    # Now simulate verified Curlec payment capture on Bill #1 (RM 15.80 -> 150 pts)
    assert _simulate_verified_curlec_capture(bill1_id) is True

    # Check status -> 150 unclaimed points retained in signed cookie & DB!
    r_after_b1 = guest_client.get(f"/api/loyalty/status?session_id={bill1_id}")
    st1 = r_after_b1.json()
    assert st1["authenticated"] is False
    assert st1["unclaimed"]["has_unclaimed"] is True
    assert st1["unclaimed"]["points"] == 150
    assert UNCLAIMED_POINTS_COOKIE in guest_client.cookies

    # Tamper test: forge cookie value -> server must ignore tampered points and return authoritative 150
    guest_client.cookies.set(UNCLAIMED_POINTS_COOKIE, "eyJnIjoiZmFrZSIsImIiOiJmYWtlIiwicCI6OTk5OTk5fQ.bad_hmac")
    r_tamper = guest_client.get("/api/loyalty/status")
    assert r_tamper.json()["unclaimed"]["points"] == 150

    # OVERWRITE TEST: Same guest completes Bill #2 (RM 8.40 -> 80 pts) WITHOUT signing in!
    bill2_id = _create_open_bill(8.40, client_token="ctok_guest_1", guest_session_id=gsid)
    assert _simulate_verified_curlec_capture(bill2_id) is True

    r_after_b2 = guest_client.get("/api/loyalty/status")
    st2 = r_after_b2.json()
    assert st2["unclaimed"]["has_unclaimed"] is True
    assert st2["unclaimed"]["points"] == 80, f"Expected overwrite to 80 pts (not accumulated 230), got {st2['unclaimed']['points']}"
    assert st2["unclaimed"]["bill_id"] == bill2_id

    # Verify Bill #1 in DB is marked 'overwritten'
    db = SessionLocal()
    b1_row = db.query(SessionBill).filter(SessionBill.id == bill1_id).first()
    assert b1_row.points_status == "overwritten"
    db.close()
    print("   [OK] Guest unclaimed retention, tamper immunity & automatic overwrite verified.")

    print("3. Testing Google Sign-In auto-claim & multi-account / duplicate claim prevention...")
    mock_google_user_a = {
        "google_id": "google_uid_alice_101",
        "email": "alice@example.com",
        "name": "Alice Tan",
        "avatar_url": "https://example.com/alice.png",
    }
    mock_google_user_b = {
        "google_id": "google_uid_bob_202",
        "email": "bob@example.com",
        "name": "Bob Lim",
        "avatar_url": "",
    }

    with patch("backend.customer_routes.verify_google_credential_token", return_value=mock_google_user_a):
        r_login_a = guest_client.post(
            "/api/customer/auth/google",
            json={"credential": "mock_valid_google_jwt_alice", "session_id": bill2_id},
        )
        assert r_login_a.status_code == 200, r_login_a.text
        login_data = r_login_a.json()
        assert login_data["ok"] is True
        assert login_data["claimed"]["claimed"] is True
        assert login_data["claimed"]["points"] == 80
        assert login_data["user"]["points_balance"] == 80
        alice_id = login_data["user"]["id"]

    # Signing in again with Alice OR Bob trying to claim the same session/bill2/bill1 MUST claim 0!
    bob_client = TestClient(app)
    bob_client.cookies.set("afrc_client_id", "ctok_guest_1")
    with patch("backend.customer_routes.verify_google_credential_token", return_value=mock_google_user_b):
        r_login_b = bob_client.post(
            "/api/customer/auth/google",
            json={"credential": "mock_valid_google_jwt_bob", "session_id": bill2_id},
        )
        assert r_login_b.status_code == 200
        assert r_login_b.json()["claimed"]["claimed"] is False
        assert r_login_b.json()["user"]["points_balance"] == 0

    # Also test trying to claim overwritten Bill #1 with Bob -> must fail (0 points)!
    with patch("backend.customer_routes.verify_google_credential_token", return_value=mock_google_user_b):
        r_login_b_old = bob_client.post(
            "/api/customer/auth/google",
            json={"credential": "mock_valid_google_jwt_bob", "session_id": bill1_id},
        )
        assert r_login_b_old.json()["claimed"]["claimed"] is False
        assert r_login_b_old.json()["user"]["points_balance"] == 0
    print("   [OK] Google Sign-In auto-transfer & anti-duplicate claim protections verified.")

    print("4. Testing concurrent payment capture idempotency (10 parallel threads)...")
    bill3_id = _create_open_bill(20.00, client_token="ctok_alice", customer_user_id=alice_id)
    db = SessionLocal()
    b3 = db.query(SessionBill).filter(SessionBill.id == bill3_id).first()
    b3.status = "paid"
    b3.curlec_payment_id = "pay_concurrent_test_3"
    db.commit()
    db.close()

    def _worker():
        wdb = SessionLocal()
        try:
            return process_verified_paid_bill(wdb, bill3_id)
        finally:
            wdb.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _worker(), range(10)))
    assert all(r.get("processed") for r in results)

    # Alice had 80 pts + 200 pts (only ONCE!) == 280 pts!
    r_pts = guest_client.get("/api/customer/points")
    assert r_pts.status_code == 200
    assert r_pts.json()["balance"] == 280, f"Expected 280 pts after concurrent test, got {r_pts.json()['balance']}"
    print("   [OK] Concurrent capture idempotency verified (awarded once: 80 + 200 = 280 pts).")

    print("5. Testing point redemption, cooldown, and checkout voucher application...")
    # Alice redeems 200 pts -> RM 1.00 voucher
    r_red = guest_client.post("/api/customer/points/redeem", json={"points": 200})
    assert r_red.status_code == 200, r_red.text
    voucher_code = r_red.json()["code"]
    assert len(voucher_code) == 6
    assert r_red.json()["new_balance"] == 80

    # Immediate 2nd redemption blocked by 12h cooldown (even if she had points; let's check cooldown after admin adds 300 pts)
    db = SessionLocal()
    admin = AdminUser(username="testadmin", password_hash=hash_password("AdminPass123!"))
    db.add(admin)
    db.commit()
    db.refresh(admin)
    admin_token = create_admin_session(db, admin.id)
    db.close()

    admin_client = TestClient(app)
    admin_client.cookies.set("afrc_admin_session", admin_token)

    r_adj = admin_client.post(
        "/api/admin/loyalty/points/adjust",
        json={"user_id": alice_id, "action": "add", "points": 200, "description": "Bonus test"},
    )
    assert r_adj.status_code == 200
    assert r_adj.json()["balance"] == 280

    # Try redeeming again while 12h cooldown is active -> must return 400 cooldown error!
    r_red_cd = guest_client.post("/api/customer/points/redeem", json={"points": 200})
    assert r_red_cd.status_code == 400
    assert "cooldown" in r_red_cd.json()["detail"].lower() or "every 12 hours" in r_red_cd.json()["detail"].lower()

    # Apply voucher_code to a new open bill (RM 12.00 food subtotal -> RM 1.00 off -> RM 11.00 payable)
    bill4_id = _create_open_bill(12.00, client_token="ctok_alice", customer_user_id=alice_id)
    db = SessionLocal()
    b4 = db.query(SessionBill).filter(SessionBill.id == bill4_id).first()
    b4.status = "open"
    db.commit()
    db.close()

    guest_client.cookies.set("afrc_client_id", "ctok_alice")
    r_apply = guest_client.post(f"/api/sessions/{bill4_id}/voucher", json={"code": voucher_code})
    assert r_apply.status_code == 200, r_apply.text
    b4_payload = r_apply.json()
    assert b4_payload["subtotal"] == 12.00
    assert b4_payload["discount_amount"] == 1.00
    assert b4_payload["total"] == 11.00
    assert b4_payload["potential_points"] == 120

    # Put curlec_order_id back and capture RM 11.00 (1100 sen)
    db = SessionLocal()
    b4 = db.query(SessionBill).filter(SessionBill.id == bill4_id).first()
    b4.curlec_order_id = "order_bill4_disc"
    b4.status = "pending"
    db.commit()
    db.close()

    assert _simulate_verified_curlec_capture(bill4_id) is True
    # Alice had 280 pts + 120 pts == 400 pts, and voucher is now exhausted!
    assert guest_client.get("/api/customer/points").json()["balance"] == 400
    v_list = guest_client.get("/api/customer/vouchers?activeOnly=true").json()
    assert all(v["code"] != voucher_code for v in v_list), "Used voucher must no longer appear in activeOnly"
    print("   [OK] Tier redemption, cooldown, and voucher checkout verified.")

    print("6. Testing refund clawback, negative balance Points Debt & voucher restoration...")
    # Admin disables cooldown & deducts 350 pts from Alice so she has 50 pts left before refunding Bill #4 (120 pts)
    assert admin_client.put("/api/admin/loyalty/settings", json={"voucher_cooldown_hours": 0}).status_code == 200
    assert admin_client.post(
        "/api/admin/loyalty/points/adjust",
        json={"user_id": alice_id, "action": "deduct", "points": 350, "description": "Test deduct before refund"},
    ).json()["balance"] == 50

    # Clear curlec_payment_id on bill4 so admin refund runs in manual/offline test mode without hitting live Razorpay API
    db = SessionLocal()
    b4 = db.query(SessionBill).filter(SessionBill.id == bill4_id).first()
    b4.curlec_payment_id = ""
    db.commit()
    db.close()

    r_rfnd = admin_client.post(
        f"/api/admin/bills/{bill4_id}/refund",
        json={"items": [{"name": "Chicken", "quantity": 1}], "reason": "Test full refund"},
    )
    assert r_rfnd.status_code == 200, r_rfnd.text
    rf_loy = r_rfnd.json()["refund"]["loyalty"]
    assert rf_loy["clawed_back_points"] == 120
    assert rf_loy["voucher_restored"] is True

    # Alice had 50 pts - 120 clawed back == -70 pts (Points Debt!)
    pts_after_rfnd = guest_client.get("/api/customer/points").json()
    assert pts_after_rfnd["balance"] == -70
    assert pts_after_rfnd["debtPoints"] == 70
    assert "Points debt: 70" in pts_after_rfnd["debtMessage"]

    # Restored voucher exists again, BUT Alice cannot validate/apply it while in Points Debt!
    r_val_debt = guest_client.post(
        "/api/customer/vouchers/validate",
        json={"code": voucher_code, "subtotal": 20.0},
    )
    assert r_val_debt.status_code == 400
    assert "frozen" in r_val_debt.json()["detail"].lower()
    print("   [OK] Refund clawback, negative Points Debt freeze & voucher restoration verified.")

    print("7. Testing Admin overview, manual voucher CSV export, customer suspension & guest clear...")
    r_ov = admin_client.get("/api/admin/loyalty/overview")
    assert r_ov.status_code == 200
    assert r_ov.json()["total_customers"] == 2

    r_mv = admin_client.post(
        "/api/admin/loyalty/vouchers",
        json={
            "code": "WELCOME5",
            "type": "fixed",
            "discount_value": 5.0,
            "min_spend": 15.0,
            "max_uses": 50,
            "expires_at": "2027-12-31",
        },
    )
    assert r_mv.status_code == 200
    r_csv = admin_client.get("/api/admin/loyalty/vouchers/export")
    assert r_csv.status_code == 200
    assert "WELCOME5" in r_csv.text

    # Suspend Alice -> immediately revokes her session & blocks /api/customer/me
    r_susp = admin_client.patch(
        f"/api/admin/loyalty/customers/{alice_id}/suspension",
        json={"is_suspended": True},
    )
    assert r_susp.status_code == 200
    assert guest_client.get("/api/customer/me").status_code in (401, 403)

    # Unsuspend Alice
    assert admin_client.patch(
        f"/api/admin/loyalty/customers/{alice_id}/suspension",
        json={"is_suspended": False},
    ).status_code == 200

    # Guest clear endpoint test
    fresh_guest = TestClient(app)
    r_fg0 = fresh_guest.get("/api/loyalty/status")
    fg_gsid = verify_guest_session_cookie(fresh_guest.cookies.get("afrc_guest_loyalty", ""))
    b_clear = _create_open_bill(10.0, client_token="ctok_clear", guest_session_id=fg_gsid)
    assert _simulate_verified_curlec_capture(b_clear) is True
    assert fresh_guest.get("/api/loyalty/status").json()["unclaimed"]["points"] == 100
    r_clr = fresh_guest.post("/api/loyalty/guest/clear")
    assert r_clr.status_code == 200
    assert fresh_guest.get("/api/loyalty/status").json()["unclaimed"]["has_unclaimed"] is False
    print("   [OK] Admin APIs, CSV export, customer suspension & guest clear verified.")

    print("\nALL LOYALTY & ACCOUNT E2E TESTS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    run_all_tests()
