let pageBills = [];
let statusFilter = "all";
let currentPage = 1;
let pageSize = 20;
let totalPages = 1;
let totalItems = 0;
let searchDebounceTimer = null;

function statusBadge(status, voidReason) {
  const s = (status || "open").toLowerCase();
  if (s === "paid") return '<span class="badge badge-paid">Paid</span>';
  if (s === "pending" || s === "open") return '<span class="badge badge-pending">Pending</span>';
  if (s === "void" || s === "cancelled") {
    const cause = voidReason || "Voided";
    return `
      <div style="display:flex; flex-direction:column; align-items:flex-start; gap:0.25rem;">
        <span class="badge badge-cancelled">Void</span>
        <span style="font-size:0.72rem; font-weight:600; color:#b91c1c; line-height:1.25;">${esc(cause)}</span>
      </div>`;
  }
  return '<span class="badge badge-pending">Pending</span>';
}

function renderPhotoCell(b) {
  if (!b.has_image) {
    return `<span style="color:#94a3b8; font-size:0.78rem;">No photo</span>`;
  }
  const imgUrl = `/api/sessions/${encodeURIComponent(b.session_id)}/image?v=${encodeURIComponent(b.retake_count || 0)}`;
  return `
    <button class="photo-thumb-btn" type="button" title="Click to view full photo" aria-label="View plate photo for Bill #${esc(b.bill_number || "")}" onclick="viewPhoto('${imgUrl}')">
      <img src="${imgUrl}" alt="Plate preview" loading="lazy" decoding="async" onerror="hideBrokenThumb(this)">
    </button>`;
}

async function loadBills(options = {}) {
  const q = (document.getElementById("billSearch")?.value || "").trim();
  const params = new URLSearchParams({
    page: String(currentPage),
    page_size: String(pageSize),
    status: statusFilter,
    q,
    include_summary: "false",
    _t: String(Date.now()),
  });
  const res = await fetch("/api/admin/bills?" + params.toString(), { cache: "no-store" });
  if (res.status === 401) {
    window.location.href = "/login?next=/admin/bills";
    return;
  }
  if (!res.ok) return;
  const data = await res.json();
  pageBills = data.bills || [];

  const pag = data.pagination || {};
  currentPage = pag.page || 1;
  pageSize = pag.page_size || pageSize;
  totalPages = pag.total_pages || 1;
  totalItems = pag.total_items || 0;

  renderBillsTable();
  renderPagination();
}

function onBillSearchInput() {
  clearTimeout(searchDebounceTimer);
  searchDebounceTimer = setTimeout(() => {
    currentPage = 1;
    loadBills({ includeSummary: false });
  }, 250);
}

function setStatusFilter(status, btn) {
  statusFilter = status;
  currentPage = 1;
  document.querySelectorAll(".pill-filter").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  loadBills({ includeSummary: false });
}

function goToBillsPage(page) {
  const next = Math.max(1, Math.min(totalPages, Number(page) || 1));
  if (next === currentPage) return;
  currentPage = next;
  loadBills({ includeSummary: false });
}

function changePageSize(newSize) {
  pageSize = Math.max(1, Math.min(100, Number(newSize) || 20));
  currentPage = 1;
  loadBills({ includeSummary: false });
}

function renderPagination() {
  const infoEl = document.getElementById("billsPaginationInfo");
  const ctrlEl = document.getElementById("billsPaginationControls");
  if (!infoEl || !ctrlEl) return;

  if (totalItems === 0) {
    infoEl.textContent = "Showing 0 bills";
    ctrlEl.innerHTML = "";
    return;
  }

  const start = (currentPage - 1) * pageSize + 1;
  const end = Math.min(totalItems, currentPage * pageSize);
  infoEl.textContent = `Showing ${start}–${end} of ${totalItems} bills (Page ${currentPage} of ${totalPages})`;

  const windowStart = Math.max(1, currentPage - 2);
  const windowEnd = Math.min(totalPages, currentPage + 2);
  let pageBtns = "";
  if (windowStart > 1) {
    pageBtns += `<button class="page-btn" onclick="goToBillsPage(1)">1</button>`;
    if (windowStart > 2) pageBtns += `<span style="color:#94a3b8; padding:0 0.2rem;">…</span>`;
  }
  for (let p = windowStart; p <= windowEnd; p++) {
    pageBtns += `<button class="page-btn ${p === currentPage ? "active" : ""}" onclick="goToBillsPage(${p})">${p}</button>`;
  }
  if (windowEnd < totalPages) {
    if (windowEnd < totalPages - 1) pageBtns += `<span style="color:#94a3b8; padding:0 0.2rem;">…</span>`;
    pageBtns += `<button class="page-btn" onclick="goToBillsPage(${totalPages})">${totalPages}</button>`;
  }

  ctrlEl.innerHTML = `
    <select class="page-select" aria-label="Rows per page" onchange="changePageSize(this.value)">
      ${[10, 20, 50, 100].map(sz => `<option value="${sz}" ${sz === pageSize ? "selected" : ""}>${sz} / page</option>`).join("")}
    </select>
    <button class="page-btn" ${currentPage <= 1 ? "disabled" : ""} onclick="goToBillsPage(${currentPage - 1})">← Prev</button>
    ${pageBtns}
    <button class="page-btn" ${currentPage >= totalPages ? "disabled" : ""} onclick="goToBillsPage(${currentPage + 1})">Next →</button>
  `;
}

