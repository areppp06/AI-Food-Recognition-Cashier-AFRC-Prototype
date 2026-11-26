"""HTML page routes and robots.txt."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from ..auth import get_current_admin
from ..db import AdminUser, SessionBill
from ..loyalty import get_current_customer, get_or_create_guest_session_id, set_guest_loyalty_cookie, set_unclaimed_points_cookie
from ..services.config_service import FORBIDDEN_SESSION_HTML, NO_CACHE_HEADERS, _restaurant_settings
from ..services.security_service import _get_client_token
from .deps import get_db

router = APIRouter()

ADMIN_PAGES = {
    "dashboard",
    "bills",
    "menu",
    "categories",
    "restaurant",
    "customers",
    "points",
    "vouchers",
    "feedbacks",
    "audit",
    "archives",
}


def _check_admin_access(request: Request, db: Session, next_url: str = "/admin") -> RedirectResponse | None:
    admin_count = db.query(AdminUser).count()
    if admin_count == 0:
        return RedirectResponse(url="/login?setup=1", status_code=303)
    current_admin = get_current_admin(request, db)
    if not current_admin:
        return RedirectResponse(url=f"/login?next={next_url}", status_code=303)
    return None


@router.get("/robots.txt", include_in_schema=False)
def robots_txt():
    return Response(
        content="User-agent: *\nDisallow: /\n",
        media_type="text/plain",
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-Robots-Tag": "noindex, nofollow, noarchive, nosnippet",
        },
    )


@router.get("/")
async def root(request: Request):
    settings = _restaurant_settings()
    if settings["maintenance_mode"]:
        return RedirectResponse("/maintenance", status_code=307)
    if settings["coming_soon"]:
        return RedirectResponse("/coming-soon", status_code=307)
    gsid, is_new = get_or_create_guest_session_id(request)
    resp = FileResponse("frontend/index.html", headers=NO_CACHE_HEADERS)
    if is_new:
        set_guest_loyalty_cookie(resp, request, gsid)
    return resp


@router.get("/coming-soon")
async def coming_soon_page():
    return FileResponse("frontend/coming_soon.html", headers=NO_CACHE_HEADERS)


@router.get("/maintenance")
async def maintenance_page():
    return FileResponse("frontend/maintenance.html", headers=NO_CACHE_HEADERS)


@router.get("/privacy")
@router.get("/privacy-policy")
async def privacy_page():
    return FileResponse("frontend/privacy.html", headers=NO_CACHE_HEADERS)


@router.get("/terms")
@router.get("/terms-and-conditions")
async def terms_page():
    return FileResponse("frontend/terms.html", headers=NO_CACHE_HEADERS)


@router.get("/payment-policy")
@router.get("/refund-policy")
@router.get("/payment-and-refund-policy")
async def payment_policy_page():
    return FileResponse("frontend/payment-policy.html", headers=NO_CACHE_HEADERS)


@router.get("/contact")
@router.get("/contact-us")
async def contact_page():
    return FileResponse("frontend/contact.html", headers=NO_CACHE_HEADERS)


@router.get("/about")
@router.get("/about-us")
async def about_page():
    return FileResponse("frontend/about.html", headers=NO_CACHE_HEADERS)


@router.get("/pricelist")
@router.get("/prices")
async def pricelist_page():
    return FileResponse("frontend/pricelist.html", headers=NO_CACHE_HEADERS)


@router.get("/guide")
@router.get("/how-to-use")
@router.get("/learn")
async def guide_page():
    return FileResponse("frontend/guide.html", headers=NO_CACHE_HEADERS)


@router.get("/login")
async def login_page(request: Request, db: Session = Depends(get_db)):
    current_admin = get_current_admin(request, db)
    if current_admin:
        return RedirectResponse(url="/admin/dashboard", status_code=303)
    return FileResponse("frontend/login.html", headers=NO_CACHE_HEADERS)


@router.get("/admin")
async def admin_root(request: Request, db: Session = Depends(get_db)):
    redir = _check_admin_access(request, db, next_url="/admin/dashboard")
    if redir:
        return redir
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@router.get("/admin/{page_name}")
async def admin_subpage(page_name: str, request: Request, db: Session = Depends(get_db)):
    slug = page_name.strip().lower().removesuffix(".html")
    if slug not in ADMIN_PAGES:
        raise HTTPException(status_code=404, detail="Admin page not found")
    redir = _check_admin_access(request, db, next_url=f"/admin/{slug}")
    if redir:
        return redir
    target_path = Path("frontend/admin") / f"{slug}.html"
    if not target_path.is_file():
        raise HTTPException(status_code=404, detail="Admin page not found")
    return FileResponse(str(target_path), headers=NO_CACHE_HEADERS)


@router.get("/pay")
async def pay_page(request: Request, session: str = "", db: Session = Depends(get_db)):
    gsid, is_new = get_or_create_guest_session_id(request)
    if session:
        bill = db.query(SessionBill).filter(SessionBill.id == session).first()
        if bill and bill.client_token:
            current_admin = get_current_admin(request, db)
            current_cust = get_current_customer(request, db)
            owns_by_user = bool(current_cust and bill.customer_user_id and current_cust.id == bill.customer_user_id)
            owns_by_guest = bool(gsid and bill.guest_session_id and bill.guest_session_id == gsid)
            if not current_admin and not owns_by_user and not owns_by_guest:
                c_token = _get_client_token(request)
                if not c_token or c_token != bill.client_token:
                    return Response(
                        content=FORBIDDEN_SESSION_HTML,
                        status_code=403,
                        media_type="text/html",
                    )
    resp = FileResponse("frontend/pay.html", headers=NO_CACHE_HEADERS)
    if is_new:
        set_guest_loyalty_cookie(resp, request, gsid)
    return resp


@router.get("/payment/success")
async def payment_success_page(request: Request, session: str = "", db: Session = Depends(get_db)):
    gsid, is_new = get_or_create_guest_session_id(request)
    bill = None
    if session:
        bill = db.query(SessionBill).filter(SessionBill.id == session).first()
        if bill and bill.client_token:
            current_admin = get_current_admin(request, db)
            current_cust = get_current_customer(request, db)
            owns_by_user = bool(current_cust and bill.customer_user_id and current_cust.id == bill.customer_user_id)
            owns_by_guest = bool(gsid and bill.guest_session_id and bill.guest_session_id == gsid)
            if not current_admin and not owns_by_user and not owns_by_guest:
                c_token = _get_client_token(request)
                if not c_token or c_token != bill.client_token:
                    return Response(
                        content=FORBIDDEN_SESSION_HTML,
                        status_code=403,
                        media_type="text/html",
                    )
    resp = FileResponse("frontend/success.html", headers=NO_CACHE_HEADERS)
    if is_new:
        set_guest_loyalty_cookie(resp, request, gsid)
    if bill and (bill.status or "").lower() == "paid" and (bill.points_status or "").lower() == "unclaimed" and bill.customer_user_id is None:
        pts_net = max(0, int(bill.points_earned or 0) - int(bill.points_clawed_back or 0))
        if pts_net > 0:
            set_unclaimed_points_cookie(resp, request, bill.guest_session_id or gsid, bill.id, pts_net)
    return resp
