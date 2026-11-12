import os
import sys
import json
import random
import uuid
from pathlib import Path
from datetime import datetime, timedelta
from backend.db import SessionLocal, SessionBill, BillRefund, AuditLog, Dish, format_bill_number

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_FIXTURE_PATH = ROOT_DIR / "data" / "mock_seed.json"

def load_seed_fixture():
    if not DATA_FIXTURE_PATH.is_file():
        print(f"Fixture not found at {DATA_FIXTURE_PATH}. Generating fresh mock data instead...")
        generate_and_seed_database(95)
        return

    print(f"Loading mock data fixture from {DATA_FIXTURE_PATH}...")
    with open(DATA_FIXTURE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    db = SessionLocal()
    try:
        # Clear previous session data
        db.query(BillRefund).delete()
        db.query(SessionBill).delete()
        db.query(AuditLog).delete()
        db.commit()

        # Parse & insert sessions
        session_objs = []
        for s in data.get("sessions", []):
            session_objs.append(SessionBill(
                id=s["id"],
                bill_number=s.get("bill_number", ""),
                raw_ai=s.get("raw_ai", "[]"),
                final_bill=s.get("final_bill", "[]"),
                status=s.get("status", "open"),
                void_reason=s.get("void_reason", ""),
                receipt_email=s.get("receipt_email", ""),
                receipt_phone=s.get("receipt_phone", ""),
                curlec_order_id=s.get("curlec_order_id", ""),
                curlec_payment_id=s.get("curlec_payment_id", ""),
                retake_count=s.get("retake_count", 0),
                client_token=s.get("client_token", ""),
                created_at=datetime.fromisoformat(s["created_at"]) if s.get("created_at") else datetime.utcnow(),
                updated_at=datetime.fromisoformat(s["updated_at"]) if s.get("updated_at") else datetime.utcnow(),
                paid_at=datetime.fromisoformat(s["paid_at"]) if s.get("paid_at") else None,
            ))
        db.bulk_save_objects(session_objs)
        db.commit()

        # Parse & insert bill refunds
        for r in data.get("bill_refunds", []):
            db.add(BillRefund(
                session_id=r["session_id"],
                bill_number=r.get("bill_number", ""),
                curlec_payment_id=r.get("curlec_payment_id", ""),
                curlec_refund_id=r.get("curlec_refund_id", ""),
                refund_amount=float(r.get("refund_amount", 0.0)),
                refunded_items=r.get("refunded_items", "[]"),
                reason=r.get("reason", ""),
                status=r.get("status", "processed"),
                created_at=datetime.fromisoformat(r["created_at"]) if r.get("created_at") else datetime.utcnow(),
            ))

        # Parse & insert audit logs
        for a in data.get("audit_logs", []):
            db.add(AuditLog(
                cashier_id=a.get("cashier_id", 0),
                action=a.get("action", ""),
                details=a.get("details", ""),
                timestamp=datetime.fromisoformat(a["timestamp"]) if a.get("timestamp") else datetime.utcnow(),
            ))
        db.commit()

        print(f"Successfully loaded fixture into database:")
        print(f"  - Sessions: {len(session_objs)}")
        print(f"  - Refunds: {len(data.get('bill_refunds', []))}")
        print(f"  - Audit Logs: {len(data.get('audit_logs', []))}")

    except Exception as e:
        db.rollback()
        print(f"Error loading fixture: {e}")
        raise
    finally:
        db.close()

def export_db_to_fixture():
    db = SessionLocal()
    os.makedirs(ROOT_DIR / "data", exist_ok=True)
    try:
        sessions_data = []
        for s in db.query(SessionBill).order_by(SessionBill.created_at.asc()).all():
            sessions_data.append({
                "id": s.id,
                "bill_number": s.bill_number,
                "raw_ai": s.raw_ai,
                "final_bill": s.final_bill,
                "status": s.status,
                "void_reason": s.void_reason,
                "receipt_email": s.receipt_email,
                "receipt_phone": s.receipt_phone,
                "curlec_order_id": s.curlec_order_id,
                "curlec_payment_id": s.curlec_payment_id,
                "retake_count": s.retake_count,
                "client_token": s.client_token,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
                "paid_at": s.paid_at.isoformat() if s.paid_at else None,
            })

        refunds_data = []
        for r in db.query(BillRefund).order_by(BillRefund.created_at.asc()).all():
            refunds_data.append({
                "session_id": r.session_id,
                "bill_number": r.bill_number,
                "curlec_payment_id": r.curlec_payment_id,
                "curlec_refund_id": r.curlec_refund_id,
                "refund_amount": r.refund_amount,
                "refunded_items": r.refunded_items,
                "reason": r.reason,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })

        audit_data = []
        for a in db.query(AuditLog).order_by(AuditLog.timestamp.asc()).all():
            audit_data.append({
                "cashier_id": a.cashier_id,
                "action": a.action,
                "details": a.details,
                "timestamp": a.timestamp.isoformat() if a.timestamp else None,
            })

        fixture = {
            "total_sessions": len(sessions_data),
            "total_refunds": len(refunds_data),
            "total_audit_logs": len(audit_data),
            "sessions": sessions_data,
            "bill_refunds": refunds_data,
            "audit_logs": audit_data,
        }

        with open(DATA_FIXTURE_PATH, "w", encoding="utf-8") as f:
            json.dump(fixture, f, indent=2)

        print(f"Exported {len(sessions_data)} sessions, {len(refunds_data)} refunds, {len(audit_data)} audit logs to {DATA_FIXTURE_PATH}")
    finally:
        db.close()

def generate_and_seed_database(num_transactions: int = 95):
    db = SessionLocal()
    try:
        dishes = db.query(Dish).all()
        if not dishes:
            print("No dishes found in database. Please seed dishes first.")
            return

        dish_pool = [{"name": d.name, "price": float(d.price)} for d in dishes]

        db.query(BillRefund).delete()
        db.query(SessionBill).delete()
        db.query(AuditLog).delete()
        db.commit()

        start_date = datetime(2025, 5, 1, 9, 0, 0)
        end_date = datetime(2026, 11, 13, 14, 0, 0)
        total_seconds = int((end_date - start_date).total_seconds())

        random_offsets = sorted(random.sample(range(total_seconds), num_transactions))
        timestamps = [start_date + timedelta(seconds=offset) for offset in random_offsets]

        now = datetime.utcnow()
        timestamps[-1] = now - timedelta(minutes=random.randint(10, 45))
        timestamps[-2] = now - timedelta(hours=random.randint(2, 4))
        timestamps[-3] = now - timedelta(days=1, hours=random.randint(1, 5))

        indices = list(range(num_transactions - 2))
        random.shuffle(indices)

        void_indices = set(indices[:15])
        full_refund_indices = set(indices[15:23])
        partial_refund_indices = set(indices[23:31])
        paid_indices = set(indices[31:])
        pending_indices = {num_transactions - 2, num_transactions - 1}

        # Real system void reasons guaranteed representation
        guaranteed_void_reasons = (
            ["Bill timed out"] * 6
            + ["Customer Cancelled"] * 4
            + ["Needed Cashier Assistance"] * 2
            + ["Exceeded 3 Retries Limit"] * 2
            + ["Voided by Admin"] * 1
        )
        random.shuffle(guaranteed_void_reasons)
        void_reasons_map = {idx: guaranteed_void_reasons[i] for i, idx in enumerate(sorted(void_indices))}

        customer_phones = [
            "+60 12-345 6789", "+60 11-218 1510", "+60 19-876 5432",
            "+60 17-234 5678", "+60 16-987 6543", "+60 13-456 7890",
            "+60 14-321 0987", "+60 18-765 4321", "+60 12-901 2345"
        ]
        customer_emails = [
            "syed@example.com", "hafiz@takcakap.com", "azman.k@gmail.com",
            "sarah.w@yahoo.com", "nurul.a@hotmail.com", "chong.wl@gmail.com",
            "siti.aminah@gmail.com", "kumar.r@gmail.com", "farid.pos@gmail.com"
        ]

        def create_tray_items():
            tray = []
            rice = next((d for d in dish_pool if "rice" in d["name"].lower()), {"name": "Rice", "price": 1.0})
            rice_qty = random.choice([1, 1, 1, 2])
            tray.append({
                "name": rice["name"],
                "price": rice["price"],
                "quantity": rice_qty,
                "subtotal": round(rice["price"] * rice_qty, 2)
            })

            proteins = [d for d in dish_pool if any(k in d["name"].lower() for k in ["chicken", "beef", "fish", "lamb", "catfish", "bawal", "sotong"])]
            chosen_proteins = random.sample(proteins, k=random.choice([1, 1, 2]))
            for p in chosen_proteins:
                qty = 1
                tray.append({
                    "name": p["name"],
                    "price": p["price"],
                    "quantity": qty,
                    "subtotal": round(p["price"] * qty, 2)
                })

            sides = [d for d in dish_pool if any(k in d["name"].lower() for k in ["vegetables", "bendi", "egg", "cili"])]
            if sides:
                chosen_sides = random.sample(sides, k=random.choice([1, 1, 2]))
                for s in chosen_sides:
                    qty = random.choice([1, 1, 2]) if "egg" in s["name"].lower() or "cili" in s["name"].lower() else 1
                    tray.append({
                        "name": s["name"],
                        "price": s["price"],
                        "quantity": qty,
                        "subtotal": round(s["price"] * qty, 2)
                    })
            return tray

        created_bills = []
        created_refunds = []
        created_audits = []

        for seq_idx in range(num_transactions):
            bill_seq = seq_idx + 1
            bill_no = format_bill_number(bill_seq)
            t_created = timestamps[seq_idx]
            sid = str(uuid.uuid4())

            tray_items = create_tray_items()
            bill_total = round(sum(i["subtotal"] for i in tray_items), 2)
            raw_ai = json.dumps(tray_items)
            final_bill = json.dumps(tray_items)

            has_contact = random.random() < 0.65
            email = random.choice(customer_emails) if has_contact else ""
            phone = random.choice(customer_phones) if has_contact else ""

            if seq_idx in pending_indices:
                status = "open"
                void_reason = ""
                t_updated = t_created + timedelta(seconds=random.randint(5, 30))
                paid_at = None
                payment_id = ""
                order_id = ""
                retake_count = random.choice([0, 1])

            elif seq_idx in void_indices:
                status = "void"
                void_reason = void_reasons_map[seq_idx]
                retake_count = 3 if "Retries" in void_reason else random.choice([0, 1, 2])
                t_updated = t_created + timedelta(seconds=random.randint(45, 180))
                paid_at = None
                payment_id = ""
                order_id = ""

            else:
                status = "paid"
                void_reason = ""
                retake_count = random.choice([0, 0, 1, 2])
                duration = random.randint(30, 240)
                t_updated = t_created + timedelta(seconds=duration)
                paid_at = t_updated
                order_id = f"ord_mock_{uuid.uuid4().hex[:14]}"
                payment_id = f"pay_mock_{uuid.uuid4().hex[:14]}"

            bill = SessionBill(
                id=sid,
                bill_number=bill_no,
                raw_ai=raw_ai,
                final_bill=final_bill,
                status=status,
                void_reason=void_reason,
                receipt_email=email,
                receipt_phone=phone,
                curlec_order_id=order_id,
                curlec_payment_id=payment_id,
                retake_count=retake_count,
                created_at=t_created,
                updated_at=t_updated,
                paid_at=paid_at
            )
            created_bills.append(bill)

            if seq_idx in full_refund_indices:
                rfnd_id = f"rfnd_mock_{uuid.uuid4().hex[:14]}"
                refund_reasons = [
                    "Customer returned entire tray",
                    "Foreign object reported in food",
                    "Customer left without taking order",
                    "Duplicate card charge error",
                    "Customer dissatisfied with freshness"
                ]
                reason = random.choice(refund_reasons)
                t_refund = paid_at + timedelta(minutes=random.randint(5, 45))

                rf = BillRefund(
                    session_id=sid,
                    bill_number=bill_no,
                    curlec_payment_id=payment_id,
                    curlec_refund_id=rfnd_id,
                    refund_amount=bill_total,
                    refunded_items=json.dumps(tray_items),
                    reason=reason,
                    status="processed",
                    created_at=t_refund
                )
                created_refunds.append(rf)

                item_str = ", ".join(f"{i['quantity']}x {i['name']}" for i in tray_items)
                created_audits.append(AuditLog(
                    cashier_id=0,
                    action="REFUND_ISSUED",
                    details=f"Bill #{bill_no}: RM {bill_total:.2f} ({item_str}) [Curlec {rfnd_id}] - Reason: {reason}",
                    timestamp=t_refund
                ))

            elif seq_idx in partial_refund_indices:
                rfnd_id = f"rfnd_mock_{uuid.uuid4().hex[:14]}"
                non_rice = [i for i in tray_items if "rice" not in i["name"].lower()]
                refund_target = non_rice[0] if non_rice else tray_items[0]
                rf_amount = refund_target["subtotal"]
                rf_items = [{
                    "name": refund_target["name"],
                    "price": refund_target["price"],
                    "quantity": refund_target["quantity"],
                    "subtotal": rf_amount
                }]
                partial_reasons = [
                    f"Cold {refund_target['name']} returned",
                    f"Wrong portion for {refund_target['name']}",
                    f"Customer found {refund_target['name']} too spicy",
                    "Customer charged for dish not taken",
                    "Food spilled before dining"
                ]
                reason = random.choice(partial_reasons)
                t_refund = paid_at + timedelta(minutes=random.randint(3, 30))

                rf = BillRefund(
                    session_id=sid,
                    bill_number=bill_no,
                    curlec_payment_id=payment_id,
                    curlec_refund_id=rfnd_id,
                    refund_amount=rf_amount,
                    refunded_items=json.dumps(rf_items),
                    reason=reason,
                    status="processed",
                    created_at=t_refund
                )
                created_refunds.append(rf)

                created_audits.append(AuditLog(
                    cashier_id=0,
                    action="REFUND_ISSUED",
                    details=f"Bill #{bill_no}: RM {rf_amount:.2f} (1x {refund_target['name']}) [Curlec {rfnd_id}] - Reason: {reason}",
                    timestamp=t_refund
                ))

        db.bulk_save_objects(created_bills)
        db.commit()

        for rf in created_refunds:
            db.add(rf)
        for al in created_audits:
            db.add(al)
        db.commit()

        print(f"Successfully generated & seeded {len(created_bills)} transactions.")
        export_db_to_fixture()

    except Exception as e:
        db.rollback()
        print(f"Error seeding data: {e}")
        raise
    finally:
        db.close()

if __name__ == "__main__":
    if "--export" in sys.argv:
        export_db_to_fixture()
    elif "--generate" in sys.argv:
        generate_and_seed_database(95)
    else:
        # Default: load from fixture if present, or generate
        if DATA_FIXTURE_PATH.is_file():
            load_seed_fixture()
        else:
            generate_and_seed_database(95)
