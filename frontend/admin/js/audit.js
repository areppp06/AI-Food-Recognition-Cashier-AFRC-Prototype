let auditCurrentPage = 1;
let auditPageSize = 20;
let auditTotalPages = 1;
let auditTotalItems = 0;

async function loadAuditLogs(page = auditCurrentPage, pageSize = auditPageSize) {
  auditCurrentPage = page;
  auditPageSize = pageSize;
  try {
    const res = await fetch(`/api/admin/audit-logs?page=${auditCurrentPage}&page_size=${auditPageSize}`);
    if (!res.ok) return;
    const data = await res.json();
    const logs = data.logs || (Array.isArray(data) ? data : []);
    const pag = data.pagination || {};
    auditTotalItems = pag.total_items ?? logs.length;
    auditTotalPages = pag.total_pages || 1;

    const tbody = document.getElementById("auditBody");
    const mobileList = document.getElementById("auditMobileList");

    if (!logs.length) {
      if (tbody) tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; color:#94a3b8; padding:2rem;">No audit logs found.</td></tr>';
      if (mobileList) mobileList.innerHTML = '<div style="text-align:center; color:#94a3b8; padding:2rem;">No audit logs found.</div>';
      renderAuditPagination();
      return;
    }
    if (tbody) {
      tbody.innerHTML = logs.map(l => `
        <tr>
          <td>#${esc(l.id)}</td>
          <td>${esc(formatDateTime(l.timestamp))}</td>
          <td style="font-weight:600;">${esc(l.cashier_name)}</td>
          <td><span class="badge badge-pending">${esc(l.action)}</span></td>
          <td style="font-family:monospace; font-size:0.82rem;">${esc(l.details)}</td>
        </tr>
      `).join("");
    }
    if (mobileList) {
      mobileList.innerHTML = logs.map(l => `
        <div class="audit-mobile-card">
          <div class="amc-head">
            <span class="badge badge-pending">${esc(l.action)}</span>
            <span class="amc-time">${esc(formatDateTime(l.timestamp))}</span>
          </div>
          <div class="amc-meta">
            <span class="amc-user">User: <strong>${esc(l.cashier_name)}</strong></span>
            <span class="amc-id">#${esc(l.id)}</span>
          </div>
          <div class="amc-details">${esc(l.details)}</div>
        </div>
      `).join("");
    }
    renderAuditPagination();
  } catch (err) {
    console.error("Failed to load audit logs:", err);
  }
}

function renderAuditPagination() {
  const infoEl = document.getElementById("auditPaginationInfo");
  const ctrlEl = document.getElementById("auditPaginationControls");
  if (!infoEl || !ctrlEl) return;

  if (auditTotalItems === 0) {
    infoEl.textContent = "Showing 0 logs";
    ctrlEl.innerHTML = "";
    return;
  }

  const start = (auditCurrentPage - 1) * auditPageSize + 1;
  const end = Math.min(auditTotalItems, auditCurrentPage * auditPageSize);
  infoEl.textContent = `Showing ${start}–${end} of ${auditTotalItems} logs (Page ${auditCurrentPage} of ${auditTotalPages})`;

  const windowStart = Math.max(1, auditCurrentPage - 2);
  const windowEnd = Math.min(auditTotalPages, auditCurrentPage + 2);
  let pageBtns = "";
  if (windowStart > 1) {
    pageBtns += `<button class="page-btn" onclick="goToAuditPage(1)">1</button>`;
    if (windowStart > 2) pageBtns += `<span style="color:#94a3b8; padding:0 0.2rem;">…</span>`;
  }
  for (let p = windowStart; p <= windowEnd; p++) {
    pageBtns += `<button class="page-btn ${p === auditCurrentPage ? "active" : ""}" onclick="goToAuditPage(${p})">${p}</button>`;
  }
  if (windowEnd < auditTotalPages) {
    if (windowEnd < auditTotalPages - 1) pageBtns += `<span style="color:#94a3b8; padding:0 0.2rem;">…</span>`;
    pageBtns += `<button class="page-btn" onclick="goToAuditPage(${auditTotalPages})">${auditTotalPages}</button>`;
  }

  ctrlEl.innerHTML = `
    <select class="page-select" aria-label="Logs per page" onchange="changeAuditPageSize(this.value)">
      ${[10, 20, 50, 100].map(sz => `<option value="${sz}" ${sz === auditPageSize ? "selected" : ""}>${sz} / page</option>`).join("")}
    </select>
    <button class="page-btn" ${auditCurrentPage <= 1 ? "disabled" : ""} onclick="goToAuditPage(${auditCurrentPage - 1})">← Prev</button>
    ${pageBtns}
    <button class="page-btn" ${auditCurrentPage >= auditTotalPages ? "disabled" : ""} onclick="goToAuditPage(${auditCurrentPage + 1})">Next →</button>
  `;
}

function goToAuditPage(page) {
  if (page < 1 || page > auditTotalPages) return;
  loadAuditLogs(page, auditPageSize);
}

function changeAuditPageSize(newSize) {
  auditPageSize = parseInt(newSize, 10) || 20;
  auditCurrentPage = 1;
  loadAuditLogs(1, auditPageSize);
}

