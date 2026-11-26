let loyVoucherSource = "manual";
let loyVoucherPage = 1;
let loyPendingDeleteVoucher = null;

function initVoucherFormDefaults() {
  const expEl = document.getElementById("vNewExpiry");
  if (expEl && !expEl.value) {
    const d = new Date(Date.now() + 30 * 86400000);
    expEl.value = d.toISOString().slice(0, 10);
  }
  onVoucherTypeSelectChange();
  onVoucherEmailInputChange();
}

function setAdminVoucherSource(src, btn) {
  loyVoucherSource = src;
  loyVoucherPage = 1;
  document.querySelectorAll("#adminVoucherSourceTabs .voucher-folder-tab").forEach(b => {
    b.classList.toggle("active", b.dataset.vsrc === src);
  });
  loadAdminVouchers(1);
}

function onVoucherTypeSelectChange() {
  const t = document.getElementById("vNewType")?.value || "fixed";
  const wrap = document.getElementById("vNewMaxDiscWrap");
  if (wrap) wrap.style.display = t === "percentage" ? "flex" : "none";
}

function onVoucherEmailInputChange() {
  const em = (document.getElementById("vNewEmail")?.value || "").trim();
  const wrap = document.getElementById("vNewMaxUsesWrap");
  if (wrap) wrap.style.display = em ? "none" : "flex";
}

function generateRandomVoucherCode() {
  const chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  let out = "AFRC-";
  for (let i = 0; i < 6; i++) out += chars.charAt(Math.floor(Math.random() * chars.length));
  const inp = document.getElementById("vNewCode");
  if (inp) inp.value = out;
}

