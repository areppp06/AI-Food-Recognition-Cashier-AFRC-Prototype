let archiveCurrentPeriod = "7d";
let archiveCurrentPage = 1;
const archivePageSize = 10;
let archiveTotalPages = 1;
let currentArchiveItems = [];

// ==================== ARCHIVES & FAILED SCANS ====================

function setArchivePeriod(period, btn) {
  archiveCurrentPeriod = period;
  if (btn) {
    document.querySelectorAll("#archivePeriodPills .range-pill").forEach(p => p.classList.remove("active"));
    btn.classList.add("active");
  }
  archiveCurrentPage = 1;
  loadArchives(1);
}

function onArchiveFilterChange() {
  archiveCurrentPage = 1;
  loadArchives(1);
}

function formatRejectionCode(code) {
  if (!code || code === "None") return "None";
  const map = {
    UNREADABLE_PHOTO: "Unreadable / Blurry",
    NO_FOOD_DETECTED: "No Food Detected",
    AI_SERVICE_ERROR: "AI Service Error",
    IMAGE_TOO_LARGE: "Image Too Large",
  };
  return map[code] || code.replace(/_/g, " ");
}

function rejectionBadge(code) {
  const c = code || "UNKNOWN";
  let bg = "#f1f5f9";
  let fg = "#475569";
  let border = "#e2e8f0";
  if (c === "UNREADABLE_PHOTO") {
    bg = "#fef3c7"; fg = "#92400e"; border = "#fde68a";
  } else if (c === "NO_FOOD_DETECTED") {
    bg = "#ffedd5"; fg = "#c2410c"; border = "#fed7aa";
  } else if (c === "AI_SERVICE_ERROR") {
    bg = "#ffe4e6"; fg = "#be123c"; border = "#fecdd3";
  }
  return `<span class="badge" style="background:${bg}; color:${fg}; border:1px solid ${border}; font-weight:700; font-size:0.75rem; padding:0.25rem 0.55rem; border-radius:6px;">${escapeHtml(formatRejectionCode(c))}</span>`;
}

async function loadArchives(page = 1) {
  archiveCurrentPage = page;
  const tbody = document.getElementById("archiveTableBody");
  const mobileList = document.getElementById("archiveMobileList");
  if (tbody) {
    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; padding:2.5rem; color:#94a3b8;"><div class="spinner-sm" style="display:inline-block; margin-right:0.5rem; vertical-align:middle;"></div>Loading scan archives...</td></tr>`;
  }

  const code = document.getElementById("archiveCodeFilter")?.value || "all";
  const scanType = document.getElementById("archiveTypeFilter")?.value || "all";
  const search = (document.getElementById("archiveSearchInput")?.value || "").trim();

  const params = new URLSearchParams({
    period: archiveCurrentPeriod,
    code: code,
    scan_type: scanType,
    page: String(archiveCurrentPage),
    page_size: String(archivePageSize),
  });
  if (search) params.append("q", search);

  try {
    const res = await fetch(`/api/admin/failed-scans?${params.toString()}`);
    if (!res.ok) {
      if (tbody) tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; padding:2rem; color:#ef4444;">Failed to load archives. Please try again.</td></tr>`;
      return;
    }
    const data = await res.json();
    currentArchiveItems = data.items || [];
    const pagination = data.pagination || {};
    const summary = data.summary || {};

    // Update KPI cards
    const totalEl = document.getElementById("archiveTotalFailed");
    if (totalEl) totalEl.textContent = summary.total_failed ?? 0;
    const todayEl = document.getElementById("archiveTodayFailed");
    if (todayEl) todayEl.textContent = summary.today_failed ?? 0;
    const retriesEl = document.getElementById("archiveRetriesCount");
    if (retriesEl) retriesEl.textContent = summary.retries_failed ?? 0;
    const topReasonEl = document.getElementById("archiveTopReason");
    if (topReasonEl) {
      topReasonEl.textContent = formatRejectionCode(summary.top_rejection || "None");
    }

    renderArchives(currentArchiveItems);
    renderArchivePagination(pagination);
  } catch (err) {
    console.error("Load archives error:", err);
    if (tbody) tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; padding:2rem; color:#ef4444;">Network error while loading archives.</td></tr>`;
  }
}