function renderBillsTable() {
  const tbody = document.getElementById("allBillsBody");
  const mobileList = document.getElementById("billsMobileList");

  if (!pageBills.length) {
    if (tbody) tbody.innerHTML = '<tr><td colspan="8" style="text-align:center; color:#94a3b8; padding:2rem;">No matching bills found.</td></tr>';
    if (mobileList) mobileList.innerHTML = '<div style="text-align:center; color:#94a3b8; padding:2rem;">No matching bills found.</div>';
    return;
  }

  if (tbody) {
    tbody.innerHTML = pageBills.map(b => {
      const refundedAmt = Number(b.refunded_amount || 0);
      const totalAmt = Number(b.total || 0);
      const isFullyRefunded = b.status === "paid" && totalAmt > 0 && refundedAmt >= totalAmt - 0.005;
      const markPaidBtn = b.status !== "paid"
        ? `<button class="btn btn-sm" onclick="setBillStatus('${esc(b.session_id)}', 'paid')">Mark Paid</button>`
        : "";
      const refundBtn = b.status === "paid"
        ? (isFullyRefunded
            ? `<button class="btn btn-outline btn-sm" onclick="openRefundModal('${esc(b.session_id)}')" title="View refund details">Refunded</button>`
            : `<button class="btn btn-outline btn-sm" style="color:#dc2626; border-color:#fecaca;" onclick="openRefundModal('${esc(b.session_id)}')">Refund</button>`)
        : "";
      const refundedSub = refundedAmt > 0
        ? `<div style="font-size:0.72rem; color:#dc2626; font-weight:700; margin-top:0.2rem;">Refunded RM ${refundedAmt.toFixed(2)}</div>`
        : "";
      const contactsHtml = (b.receipt_phone || b.receipt_email)
        ? `${b.receipt_phone ? `<div style="font-weight:700; color:#0f172a; font-size:0.82rem;">${esc(b.receipt_phone)}</div>` : ""}${b.receipt_email ? `<div style="color:#64748b; font-size:0.78rem;">${esc(b.receipt_email)}</div>` : ""}`
        : "-";
      return `
        <tr>
          <td><span class="bill-code">#${esc(b.bill_number)}</span></td>
          <td>${esc(formatDateTime(b.created_at))}</td>
          <td>${formatItemsSummary(b.items)}</td>
          <td>${contactsHtml}</td>
          <td style="font-weight:700;">RM ${totalAmt.toFixed(2)}</td>
          <td>${statusBadge(b.status, b.void_reason)}${refundedSub}</td>
          <td>${renderPhotoCell(b)}</td>
          <td>
            <div style="display:flex; gap:0.4rem; flex-wrap:wrap;">
              <button class="btn btn-outline btn-sm" onclick="downloadBillPdf('${esc(b.session_id)}')">PDF</button>
              ${refundBtn}
              ${markPaidBtn}
            </div>
          </td>
        </tr>
      `;
    }).join("");
  }

  if (mobileList) {
    mobileList.innerHTML = pageBills.map(b => {
      const refundedAmt = Number(b.refunded_amount || 0);
      const totalAmt = Number(b.total || 0);
      const isFullyRefunded = b.status === "paid" && totalAmt > 0 && refundedAmt >= totalAmt - 0.005;
      const markPaidBtn = b.status !== "paid"
        ? `<button class="btn btn-sm bmc-btn" onclick="setBillStatus('${esc(b.session_id)}', 'paid')">Mark Paid</button>`
        : "";
      const refundBtn = b.status === "paid"
        ? (isFullyRefunded
            ? `<button class="btn btn-outline btn-sm bmc-btn" onclick="openRefundModal('${esc(b.session_id)}')" title="View refund details">Refunded</button>`
            : `<button class="btn btn-outline btn-sm bmc-btn" style="color:#dc2626; border-color:#fecaca;" onclick="openRefundModal('${esc(b.session_id)}')">Refund</button>`)
        : "";
      const refundedSub = refundedAmt > 0
        ? `<div style="font-size:0.75rem; color:#dc2626; font-weight:700; margin-top:0.25rem;">Refunded: RM ${refundedAmt.toFixed(2)}</div>`
        : "";
      const contactsHtml = (b.receipt_phone || b.receipt_email)
        ? `<div class="bmc-section"><span class="bmc-section-label">Customer Contact</span><div style="font-size:0.82rem; color:#1e293b;">${b.receipt_phone ? `<strong>${esc(b.receipt_phone)}</strong>` : ""}${b.receipt_phone && b.receipt_email ? " • " : ""}${b.receipt_email ? `<span>${esc(b.receipt_email)}</span>` : ""}</div></div>`
        : "";
      const imgUrl = `/api/sessions/${encodeURIComponent(b.session_id)}/image?v=${encodeURIComponent(b.retake_count || 0)}`;
      const viewImageBtn = b.has_image
        ? `<button type="button" class="btn btn-outline btn-sm bmc-btn-full" onclick="viewPhoto('${imgUrl}')">
            <svg style="width:14px; height:14px;" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
              <path d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" stroke-linecap="round" stroke-linejoin="round"></path>
            </svg>
            <span>View Image</span>
          </button>`
        : `<button type="button" class="btn btn-outline btn-sm bmc-btn-full" disabled style="opacity:0.45; cursor:not-allowed; color:#94a3b8; border-color:#e2e8f0;">
            <svg style="width:14px; height:14px;" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
              <path d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" stroke-linecap="round" stroke-linejoin="round"></path>
            </svg>
            <span>No Image Available</span>
          </button>`;
      return `
        <div class="bill-mobile-card">
          <div class="bmc-header">
            <div>
              <div class="bill-code" style="font-size:0.95rem;">#${esc(b.bill_number)}</div>
              <div class="bmc-date">${esc(formatDateTime(b.created_at))}</div>
            </div>
            <div>${statusBadge(b.status, b.void_reason)}</div>
          </div>
          <div class="bmc-body">
            <div class="bmc-amount-box">
              <span class="bmc-amount-label">Total Amount</span>
              <span class="bmc-amount-val">RM ${totalAmt.toFixed(2)}</span>
            </div>
            ${refundedSub}
            <div class="bmc-section">
              <span class="bmc-section-label">Ordered Items</span>
              <div class="bmc-items-val">${formatItemsSummary(b.items)}</div>
            </div>
            ${contactsHtml}
          </div>
          <div class="bmc-actions">
            <div class="bmc-actions-row">
              <button class="btn btn-outline btn-sm bmc-btn" onclick="downloadBillPdf('${esc(b.session_id)}')">
                <svg style="width:13px; height:13px;" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" stroke-linecap="round" stroke-linejoin="round"></path></svg>
                <span>PDF Receipt</span>
              </button>
              ${refundBtn}
              ${markPaidBtn}
            </div>
            ${viewImageBtn}
          </div>
        </div>
      `;
    }).join("");
  }
}

