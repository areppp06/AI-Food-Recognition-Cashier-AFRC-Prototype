function renderCustomerAvatarHtml(u) {
  const nameStr = String((u && u.name) || "Customer").trim();
  const initial = (nameStr.charAt(0) || "C").toUpperCase();
  if (u && u.avatar_url) {
    return `<span class="cust-avatar"><img src="${esc(u.avatar_url)}" alt="${esc(nameStr)}" onerror="this.remove();this.parentNode&&(this.parentNode.textContent='${esc(initial)}')"></span>`;
  }
  return `<span class="cust-avatar">${esc(initial)}</span>`;
}

function fmtShortDate(iso) {
  if (!iso) return "N/A";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "N/A";
  return d.toLocaleDateString("en-MY", { year: "numeric", month: "short", day: "2-digit" });
}

async function loadAdminLoyaltyOverview() {
  try {
    const res = await fetch("/api/admin/loyalty/overview", { cache: "no-store" });
    if (!res.ok) return;
    const d = await res.json();

    const setEl = (id, text) => {
      const el = document.getElementById(id);
      if (el) el.textContent = text;
    };

    // Customers stats
    setEl("custStatTotal", Number(d.total_customers || 0).toLocaleString());
    setEl("custStatActive7d", Number(d.active_customers_7d || 0).toLocaleString());
    setEl("custStatSuspended", Number(d.suspended_customers || 0).toLocaleString());
    setEl("custStatNewWeek", Number(d.new_this_week || 0).toLocaleString());

    // Points stats
    setEl("loyKpiPoints", Number(d.circulating_points || 0).toLocaleString() + " pts");
    setEl("loyKpiPointsVal", "RM " + Number(d.est_value_rm || 0).toFixed(2));

    // Voucher stats
    const vs = d.voucher_stats || {};
    setEl("loyKpiVoucherToday", "RM " + Number(vs.today || 0).toFixed(2));
    setEl("loyKpiVoucherWeek", "RM " + Number(vs.this_week || 0).toFixed(2));
    setEl("loyKpiVoucherMonth", "RM " + Number(vs.this_month || 0).toFixed(2));

    const cdInp = document.getElementById("loyCooldownInput");
    if (cdInp && document.activeElement !== cdInp && d.voucher_cooldown_hours !== undefined) {
      cdInp.value = d.voucher_cooldown_hours;
    }
  } catch (e) {}
}
