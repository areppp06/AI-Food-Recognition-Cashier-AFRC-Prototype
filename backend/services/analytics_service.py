"""Admin dashboard analytics and KPI reporting service."""
from __future__ import annotations

import calendar
import json
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from ..db import ScanAttempt, SelfServiceBill, SessionBill
from .config_service import _local_tz_offset, _rate_limit_config, _restaurant_profile, _shift_year_month
from .menu_service import _dish_alias_map


def _compute_overview_data(
    db: Session,
    days: int = 7,
    period: str = "",
    month: str = "",
    quarter: str = "",
    include_export_bills: bool = False,
    channel: str = "all",
) -> dict:
    offset = _local_tz_offset()
    now_local = datetime.utcnow() + offset
    today_date = now_local.date()
    week_start_date = today_date - timedelta(days=6)
    max_retakes = _rate_limit_config()["max_retakes"]
    ch_norm = (channel or "all").strip().lower()
    if ch_norm not in ("all", "afrc", "selfservice"):
        ch_norm = "all"

    afrc_rows = (
        db.query(
            SessionBill.id,
            SessionBill.bill_number,
            SessionBill.status,
            SessionBill.void_reason,
            SessionBill.final_bill,
            SessionBill.receipt_email,
            SessionBill.receipt_phone,
            SessionBill.curlec_payment_id,
            SessionBill.retake_count,
            SessionBill.discount_amount,
            SessionBill.created_at,
            SessionBill.updated_at,
            SessionBill.paid_at,
        )
        .order_by(SessionBill.created_at.desc())
        .all()
    )
    ss_raw = (
        db.query(SelfServiceBill)
        .order_by(SelfServiceBill.created_at.desc())
        .all()
    )
    ss_rows = [
        (
            b.id,
            b.bill_number,
            b.status,
            b.void_reason,
            b.items_json,
            b.receipt_email,
            "",
            b.chip_purchase_id,
            0,
            0.0,
            b.created_at,
            b.updated_at,
            b.paid_at,
        )
        for b in ss_raw
    ]
    if ch_norm == "afrc":
        rows = afrc_rows
    elif ch_norm == "selfservice":
        rows = ss_rows
    else:
        rows = sorted(
            list(afrc_rows) + ss_rows,
            key=lambda r: (r[10] or r[11] or datetime.min),
            reverse=True,
        )

    try:
        days_int = int(days)
    except (TypeError, ValueError):
        days_int = 7

    p_norm = (period or "").strip().lower()
    if p_norm not in ("today", "7d", "30d", "monthly", "quarterly", "all"):
        if days_int == 1:
            p_norm = "today"
        elif days_int == 30:
            p_norm = "30d"
        elif days_int == 0:
            p_norm = "all"
        else:
            p_norm = "7d"

    month_str = (month or "").strip()
    quarter_str = (quarter or "").strip().upper()

    available_months = []
    for delta in range(0, -24, -1):
        ym_y, ym_m = _shift_year_month(today_date.year, today_date.month, delta)
        available_months.append({
            "value": f"{ym_y:04d}-{ym_m:02d}",
            "label": date(ym_y, ym_m, 1).strftime("%B %Y"),
        })

    cur_q = (today_date.month - 1) // 3 + 1
    available_quarters = []
    q_month_span = {1: "Jan-Mar", 2: "Apr-Jun", 3: "Jul-Sep", 4: "Oct-Dec"}
    for q_idx in range(8):
        total_q = today_date.year * 4 + (cur_q - 1) - q_idx
        qy, qm_idx = divmod(total_q, 4)
        qn = qm_idx + 1
        available_quarters.append({
            "value": f"{qy:04d}-Q{qn}",
            "label": f"Q{qn} {qy} ({q_month_span[qn]})",
        })

    start_date: date | None = None
    end_date: date | None = None
    granularity = "day"
    period_label = "Last 7 Days"
    file_tag = "last-7d"
    selected_month = ""
    selected_quarter = ""

    buckets: dict[str, dict] = {}
    bucket_order: list[str] = []

    def _add_bucket(key: str, label: str) -> None:
        if key not in buckets:
            bucket_order.append(key)
            buckets[key] = {
                "key": key,
                "label": label,
                "revenue": 0.0,
                "paid_count": 0,
                "void_count": 0,
                "open_count": 0,
                "total_count": 0,
                "scan_count": 0,
            }

    if p_norm == "today":
        days_int = 1
        start_date = today_date
        end_date = today_date
        granularity = "hour"
        period_label = f"Today, {today_date.strftime('%d %b %Y')} (Hourly)"
        file_tag = "today"
        for h in range(24):
            lbl = f"{h:02d}:00"
            _add_bucket(lbl, lbl)
    elif p_norm == "7d":
        days_int = 7
        start_date = today_date - timedelta(days=6)
        end_date = today_date
        granularity = "day"
        period_label = f"Last 7 Days ({start_date.strftime('%d %b %Y')} - {end_date.strftime('%d %b %Y')})"
        file_tag = "last-7d"
        for i in range(6, -1, -1):
            d = today_date - timedelta(days=i)
            _add_bucket(d.isoformat(), d.strftime("%d %b %Y"))
    elif p_norm == "30d":
        days_int = 30
        start_date = today_date - timedelta(days=29)
        end_date = today_date
        granularity = "day"
        period_label = f"Last 30 Days ({start_date.strftime('%d %b %Y')} - {end_date.strftime('%d %b %Y')})"
        file_tag = "last-30d"
        for i in range(29, -1, -1):
            d = today_date - timedelta(days=i)
            _add_bucket(d.isoformat(), d.strftime("%d %b %Y"))
    elif p_norm == "monthly":
        days_int = 30
        parsed_m = None
        if len(month_str) == 7 and month_str[4] == "-":
            try:
                my = int(month_str[:4])
                mm = int(month_str[5:7])
                if 2000 <= my <= 2100 and 1 <= mm <= 12:
                    parsed_m = (my, mm)
            except ValueError:
                parsed_m = None
        if parsed_m:
            my, mm = parsed_m
            selected_month = f"{my:04d}-{mm:02d}"
            last_day = calendar.monthrange(my, mm)[1]
            start_date = date(my, mm, 1)
            end_date = date(my, mm, last_day)
            granularity = "day"
            period_label = start_date.strftime("%B %Y")
            file_tag = f"month-{selected_month}"
            for day_num in range(1, last_day + 1):
                d = date(my, mm, day_num)
                _add_bucket(d.isoformat(), d.strftime("%d %b %Y"))
        else:
            oy, om = _shift_year_month(today_date.year, today_date.month, -11)
            start_date = date(oy, om, 1)
            end_date = today_date
            granularity = "month"
            period_label = f"Last 12 Months ({start_date.strftime('%b %Y')} - {end_date.strftime('%b %Y')})"
            file_tag = "monthly-12m"
            for delta in range(-11, 1):
                ym_y, ym_m = _shift_year_month(today_date.year, today_date.month, delta)
                m_key = f"{ym_y:04d}-{ym_m:02d}"
                m_lbl = date(ym_y, ym_m, 1).strftime("%b %Y")
                _add_bucket(m_key, m_lbl)
    elif p_norm == "quarterly":
        days_int = 90
        parsed_q = None
        if len(quarter_str) == 7 and quarter_str[4:6] == "-Q":
            try:
                qy = int(quarter_str[:4])
                qn = int(quarter_str[6])
                if 2000 <= qy <= 2100 and 1 <= qn <= 4:
                    parsed_q = (qy, qn)
            except ValueError:
                parsed_q = None
        if parsed_q:
            qy, qn = parsed_q
            selected_quarter = f"{qy:04d}-Q{qn}"
            m_start = (qn - 1) * 3 + 1
            m_end = qn * 3
            start_date = date(qy, m_start, 1)
            end_date = date(qy, m_end, calendar.monthrange(qy, m_end)[1])
            granularity = "month"
            period_label = f"Q{qn} {qy} ({q_month_span[qn]})"
            file_tag = f"quarter-{selected_quarter}"
            for mm in range(m_start, m_end + 1):
                m_key = f"{qy:04d}-{mm:02d}"
                m_lbl = date(qy, mm, 1).strftime("%b %Y")
                _add_bucket(m_key, m_lbl)
        else:
            start_total_q = today_date.year * 4 + (cur_q - 1) - 3
            sy, sqm_idx = divmod(start_total_q, 4)
            sq = sqm_idx + 1
            start_date = date(sy, (sq - 1) * 3 + 1, 1)
            end_date = today_date
            granularity = "quarter"
            period_label = f"Last 4 Quarters ({start_date.strftime('%b %Y')} - {end_date.strftime('%b %Y')})"
            file_tag = "quarterly-4q"
            for q_idx in range(3, -1, -1):
                tq = today_date.year * 4 + (cur_q - 1) - q_idx
                by, bqm = divmod(tq, 4)
                bn = bqm + 1
                _add_bucket(f"{by:04d}-Q{bn}", f"Q{bn} {by:04d}")
    else:
        days_int = 0
        start_date = None
        end_date = None
        granularity = "day"
        period_label = f"All Time (up to {today_date.strftime('%d %b %Y')})"
        file_tag = "all-time"

    def _bucket_info(dt_local: datetime) -> tuple[str, str]:
        if granularity == "hour":
            lbl = f"{dt_local.hour:02d}:00"
            return lbl, lbl
        if granularity == "month":
            return f"{dt_local.year:04d}-{dt_local.month:02d}", dt_local.strftime("%b %Y")
        if granularity == "quarter":
            qq = (dt_local.month - 1) // 3 + 1
            return f"{dt_local.year:04d}-Q{qq}", f"Q{qq} {dt_local.year:04d}"
        d_val = dt_local.date()
        return d_val.isoformat(), d_val.strftime("%d %b %Y")

    def _in_date_range(d_val: date | None) -> bool:
        if d_val is None:
            return False
        if start_date is not None and d_val < start_date:
            return False
        if end_date is not None and d_val > end_date:
            return False
        return True

    all_time_revenue = 0.0
    today_revenue = 0.0
    weekly_revenue = 0.0
    today_paid_count = 0
    weekly_paid_count = 0
    today_bills = 0
    today_pending_count = 0
    today_void_count = 0
    paid_count = 0
    pending_count = 0
    void_count = 0

    period_revenue = 0.0
    period_paid = 0
    period_open = 0
    period_void = 0
    period_void_value = 0.0
    dish_agg: dict[str, dict] = {}
    void_agg: dict[str, dict] = {}
    export_bills: list[dict] = []
    alias_map = _dish_alias_map(db)

    for row in rows:
        (
            _sid,
            bill_no,
            raw_status,
            raw_void_reason,
            final_bill,
            receipt_email,
            receipt_phone,
            curlec_payment_id,
            retake_count,
            discount_amt,
            created_at,
            updated_at,
            paid_at,
        ) = row

        st = (raw_status or "open").lower()
        try:
            items = json.loads(final_bill or "[]")
            raw_sub = round(sum(float(i.get("subtotal") or 0) for i in items), 2)
            bill_total = round(max(0.0, raw_sub - float(discount_amt or 0.0)), 2)
        except Exception:
            items = []
            bill_total = 0.0

        created_utc = created_at or updated_at or datetime.utcnow()
        created_local = created_utc + offset
        created_d = created_local.date()

        paid_local = ((paid_at or created_utc) + offset) if st == "paid" else None
        paid_d = paid_local.date() if paid_local else None

        is_void = st in ("void", "cancelled")
        is_open = st in ("open", "pending", "retaking")
        void_reason = (raw_void_reason or "").strip()
        if is_void and not void_reason:
            void_reason = (
                f"Exceeded {max_retakes} Retries Limit"
                if int(retake_count or 0) >= max_retakes
                else "Bill timed out"
            )

        if created_d == today_date:
            today_bills += 1
            if is_open:
                today_pending_count += 1
            elif is_void:
                today_void_count += 1

        if st == "paid":
            paid_count += 1
            all_time_revenue += bill_total
            if paid_d == today_date:
                today_revenue += bill_total
                today_paid_count += 1
            if paid_d and week_start_date <= paid_d <= today_date:
                weekly_revenue += bill_total
                weekly_paid_count += 1
        elif is_open:
            pending_count += 1
        elif is_void:
            void_count += 1

        in_period_created = _in_date_range(created_d)
        in_period_paid = (st == "paid") and _in_date_range(paid_d)

        if in_period_paid:
            period_revenue += bill_total
            period_paid += 1
            pkey, plbl = _bucket_info(paid_local)
            _add_bucket(pkey, plbl)
            buckets[pkey]["revenue"] = round(buckets[pkey]["revenue"] + bill_total, 2)
            buckets[pkey]["paid_count"] += 1
            buckets[pkey]["total_count"] += 1
            for item in items:
                raw_d = str(item.get("name") or "Unknown").strip() or "Unknown"
                d_name = alias_map.get(raw_d) or alias_map.get(raw_d.casefold()) or raw_d
                qty = float(item.get("quantity") or 0)
                sub = float(item.get("subtotal") or 0)
                entry = dish_agg.setdefault(d_name, {"name": d_name, "quantity": 0.0, "revenue": 0.0})
                entry["quantity"] += qty
                entry["revenue"] += sub

        if in_period_created:
            ckey, clbl = _bucket_info(created_local)
            _add_bucket(ckey, clbl)
            if is_void:
                period_void += 1
                period_void_value += bill_total
                buckets[ckey]["void_count"] += 1
                buckets[ckey]["total_count"] += 1
                ventry = void_agg.setdefault(void_reason, {"reason": void_reason, "count": 0, "lost_value": 0.0})
                ventry["count"] += 1
                ventry["lost_value"] += bill_total
            elif is_open:
                period_open += 1
                buckets[ckey]["open_count"] += 1
                buckets[ckey]["total_count"] += 1

        if include_export_bills and (in_period_created or in_period_paid):
            item_desc = "; ".join(
                f"{(alias_map.get(i.get('name', '')) or alias_map.get(str(i.get('name', '')).casefold()) or i.get('name', ''))} x{i.get('quantity', 0)}"
                for i in items
            )
            export_bills.append({
                "bill_number": bill_no or "AAA-0001",
                "created_at_local": created_local.strftime("%Y-%m-%d %H:%M:%S"),
                "paid_at_local": paid_local.strftime("%Y-%m-%d %H:%M:%S") if paid_local else "",
                "status": "void" if is_void else st,
                "void_reason": void_reason if is_void else "",
                "total": bill_total,
                "items": item_desc,
                "receipt_email": receipt_email or "",
                "receipt_phone": receipt_phone or "",
                "curlec_payment_id": curlec_payment_id or "",
            })

    scan_rows = db.query(ScanAttempt.created_at).all()
    today_scans = 0
    period_scans = 0
    total_scans = len(scan_rows)

    for (s_created,) in scan_rows:
        s_utc = s_created or datetime.utcnow()
        s_local = s_utc + offset
        s_d = s_local.date()
        if s_d == today_date:
            today_scans += 1
        if _in_date_range(s_d):
            period_scans += 1
            skey, slbl = _bucket_info(s_local)
            _add_bucket(skey, slbl)
            buckets[skey]["scan_count"] = buckets[skey].get("scan_count", 0) + 1

    if p_norm == "all":
        bucket_order = sorted(buckets.keys())

    series = [buckets[k] for k in bucket_order]
    top_dishes = sorted(
        (
            {
                "name": v["name"],
                "quantity": round(v["quantity"], 2),
                "revenue": round(v["revenue"], 2),
            }
            for v in dish_agg.values()
        ),
        key=lambda x: (-x["quantity"], -x["revenue"], x["name"]),
    )[:10]
    void_causes = sorted(
        (
            {
                "reason": v["reason"],
                "count": v["count"],
                "lost_value": round(v["lost_value"], 2),
            }
            for v in void_agg.values()
        ),
        key=lambda x: (-x["count"], -x["lost_value"], x["reason"]),
    )

    afrc_today_rev = round(
        sum(
            max(0.0, sum(float(i.get("subtotal") or 0) for i in json.loads(r[4] or "[]")) - float(r[9] or 0.0))
            for r in afrc_rows
            if (r[2] or "").lower() == "paid" and ((r[12] or r[10] or r[11] or datetime.utcnow()) + offset).date() == today_date
        ),
        2,
    )
    ss_today_rev = round(
        sum(
            float(b.total_amount or 0.0)
            for b in ss_raw
            if (b.status or "").lower() == "paid" and ((b.paid_at or b.created_at or b.updated_at or datetime.utcnow()) + offset).date() == today_date
        ),
        2,
    )

    summary = {
        "channel": ch_norm,
        "today_revenue": round(today_revenue, 2),
        "today_paid_count": today_paid_count,
        "weekly_revenue": round(weekly_revenue, 2),
        "weekly_paid_count": weekly_paid_count,
        "today_bills": today_bills,
        "today_pending_count": today_pending_count,
        "today_void_count": today_void_count,
        "today_scans": today_scans,
        "total_scans": total_scans,
        "all_time_revenue": round(all_time_revenue, 2),
        "total_revenue": round(all_time_revenue, 2),
        "total_bills": len(rows),
        "paid_count": paid_count,
        "pending_count": pending_count,
        "void_count": void_count,
        "channel_breakdown": {
            "afrc_today_revenue": afrc_today_rev,
            "selfservice_today_revenue": ss_today_rev,
        },
    }
    profile = _restaurant_profile()
    summary["restaurant_name"] = profile.get("restaurant_name", "AFRC")
    summary["company_name"] = profile.get("company_name", "")
    analytics = {
        "channel": ch_norm,
        "period": p_norm,
        "month": selected_month,
        "quarter": selected_quarter,
        "days": days_int,
        "period_label": period_label,
        "file_tag": file_tag,
        "restaurant_name": profile.get("restaurant_name", "AFRC"),
        "company_name": profile.get("company_name", ""),
        "generated_at_local": now_local.strftime("%Y-%m-%d %H:%M:%S MYT"),
        "granularity": granularity,
        "available_months": available_months,
        "available_quarters": available_quarters,
        "period_totals": {
            "revenue": round(period_revenue, 2),
            "paid_count": period_paid,
            "open_count": period_open,
            "void_count": period_void,
            "void_value": round(period_void_value, 2),
            "total_bills": period_paid + period_open + period_void,
            "scan_count": period_scans,
        },
        "series": series,
        "top_dishes": top_dishes,
        "void_causes": void_causes,
    }
    res = {"summary": summary, "analytics": analytics}
    if include_export_bills:
        res["export_bills"] = export_bills
    return res