let activeRefundInfo = null;

function closeRefundModal() {
  const modal = document.getElementById("refundModal");
  if (modal) modal.classList.remove("open");
  activeRefundInfo = null;
}

async function openRefundModal(sessionId) {
  const modal = document.getElementById("refundModal");
  const body = document.getElementById("refundModalBody");
  const submitBtn = document.getElementById("refundSubmitBtn");
  const badge = document.getElementById("refundBillNoBadge");
  badge.textContent = "#...";
  body.innerHTML = '<div class="empty-state-sm">Loading bill details...</div>';
  submitBtn.disabled = true;
  submitBtn.textContent = "Process Refund";
  modal.classList.add("open");

  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    const res = await fetch(`/api/admin/bills/${encodeURIComponent(sessionId)}/refund-info`, {
      signal: controller.signal,
    });
    clearTimeout(timer);
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "Failed to load bill details");
    }
    activeRefundInfo = data;
    renderRefundModalContent(data);
  } catch (err) {
    const msg = err && err.name === "AbortError"
      ? "Timed out loading bill details. Please try again."
      : (err.message || "Unable to load refund form.");
    body.innerHTML = `<div class="refund-err-box" style="display:block;">${esc(msg)}</div>`;
  }
}

function renderRefundModalContent(info) {
  const badge = document.getElementById("refundBillNoBadge");
  const body = document.getElementById("refundModalBody");
  badge.textContent = `#${info.bill_number || "---"}`;

  const photoHtml = info.has_image && info.image_url
    ? `<img src="${info.image_url}" alt="Plate photo for Bill #${esc(info.bill_number)}" class="refund-photo-preview" onclick="viewPhoto('${info.image_url}')" title="Click to enlarge"><span style="font-size:0.72rem;color:#64748b;">Click photo to enlarge</span>`
    : `<div style="color:#94a3b8;font-size:0.82rem;text-align:center;">No photo tied to this bill</div>`;

  const items = info.items || [];
  const hasRefundableItems = items.some(it => Number(it.remaining_qty || 0) > 0);
  const itemsHtml = items.length ? items.map((it, idx) => {
    const rem = Number(it.remaining_qty || 0);
    const qty = Number(it.quantity || 0);
    const refundedQty = Number(it.refunded_qty || 0);
    const disabled = rem <= 0 || Number(info.remaining_refundable || 0) <= 0;
    const isWhole = Math.abs(rem - Math.round(rem)) < 1e-6;
    const step = isWhole ? "1" : "0.1";
    return `
      <div class="refund-item-row ${disabled ? "disabled" : ""}" data-idx="${idx}">
        <input type="checkbox" class="refund-item-chk" data-idx="${idx}" ${disabled ? "disabled" : ""} onchange="onRefundItemToggle(${idx})">
        <div>
          <div style="font-weight:700;color:#0f172a;">${esc(it.name)}</div>
          <div style="font-size:0.76rem;color:#64748b;">
            RM ${Number(it.unit_price || 0).toFixed(2)} each · Bill qty: ${esc(qty)}${refundedQty > 0 ? ` · ${esc(refundedQty)} already refunded` : ""}
          </div>
        </div>
        <div class="refund-qty-ctrl">
          <button type="button" class="refund-qty-btn" ${disabled ? "disabled" : ""} onclick="stepRefundQty(${idx}, -1)">-</button>
          <input type="number" class="refund-qty-input" id="refundQty_${idx}" min="0" max="${rem}" step="${step}" value="0" ${disabled ? "disabled" : ""} oninput="onRefundQtyInput(${idx})">
          <button type="button" class="refund-qty-btn" ${disabled ? "disabled" : ""} onclick="stepRefundQty(${idx}, 1)">+</button>
        </div>
        <div style="font-weight:700;color:#0f172a;min-width:74px;text-align:right;" id="refundLineSub_${idx}">RM 0.00</div>
      </div>
    `;
  }).join("") : '<div class="empty-state-sm">No items found on this bill.</div>';

  const history = info.refunds || [];
  const historyHtml = history.length ? `
    <div>
      <div style="font-size:0.82rem;font-weight:700;color:#334155;margin-bottom:0.4rem;">Previous Refunds</div>
      <div class="refund-info-box" style="gap:0.5rem;">
        ${history.map(r => `
          <div style="display:flex;justify-content:space-between;gap:0.75rem;flex-wrap:wrap;border-bottom:1px solid #e2e8f0;padding-bottom:0.4rem;">
            <div>
              <span style="font-weight:700;color:#0f172a;">RM ${Number(r.refund_amount || 0).toFixed(2)}</span>
              <span style="color:#64748b;font-size:0.76rem;"> · ${(r.items || []).map(x => `${esc(x.quantity)}x ${esc(x.name)}`).join(", ")}</span>
              ${r.reason ? `<div style="font-size:0.75rem;color:#64748b;">Reason: ${esc(r.reason)}</div>` : ""}
            </div>
            <div style="text-align:right;font-size:0.75rem;color:#64748b;">
              <div>${esc(r.curlec_refund_id || r.status || "recorded")}</div>
              <div>${esc(formatDateTime(r.created_at))}</div>
            </div>
          </div>
        `).join("")}
      </div>
    </div>
  ` : "";

  const paymentNote = info.curlec_payment_id
    ? `${esc(info.curlec_payment_id)} (Curlec)`
    : "No Curlec Payment ID (will record as manual/offline)";

  body.innerHTML = `
    <div id="refundErrorBox" class="refund-err-box"></div>
    <div class="refund-top-grid">
      <div class="refund-info-box">
        <div class="refund-info-row"><span class="refund-info-lbl">Bill #</span><span class="refund-info-val">#${esc(info.bill_number || "-")}</span></div>
        <div class="refund-info-row"><span class="refund-info-lbl">Customer Email</span><span class="refund-info-val">${esc(info.receipt_email || "-")}</span></div>
        <div class="refund-info-row"><span class="refund-info-lbl">Phone Number</span><span class="refund-info-val">${esc(info.receipt_phone || "-")}</span></div>
        <div class="refund-info-row"><span class="refund-info-lbl">Payment Ref</span><span class="refund-info-val" style="font-size:0.78rem;">${paymentNote}</span></div>
        <div class="refund-info-row"><span class="refund-info-lbl">Bill Total</span><span class="refund-info-val">RM ${Number(info.total || 0).toFixed(2)}</span></div>
      </div>
      <div class="refund-photo-box">${photoHtml}</div>
    </div>

    <div>
      <div class="refund-items-head">
        <span style="font-size:0.86rem;font-weight:800;color:#0f172a;">Select Items to Refund</span>
        ${hasRefundableItems ? `
          <div style="display:flex;gap:0.4rem;">
            <button type="button" class="btn btn-outline btn-sm" onclick="toggleAllRefundItems(true)">Select All</button>
            <button type="button" class="btn btn-outline btn-sm" onclick="toggleAllRefundItems(false)">Clear</button>
          </div>
        ` : ""}
      </div>
      <div class="refund-items-list">${itemsHtml}</div>
    </div>

    <div>
      <label for="refundReasonInput" style="display:block;font-size:0.8rem;font-weight:700;color:#334155;margin-bottom:0.35rem;">Refund Reason (Optional)</label>
      <input type="text" id="refundReasonInput" class="refund-reason-input" placeholder="e.g. Wrong item, customer request, overcharged" ${!hasRefundableItems ? "disabled" : ""}>
    </div>

    <div class="refund-summary-bar">
      <div>
        <div style="font-size:0.78rem;color:#9a3412;font-weight:700;">Calculated Refund Amount</div>
        <div style="font-size:0.75rem;color:#7c2d12;">Remaining refundable: RM ${Number(info.remaining_refundable || 0).toFixed(2)}</div>
      </div>
      <div style="font-size:1.3rem;font-weight:800;color:#c2410c;" id="refundCalcTotal">RM 0.00</div>
    </div>

    ${historyHtml}
  `;

  recalcRefundTotal();
}