function renderArchives(items) {
  const tbody = document.getElementById("archiveTableBody");
  const mobileList = document.getElementById("archiveMobileList");

  if (!items || items.length === 0) {
    if (tbody) {
      tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; padding:3rem 1rem; color:#94a3b8;">
        <div style="font-size:1rem; font-weight:600; color:#64748b; margin-bottom:0.25rem;">No failed scan records found</div>
        <div style="font-size:0.8rem;">No rejected scans match the selected period and filters.</div>
      </td></tr>`;
    }
    if (mobileList) {
      mobileList.innerHTML = `<div style="text-align:center; padding:2.5rem 1rem; color:#94a3b8; font-size:0.85rem;">No failed scan records found</div>`;
    }
    return;
  }

  // Desktop Table
  if (tbody) {
    tbody.innerHTML = items.map(item => {
      const imgHtml = item.has_image
        ? `<img src="${item.image_url}" alt="Failed Scan" onclick="openArchiveDetailModal(${item.id})" style="width:48px; height:48px; object-fit:cover; border-radius:8px; cursor:pointer; border:1px solid #e2e8f0; transition:transform 0.15s ease;" onmouseover="this.style.transform='scale(1.08)'" onmouseout="this.style.transform='scale(1)'" loading="lazy">`
        : `<div style="width:48px; height:48px; border-radius:8px; background:#f1f5f9; display:flex; align-items:center; justify-content:center; color:#94a3b8; font-size:0.68rem; font-weight:600;">No img</div>`;

      const isRetryBadge = item.is_retry
        ? `<span class="badge" style="background:#fef3c7; color:#92400e; font-weight:700; font-size:0.72rem; padding:0.15rem 0.45rem; border-radius:4px;">Retry Scan</span>`
        : `<span class="badge" style="background:#f1f5f9; color:#64748b; font-size:0.72rem; padding:0.15rem 0.45rem; border-radius:4px;">Initial Scan</span>`;

      const billHtml = item.bill_number
        ? `<div style="margin-top:0.3rem;"><span style="font-size:0.75rem; color:#64748b;">Bill:</span> <strong style="color:#0f172a; font-size:0.82rem;">#${escapeHtml(item.bill_number)}</strong></div>`
        : "";

      const rawAiPill = item.raw_ai_response
        ? `<span style="display:inline-block; margin-left:0.35rem; font-size:0.68rem; font-weight:700; color:#4338ca; background:#e0e7ff; padding:0.1rem 0.35rem; border-radius:4px;">AI Raw</span>`
        : "";

      const viewBillBtn = item.bill_number
        ? `<button type="button" class="btn btn-dark btn-sm" onclick="viewBillPdfPreview('${escapeHtml(item.bill_number)}')" title="View Bill #${escapeHtml(item.bill_number)} PDF" style="gap:0.3rem; padding:0.3rem 0.6rem; font-size:0.75rem;">
            <svg style="width:12px; height:12px;" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg>
            <span>View Bill</span>
          </button>`
        : "";

      return `
        <tr>
          <td style="padding:0.6rem 0.75rem; vertical-align:middle;">${imgHtml}</td>
          <td style="padding:0.6rem 0.75rem; vertical-align:middle;">
            <div style="font-weight:600; color:#0f172a; font-size:0.83rem;">${escapeHtml(item.created_at_local || "---")}</div>
            <div style="font-size:0.72rem; color:#94a3b8;">MYT Server Time</div>
          </td>
          <td style="padding:0.6rem 0.75rem; vertical-align:middle;">
            ${rejectionBadge(item.rejection_code)}
          </td>
          <td style="padding:0.6rem 0.75rem; vertical-align:middle; max-width:300px;">
            <div style="color:#334155; font-size:0.83rem; line-height:1.4; word-break:break-word;">
              ${escapeHtml(item.rejection_message || "Rejection without custom message")}
              ${rawAiPill}
            </div>
          </td>
          <td style="padding:0.6rem 0.75rem; vertical-align:middle;">
            <div>${isRetryBadge}</div>
            ${billHtml}
            <div style="font-size:0.72rem; color:#94a3b8; margin-top:0.15rem;">IP: ${escapeHtml(item.ip || "---")}</div>
          </td>
          <td style="padding:0.6rem 0.75rem; text-align:right; vertical-align:middle;">
            <div style="display:flex; justify-content:flex-end; align-items:center; gap:0.4rem; flex-wrap:wrap;">
              <button type="button" class="btn btn-outline btn-sm" onclick="openArchiveDetailModal(${item.id})" style="padding:0.3rem 0.6rem; font-size:0.75rem;">Inspect</button>
              ${viewBillBtn}
            </div>
          </td>
        </tr>
      `;
    }).join("");
  }

  // Mobile Cards
  if (mobileList) {
    mobileList.innerHTML = items.map(item => {
      const imgHtml = item.has_image
        ? `<img src="${item.image_url}" alt="Failed Scan" onclick="openArchiveDetailModal(${item.id})" style="width:64px; height:64px; object-fit:cover; border-radius:10px; cursor:pointer; border:1px solid #e2e8f0;" loading="lazy">`
        : `<div style="width:64px; height:64px; border-radius:10px; background:#f1f5f9; display:flex; align-items:center; justify-content:center; color:#94a3b8; font-size:0.7rem; font-weight:600;">No img</div>`;

      const isRetryBadge = item.is_retry
        ? `<span class="badge" style="background:#fef3c7; color:#92400e; font-weight:700; font-size:0.7rem; padding:0.15rem 0.4rem; border-radius:4px;">Retry</span>`
        : `<span class="badge" style="background:#f1f5f9; color:#64748b; font-size:0.7rem; padding:0.15rem 0.4rem; border-radius:4px;">Initial</span>`;

      const billHtml = item.bill_number
        ? `<div style="font-size:0.8rem; margin-top:0.25rem;"><span style="color:#64748b;">Bill:</span> <strong style="color:#0f172a;">#${escapeHtml(item.bill_number)}</strong></div>`
        : "";

      const viewBillBtn = item.bill_number
        ? `<button type="button" class="btn btn-dark btn-sm bmc-btn" onclick="viewBillPdfPreview('${escapeHtml(item.bill_number)}')">
            <svg style="width:12px; height:12px;" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg>
            <span>View Bill</span>
          </button>`
        : "";

      return `
        <div class="bill-mobile-card" style="display:flex; flex-direction:column; gap:0.75rem; padding:1rem; border:1px solid #e2e8f0; border-radius:12px; background:#fff; margin-bottom:0.75rem;">
          <div style="display:flex; gap:0.85rem; align-items:flex-start;">
            ${imgHtml}
            <div style="flex:1; min-width:0;">
              <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:0.5rem; margin-bottom:0.35rem;">
                ${rejectionBadge(item.rejection_code)}
                ${isRetryBadge}
              </div>
              <div style="font-size:0.82rem; font-weight:600; color:#0f172a;">${escapeHtml(item.created_at_local || "---")}</div>
              ${billHtml}
            </div>
          </div>
          <div style="font-size:0.82rem; color:#475569; background:#f8fafc; padding:0.6rem 0.75rem; border-radius:8px; border:1px solid #f1f5f9; line-height:1.4;">
            ${escapeHtml(item.rejection_message || "Rejection without message")}
          </div>
          <div style="display:flex; justify-content:space-between; align-items:center; gap:0.5rem; padding-top:0.25rem;">
            <div style="font-size:0.72rem; color:#94a3b8;">IP: ${escapeHtml(item.ip || "---")}</div>
            <div style="display:flex; gap:0.4rem;">
              <button type="button" class="btn btn-outline btn-sm bmc-btn" onclick="openArchiveDetailModal(${item.id})">Inspect</button>
              ${viewBillBtn}
            </div>
          </div>
        </div>
      `;
    }).join("");
  }
}