async function loadAdminVouchers(page = 1) {
  loyVoucherPage = page;
  const tbody = document.getElementById("loyVouchersBody");
  const mList = document.getElementById("loyVouchersMobileList");
  if (!tbody) return;
  try {
    const viewingDeleted = loyVoucherSource === "deleted";
    const query = viewingDeleted
      ? `archived=1&page=${page}&limit=15`
      : `source=${encodeURIComponent(loyVoucherSource)}&page=${page}&limit=15`;
    const res = await fetch(`/api/admin/loyalty/vouchers?${query}`, { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json();
    const list = data.vouchers || [];
    const deletedMeta = (v) => {
      if (!v.is_archived && !viewingDeleted) return "";
      const when = v.archived_at ? new Date(v.archived_at).toLocaleString("en-MY", { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "time not recorded";
      const who = v.archived_by ? esc(v.archived_by) : "unknown admin";
      const kind = v.source === "points_redemption" ? "Auto generated" : "Manual";
      return `<div style="font-size:0.74rem; color:#b91c1c; margin-top:0.15rem;">Revoked by ${who} · ${esc(when)} · ${kind}</div>`;
    };
    if (!list.length) {
      const lbl = viewingDeleted ? "revoked" : (loyVoucherSource === "manual" ? "manual" : "auto-generated");
      tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; padding:2.5rem 1rem; color:#94a3b8;">No ${lbl} vouchers found.</td></tr>`;
      if (mList) mList.innerHTML = `<div class="empty-state-sm">No ${lbl} vouchers found.</div>`;
    } else {
      tbody.innerHTML = list.map(v => {
        const valStr = v.type === "percentage" ? `${v.discount_value}%` : `RM ${Number(v.discount_value).toFixed(2)}`;
        const maxDiscSub = v.max_discount ? `<div style="font-size:0.74rem; color:#64748b; font-weight:600;">Max: RM ${Number(v.max_discount).toFixed(2)}</div>` : "";
        const isExhausted = !!v.is_redeemed;
        const stBadge = viewingDeleted
          ? '<span class="badge badge-cancelled">Revoked</span>'
          : (isExhausted
          ? '<span class="badge badge-pending">Exhausted</span>'
          : (v.is_expired ? '<span class="badge badge-cancelled">Expired</span>' : (v.is_active ? '<span class="badge badge-paid">Active</span>' : '<span class="badge badge-cancelled">Paused</span>')));
        const expStr = fmtShortDate(v.expires_at);
        const ownerSub = v.user_email
          ? `<div style="font-size:0.74rem; color:#64748b; margin-top:0.15rem;">For: ${esc(v.user_email)}</div>`
          : `<div style="font-size:0.74rem; color:#94a3b8; margin-top:0.15rem;">Public (any signed-in user)</div>`;
        const switchHtml = (!viewingDeleted && loyVoucherSource === "manual" && !isExhausted && !v.is_expired)
          ? `<label class="switch-wrap" title="${v.is_active ? 'Click to pause' : 'Click to activate'}" onclick="event.preventDefault(); toggleAdminVoucher(${v.id})">
               <span class="switch-toggle">
                 <input type="checkbox" class="switch-input" ${v.is_active ? "checked" : ""} style="pointer-events:none;">
                 <span class="switch-slider"></span>
               </span>
             </label>`
          : "";
        return `
          <tr style="${isExhausted || viewingDeleted ? 'opacity:0.72;' : ''}">
            <td>
              <span class="bill-code">${esc(v.code)}</span>
              ${ownerSub}
              ${deletedMeta(v)}
            </td>
            <td>
              <div style="font-weight:800; color:#059669;">${esc(valStr)}</div>
              ${maxDiscSub}
            </td>
            <td>
              <div style="font-size:0.83rem; color:#1e293b;">Min: RM ${Number(v.min_spend || 0).toFixed(2)}</div>
              <div style="font-size:0.76rem; font-weight:700; color:#ea580c; margin-top:0.1rem;">Uses: ${v.times_used} / ${v.max_uses}</div>
            </td>
            <td style="font-size:0.83rem; color:#334155;">${esc(expStr)}</td>
            <td>${stBadge}</td>
            <td style="text-align:center;">
              ${viewingDeleted ? '<span style="color:#94a3b8; font-size:0.78rem;">Kept</span>' : `<div style="display:inline-flex; align-items:center; gap:0.55rem;">
                ${switchHtml}
                <button type="button" class="btn btn-danger btn-sm" title="Revoke voucher" onclick="openDeleteVoucherModal(${v.id}, '${esc(v.code)}')">Revoke</button>
              </div>`}
            </td>
          </tr>
        `;
      }).join("");

      if (mList) {
        mList.innerHTML = list.map(v => {
          const valStr = v.type === "percentage" ? `${v.discount_value}%` : `RM ${Number(v.discount_value).toFixed(2)}`;
          const isExhausted = !!v.is_redeemed;
          const stBadge = viewingDeleted
            ? '<span class="badge badge-cancelled">Revoked</span>'
            : (isExhausted
            ? '<span class="badge badge-pending">Exhausted</span>'
            : (v.is_expired ? '<span class="badge badge-cancelled">Expired</span>' : (v.is_active ? '<span class="badge badge-paid">Active</span>' : '<span class="badge badge-cancelled">Paused</span>')));
          return `
            <div class="bill-mobile-card">
              <div class="bmc-header">
                <div>
                  <span class="bill-code">${esc(v.code)}</span>
                  <div style="font-size:0.75rem; color:#64748b; margin-top:0.2rem;">${v.user_email ? esc(v.user_email) : "Public voucher"}</div>
                  ${deletedMeta(v)}
                </div>
                ${stBadge}
              </div>
              <div style="display:grid; grid-template-columns:1fr 1fr; gap:0.4rem; font-size:0.8rem;">
                <div style="color:#64748b;">Discount</div>
                <div style="text-align:right; font-weight:800; color:#059669;">${esc(valStr)}</div>
                <div style="color:#64748b;">Min Spend</div>
                <div style="text-align:right; font-weight:600;">RM ${Number(v.min_spend || 0).toFixed(2)}</div>
                <div style="color:#64748b;">Uses</div>
                <div style="text-align:right; font-weight:700; color:#ea580c;">${v.times_used} / ${v.max_uses}</div>
                <div style="color:#64748b;">Expires</div>
                <div style="text-align:right; font-weight:600;">${esc(fmtShortDate(v.expires_at))}</div>
              </div>
              ${viewingDeleted ? "" : `<div class="bmc-actions">
                <div class="bmc-actions-row">
                  ${(!isExhausted && !v.is_expired && loyVoucherSource === "manual") ? `<button type="button" class="btn btn-outline btn-sm bmc-btn" onclick="toggleAdminVoucher(${v.id})">${v.is_active ? "Pause" : "Activate"}</button>` : ""}
                  <button type="button" class="btn btn-danger btn-sm bmc-btn" onclick="openDeleteVoucherModal(${v.id}, '${esc(v.code)}')">Revoke</button>
                </div>
              </div>`}
            </div>
          `;
        }).join("");
      }
    }

    document.getElementById("loyVouchersPageInfo").textContent = `Page ${data.page} of ${data.total_pages} (${data.total} total)`;
    document.getElementById("loyVouchersPageBtns").innerHTML = `
      <button type="button" class="page-btn" ${data.page <= 1 ? "disabled" : ""} onclick="loadAdminVouchers(${data.page - 1})">Previous</button>
      <button type="button" class="page-btn" ${data.page >= data.total_pages ? "disabled" : ""} onclick="loadAdminVouchers(${data.page + 1})">Next</button>
    `;
  } catch (e) {}
}

async function createAdminVoucher(event) {
  event.preventDefault();
  const vType = document.getElementById("vNewType").value;
  const userEmail = document.getElementById("vNewEmail").value.trim();
  const maxDiscRaw = document.getElementById("vNewMaxDisc")?.value || "";
  const payload = {
    code: document.getElementById("vNewCode").value.trim().toUpperCase(),
    type: vType,
    discount_value: parseFloat(document.getElementById("vNewVal").value),
    max_discount: (vType === "percentage" && maxDiscRaw) ? parseFloat(maxDiscRaw) : null,
    min_spend: parseFloat(document.getElementById("vNewMin").value || "0"),
    max_uses: userEmail ? 1 : parseInt(document.getElementById("vNewMaxUses").value || "1", 10),
    user_email: userEmail,
    expires_at: document.getElementById("vNewExpiry").value,
  };
  const btn = document.getElementById("btnCreateAdminVoucher");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/api/admin/loyalty/vouchers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(d.detail || "Could not create voucher.");
      return;
    }
    document.getElementById("vNewCode").value = "";
    document.getElementById("vNewVal").value = "";
    if (document.getElementById("vNewMaxDisc")) document.getElementById("vNewMaxDisc").value = "";
    document.getElementById("vNewEmail").value = "";
    onVoucherEmailInputChange();
    showToast("Voucher " + d.code + " created");
    setAdminVoucherSource("manual");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function toggleAdminVoucher(id) {
  const res = await fetch(`/api/admin/loyalty/vouchers/${id}/toggle`, { method: "PUT" });
  if (res.ok) {
    showToast("Voucher status updated");
    loadAdminVouchers(loyVoucherPage);
  }
}

function openDeleteVoucherModal(id, code) {
  loyPendingDeleteVoucher = { id, code };
  document.getElementById("loyDelVoucherCodeLabel").textContent = code;
  const inp = document.getElementById("loyDelVoucherConfirmInput");
  if (inp) inp.value = "";
  onDeleteVoucherConfirmInput();
  document.getElementById("loyaltyDeleteVoucherModal").classList.add("open");
}

function closeDeleteVoucherModal() {
  loyPendingDeleteVoucher = null;
  document.getElementById("loyaltyDeleteVoucherModal").classList.remove("open");
}

function onDeleteVoucherConfirmInput() {
  const inp = document.getElementById("loyDelVoucherConfirmInput");
  const btn = document.getElementById("btnConfirmDeleteVoucher");
  if (btn && inp) {
    btn.disabled = inp.value.trim() !== "REVOKE";
  }
}

async function executeDeleteVoucher() {
  if (!loyPendingDeleteVoucher) return;
  const inp = document.getElementById("loyDelVoucherConfirmInput");
  if (!inp || inp.value.trim() !== "REVOKE") return;
  const btn = document.getElementById("btnConfirmDeleteVoucher");
  btn.disabled = true;
  try {
    const res = await fetch(`/api/admin/loyalty/vouchers/${loyPendingDeleteVoucher.id}`, { method: "DELETE" });
    if (res.ok) {
      showToast(`Voucher ${loyPendingDeleteVoucher.code} revoked`);
      closeDeleteVoucherModal();
      loadAdminVouchers(loyVoucherPage);
    } else {
      alert("Failed to revoke voucher.");
    }
  } finally {
    onDeleteVoucherConfirmInput();
  }
}