function onRefundItemToggle(idx) {
  if (!activeRefundInfo) return;
  const chk = document.querySelector(`.refund-item-chk[data-idx="${idx}"]`);
  const inp = document.getElementById(`refundQty_${idx}`);
  const it = (activeRefundInfo.items || [])[idx];
  if (!chk || !inp || !it) return;
  const maxQty = Number(it.remaining_qty || 0);
  if (chk.checked && Number(inp.value || 0) <= 0) {
    inp.value = String(Math.min(1, maxQty));
  } else if (!chk.checked) {
    inp.value = "0";
  }
  recalcRefundTotal();
}

function stepRefundQty(idx, delta) {
  if (!activeRefundInfo) return;
  const chk = document.querySelector(`.refund-item-chk[data-idx="${idx}"]`);
  const inp = document.getElementById(`refundQty_${idx}`);
  const it = (activeRefundInfo.items || [])[idx];
  if (!chk || !inp || !it) return;
  const maxQty = Number(it.remaining_qty || 0);
  const nextVal = Math.max(0, Math.min(maxQty, Number(inp.value || 0) + delta));
  inp.value = String(Math.round(nextVal * 100) / 100);
  chk.checked = nextVal > 0;
  recalcRefundTotal();
}

function onRefundQtyInput(idx) {
  if (!activeRefundInfo) return;
  const chk = document.querySelector(`.refund-item-chk[data-idx="${idx}"]`);
  const inp = document.getElementById(`refundQty_${idx}`);
  const it = (activeRefundInfo.items || [])[idx];
  if (!chk || !inp || !it) return;
  const maxQty = Number(it.remaining_qty || 0);
  let val = Number(inp.value || 0);
  if (isNaN(val) || val < 0) val = 0;
  if (val > maxQty) {
    val = maxQty;
    inp.value = String(maxQty);
  }
  chk.checked = val > 0;
  recalcRefundTotal();
}