function renderArchivePagination(pagination) {
  const bar = document.getElementById("archivePaginationBar");
  const info = document.getElementById("archivePaginationInfo");
  const controls = document.getElementById("archivePaginationControls");
  if (!bar || !info || !controls) return;

  const total = pagination.total_items || 0;
  const curPage = pagination.page || 1;
  const totalPages = pagination.total_pages || 1;
  archiveTotalPages = totalPages;

  info.textContent = total > 0
    ? `Showing page ${curPage} of ${totalPages} (${total} total records)`
    : "Showing 0 archives";

  if (totalPages <= 1) {
    controls.innerHTML = "";
    return;
  }

  controls.innerHTML = `
    <button class="pagination-btn" onclick="changeArchivePage(${curPage - 1})" ${curPage <= 1 ? "disabled" : ""}>&laquo; Prev</button>
    <span style="font-size:0.8rem; color:#64748b; font-weight:600;">Page ${curPage} / ${totalPages}</span>
    <button class="pagination-btn" onclick="changeArchivePage(${curPage + 1})" ${curPage >= totalPages ? "disabled" : ""}>Next &raquo;</button>
  `;
}

function changeArchivePage(newPage) {
  if (newPage < 1 || newPage > archiveTotalPages) return;
  loadArchives(newPage);
}