function toggleAllRefundItems(selectAll) {
  if (!activeRefundInfo) return;
  (activeRefundInfo.items || []).forEach((it, idx) => {
    const chk = document.querySelector(`.refund-item-chk[data-idx="${idx}"]`);
    const inp = document.getElementById(`refundQty_${idx}`);
    if (!chk || !inp || chk.disabled) return;
    chk.checked = !!selectAll;
    inp.value = selectAll ? String(Number(it.remaining_qty || 0)) : "0";
  });
  recalcRefundTotal();
}

function getSelectedRefundPayload() {
  if (!activeRefundInfo) return { items: [], total: 0 };
  const selected = [];
  let total = 0;
  (activeRefundInfo.items || []).forEach((it, idx) => {
    const chk = document.querySelector(`.refund-item-chk[data-idx="${idx}"]`);
    const inp = document.getElementById(`refundQty_${idx}`);
    const subEl = document.getElementById(`refundLineSub_${idx}`);
    const maxQty = Number(it.remaining_qty || 0);
    let qty = chk && chk.checked ? Number(inp ? inp.value : 0) : 0;
    if (isNaN(qty) || qty < 0) qty = 0;
    if (qty > maxQty) qty = maxQty;
    const lineSub = Math.round(Number(it.unit_price || 0) * qty * 100) / 100;
    if (subEl) subEl.textContent = `RM ${lineSub.toFixed(2)}`;
    if (qty > 0) {
      selected.push({ name: it.name, quantity: qty });
      total += lineSub;
    }
  });
  const maxRemaining = Number(activeRefundInfo.remaining_refundable || 0);
  total = Math.min(Math.round(total * 100) / 100, maxRemaining);
  return { items: selected, total };
}