function openArchiveDetailModal(scanId) {
  const item = currentArchiveItems.find(x => x.id === scanId);
  if (!item) return;

  const modal = document.getElementById("archiveModal");
  if (!modal) return;

  const codeBadge = document.getElementById("archiveModalCodeBadge");
  if (codeBadge) {
    codeBadge.textContent = formatRejectionCode(item.rejection_code);
  }

  const imgEl = document.getElementById("archiveModalImg");
  const imgBox = document.getElementById("archiveModalImgBox");
  if (item.has_image) {
    imgBox.style.display = "flex";
    imgEl.src = item.image_url;
  } else {
    imgBox.style.display = "none";
    imgEl.src = "";
  }

  const reasonEl = document.getElementById("archiveModalReasonText");
  if (reasonEl) reasonEl.textContent = item.rejection_message || "No explanation recorded";

  const timeEl = document.getElementById("archiveModalTime");
  if (timeEl) timeEl.textContent = item.created_at_local || "---";

  const contextEl = document.getElementById("archiveModalContext");
  if (contextEl) {
    let ctxText = item.is_retry ? "Retry Attempt" : "Initial Food Scan";
    if (item.bill_number) {
      ctxText += ` (Bill #${item.bill_number})`;
    }
    contextEl.textContent = ctxText;
  }

  const aiBox = document.getElementById("archiveModalAiBox");
  const aiPre = document.getElementById("archiveModalAiPre");
  if (item.raw_ai_response && item.raw_ai_response.trim()) {
    aiBox.style.display = "block";
    aiPre.textContent = item.raw_ai_response;
  } else {
    aiBox.style.display = "none";
    aiPre.textContent = "";
  }

  const billBox = document.getElementById("archiveModalBillActionBox");
  const btnViewBill = document.getElementById("btnArchiveModalViewBill");
  if (item.bill_number && btnViewBill) {
    billBox.style.display = "block";
    btnViewBill.onclick = () => viewBillPdfPreview(item.bill_number);
    btnViewBill.innerHTML = `
      <svg class="btn-icon" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line><polyline points="10 9 9 9 8 9"></polyline></svg>
      <span>View Bill #${escapeHtml(item.bill_number)} PDF Preview</span>
    `;
  } else if (billBox) {
    billBox.style.display = "none";
  }

  modal.classList.add("open");
}

function closeArchiveModal() {
  const modal = document.getElementById("archiveModal");
  if (modal) modal.classList.remove("open");
}