function recalcRefundTotal() {
  const { items, total } = getSelectedRefundPayload();
  const totalEl = document.getElementById("refundCalcTotal");
  const submitBtn = document.getElementById("refundSubmitBtn");
  const isFullyRefunded = !activeRefundInfo || Number(activeRefundInfo.remaining_refundable || 0) <= 0;
  if (totalEl) totalEl.textContent = `RM ${total.toFixed(2)}`;
  if (submitBtn) {
    submitBtn.disabled = isFullyRefunded || !items.length || total <= 0;
    submitBtn.textContent = isFullyRefunded
      ? "Fully Refunded"
      : (total > 0 ? `Process Refund (RM ${total.toFixed(2)})` : "Process Refund");
  }
}

let isRefundSubmitting = false;

async function submitRefund() {
  if (!activeRefundInfo || isRefundSubmitting) return;
  const errBox = document.getElementById("refundErrorBox");
  const submitBtn = document.getElementById("refundSubmitBtn");
  const reasonEl = document.getElementById("refundReasonInput");
  const { items, total } = getSelectedRefundPayload();
  if (!items.length || total <= 0) return;

  isRefundSubmitting = true;
  if (errBox) {
    errBox.style.display = "none";
    errBox.textContent = "";
  }
  submitBtn.disabled = true;
  submitBtn.innerHTML = '<span style="display:inline-block;width:12px;height:12px;border:2px solid #fff;border-top-color:transparent;border-radius:50%;animation:spin 0.6s linear infinite;margin-right:6px;vertical-align:middle;"></span> Processing...';

  try {
    const res = await fetch(`/api/admin/bills/${encodeURIComponent(activeRefundInfo.session_id)}/refund`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        items,
        reason: reasonEl ? reasonEl.value.trim() : "",
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "Refund failed");
    }
    showToast(`Refunded RM ${Number(data.refund?.refund_amount || total).toFixed(2)} for #${activeRefundInfo.bill_number}`);
    closeRefundModal();
    await loadBills({ includeSummary: true });
  } catch (err) {
    if (errBox) {
      errBox.textContent = err.message || "Refund failed";
      errBox.style.display = "block";
    }
    recalcRefundTotal();
  } finally {
    isRefundSubmitting = false;
  }
}


async function setBillStatus(sessionId, status) {
  const res = await fetch(`/api/admin/bills/${encodeURIComponent(sessionId)}/status`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ status })
  });
  if (res.ok) {
    showToast(`Bill updated to ${status.toUpperCase()}`);
    await loadBills({ includeSummary: true });
  }
}
