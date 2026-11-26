let loyCustPage = 1;
let loyCustStatus = "all";
let loyCustSearchTimer = null;
let loyAdjTargetUser = null;
let loyAdjAction = "add";
let loyPendingSuspend = null;

function onLoyaltyCustomerSearch() {
  clearTimeout(loyCustSearchTimer);
  loyCustSearchTimer = setTimeout(() => loadAdminCustomers(1), 260);
}

function setLoyaltyCustomerFilter(status, btn) {
  loyCustStatus = status;
  document.querySelectorAll("#loyCustomerFilterPills .pill-filter").forEach(b => b.classList.remove("active"));
  if (btn) btn.classList.add("active");
  loadAdminCustomers(1);
}

async function loadAdminCustomers(page = 1) {
  loyCustPage = page;
  const search = (document.getElementById("loyCustomerSearch")?.value || "").trim();
  const tbody = document.getElementById("loyCustomersBody");
  const mList = document.getElementById("loyCustomersMobileList");
  if (!tbody) return;

  try {
    const res = await fetch(`/api/admin/loyalty/customers?page=${page}&limit=15&status=${encodeURIComponent(loyCustStatus)}&search=${encodeURIComponent(search)}`, { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json();
    const list = data.customers || [];
    const subEl = document.getElementById("custVisibleSub");
    if (subEl) {
      subEl.textContent = `Showing ${list.length} of ${Number(data.total || 0).toLocaleString()} customer account${data.total === 1 ? "" : "s"}`;
    }

    if (!list.length) {
      tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; padding:2.5rem 1rem; color:#94a3b8;">No matching customer accounts found.</td></tr>';
      if (mList) mList.innerHTML = '<div class="empty-state-sm">No matching customer accounts found.</div>';
    } else {
      tbody.innerHTML = list.map(u => {
        const bal = Number(u.points_balance || 0);
        const balStyle = bal < 0 ? "color:#dc2626; font-weight:800;" : "color:#059669; font-weight:800;";
        const stBadge = u.is_suspended
          ? '<span class="badge badge-cancelled">Suspended</span>'
          : (bal < 0 ? '<span class="badge badge-pending">Points Debt</span>' : '<span class="badge badge-paid">Active</span>');
        const userJson = JSON.stringify({ id: u.id, name: u.name, email: u.email, points_balance: bal }).replace(/'/g, "&#39;");
        const suspBtnClass = u.is_suspended ? "btn btn-sm" : "btn btn-danger btn-sm";
        const suspBtnStyle = u.is_suspended ? "background:#059669; color:#ffffff;" : "";
        return `
          <tr>
            <td>
              <div style="display:flex; align-items:center; gap:0.75rem;">
                ${renderCustomerAvatarHtml(u)}
                <div>
                  <div style="font-weight:700; color:#0f172a;">${esc(u.name)}</div>
                  <div style="font-size:0.74rem; color:#94a3b8;">ID #${u.id}</div>
                </div>
              </div>
            </td>
            <td>
              <div style="font-size:0.84rem; font-weight:600; color:#1e293b;">${esc(u.email)}</div>
              <div style="font-size:0.76rem; color:#64748b;">${esc(u.phone_number || "No phone number")} · Joined ${esc(fmtShortDate(u.created_at))}</div>
            </td>
            <td style="font-weight:700; color:#0f172a;">${Number(u.order_count || 0)}</td>
            <td style="font-weight:700; color:#0f172a;">RM ${Number(u.total_spent || 0).toFixed(2)}</td>
            <td style="${balStyle}">${bal.toLocaleString()} pts</td>
            <td>${stBadge}</td>
            <td style="text-align:right;">
              <div style="display:inline-flex; gap:0.4rem; flex-wrap:wrap; justify-content:flex-end;">
                <button type="button" class="btn btn-outline btn-sm" onclick="openCustomerDetailModal(${u.id})">History</button>
                <button type="button" class="btn btn-outline btn-sm" onclick='openLoyaltyAdjustModal(${userJson})'>Adjust Pts</button>
                <button type="button" class="${suspBtnClass}" style="${suspBtnStyle}" onclick='openSuspendCustomerModal(${JSON.stringify({ id: u.id, name: u.name, email: u.email, is_suspended: !!u.is_suspended }).replace(/'/g, "&#39;")})'>${u.is_suspended ? "Reactivate" : "Suspend"}</button>
              </div>
            </td>
          </tr>
        `;
      }).join("");

      if (mList) {
        mList.innerHTML = list.map(u => {
          const bal = Number(u.points_balance || 0);
          const stBadge = u.is_suspended
            ? '<span class="badge badge-cancelled">Suspended</span>'
            : (bal < 0 ? '<span class="badge badge-pending">Points Debt</span>' : '<span class="badge badge-paid">Active</span>');
          const userJson = JSON.stringify({ id: u.id, name: u.name, email: u.email, points_balance: bal }).replace(/'/g, "&#39;");
          return `
            <div class="bill-mobile-card">
              <div class="bmc-header">
                <div style="display:flex; align-items:center; gap:0.65rem; min-width:0;">
                  ${renderCustomerAvatarHtml(u)}
                  <div style="min-width:0;">
                    <div style="font-weight:800; font-size:0.92rem; color:#0f172a; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${esc(u.name)}</div>
                    <div style="font-size:0.76rem; color:#64748b; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${esc(u.email)}</div>
                  </div>
                </div>
                ${stBadge}
              </div>
              <div style="display:grid; grid-template-columns:1fr 1fr; gap:0.45rem; font-size:0.8rem;">
                <div style="color:#64748b;">Phone</div>
                <div style="text-align:right; font-weight:600;">${esc(u.phone_number || "None")}</div>
                <div style="color:#64748b;">Joined</div>
                <div style="text-align:right; font-weight:600;">${esc(fmtShortDate(u.created_at))}</div>
                <div style="color:#64748b;">Paid Bills</div>
                <div style="text-align:right; font-weight:700;">${Number(u.order_count || 0)} (RM ${Number(u.total_spent || 0).toFixed(2)})</div>
                <div style="color:#64748b;">Points Balance</div>
                <div style="text-align:right; font-weight:800; color:${bal < 0 ? '#dc2626' : '#059669'};">${bal.toLocaleString()} pts</div>
              </div>
              <div class="bmc-actions">
                <div class="bmc-actions-row">
                  <button type="button" class="btn btn-outline btn-sm bmc-btn" onclick="openCustomerDetailModal(${u.id})">History</button>
                  <button type="button" class="btn btn-outline btn-sm bmc-btn" onclick='openLoyaltyAdjustModal(${userJson})'>Adjust Pts</button>
                </div>
                <button type="button" class="${u.is_suspended ? 'btn btn-sm bmc-btn-full' : 'btn btn-danger btn-sm bmc-btn-full'}" style="${u.is_suspended ? 'background:#059669;color:#ffffff;' : ''}" onclick='openSuspendCustomerModal(${JSON.stringify({ id: u.id, name: u.name, email: u.email, is_suspended: !!u.is_suspended }).replace(/'/g, "&#39;")})'>
                  ${u.is_suspended ? "Reactivate Account" : "Suspend Account"}
                </button>
              </div>
            </div>
          `;
        }).join("");
      }
    }

    document.getElementById("loyCustomersPageInfo").textContent = `Page ${data.page} of ${data.total_pages} (${data.total} total)`;
    document.getElementById("loyCustomersPageBtns").innerHTML = `
      <button type="button" class="page-btn" ${data.page <= 1 ? "disabled" : ""} onclick="loadAdminCustomers(${data.page - 1})">Previous</button>
      <button type="button" class="page-btn" ${data.page >= data.total_pages ? "disabled" : ""} onclick="loadAdminCustomers(${data.page + 1})">Next</button>
    `;
  } catch (e) {}
}

// ---------- Quick Adjust Modal (from Customers table) ----------

function setLoyAdjMode(mode) {
  loyAdjAction = mode;
  const addBtn = document.getElementById("loyAdjBtnAdd");
  const dedBtn = document.getElementById("loyAdjBtnDeduct");
  if (mode === "add") {
    addBtn.className = "btn btn-sm";
    addBtn.style.background = "#059669";
    dedBtn.className = "btn btn-outline btn-sm";
    dedBtn.style.background = "";
  } else {
    dedBtn.className = "btn btn-sm";
    dedBtn.style.background = "#dc2626";
    addBtn.className = "btn btn-outline btn-sm";
    addBtn.style.background = "";
  }
}

function openLoyaltyAdjustModal(u) {
  loyAdjTargetUser = u;
  document.getElementById("loyAdjCustomerName").textContent = `${u.name} (${u.email})`;
  document.getElementById("loyAdjCustomerBalance").textContent = `Current Balance: ${Number(u.points_balance).toLocaleString()} pts`;
  document.getElementById("loyAdjPointsInput").value = "";
  document.getElementById("loyAdjDescInput").value = "";
  setLoyAdjMode("add");
  document.getElementById("loyaltyAdjustModal").classList.add("open");
}

function closeLoyaltyAdjustModal() {
  loyAdjTargetUser = null;
  document.getElementById("loyaltyAdjustModal").classList.remove("open");
}

async function submitLoyaltyAdjust(event) {
  event.preventDefault();
  if (!loyAdjTargetUser) return;
  const pts = parseInt(document.getElementById("loyAdjPointsInput").value, 10);
  const desc = document.getElementById("loyAdjDescInput").value.trim();
  if (!pts || pts <= 0 || !desc) return;
  const btn = document.getElementById("btnSubmitLoyAdj");
  btn.disabled = true;
  try {
    const res = await fetch("/api/admin/loyalty/points/adjust", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_id: loyAdjTargetUser.id,
        action: loyAdjAction,
        points: pts,
        description: desc,
      }),
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(d.detail || "Failed to adjust points.");
      return;
    }
    closeLoyaltyAdjustModal();
    showToast(`Points updated. New balance: ${Number(d.balance).toLocaleString()} pts`);
    loadAdminLoyaltyOverview();
    loadAdminCustomers(loyCustPage);
  } finally {
    btn.disabled = false;
  }
}

// ---------- Customer Suspension Modal & Activity History Modal ----------

function openSuspendCustomerModal(u) {
  const nextSuspended = !u.is_suspended;
  loyPendingSuspend = { id: u.id, name: u.name, email: u.email, nextSuspended };
  document.getElementById("loySuspendModalTitle").textContent = nextSuspended
    ? "Suspend Customer Account"
    : "Reactivate Customer Account";
  document.getElementById("loySuspendModalDesc").textContent = nextSuspended
    ? `Suspend ${u.name} (${u.email})? Active sessions will be revoked and they will not be able to sign in or redeem points until reactivated.`
    : `Reactivate ${u.name} (${u.email})? They will regain access to their loyalty account immediately.`;
  const btn = document.getElementById("btnConfirmSuspendExec");
  btn.textContent = nextSuspended ? "Suspend Account" : "Reactivate Account";
  btn.style.background = nextSuspended ? "#dc2626" : "#059669";
  document.getElementById("loyaltySuspendModal").classList.add("open");
}

function closeSuspendCustomerModal() {
  loyPendingSuspend = null;
  document.getElementById("loyaltySuspendModal").classList.remove("open");
}

async function executeSuspendCustomer() {
  if (!loyPendingSuspend) return;
  const btn = document.getElementById("btnConfirmSuspendExec");
  btn.disabled = true;
  try {
    const res = await fetch(`/api/admin/loyalty/customers/${loyPendingSuspend.id}/suspension`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_suspended: loyPendingSuspend.nextSuspended }),
    });
    if (res.ok) {
      showToast(loyPendingSuspend.nextSuspended ? "Customer account suspended" : "Customer account reactivated");
      closeSuspendCustomerModal();
      loadAdminLoyaltyOverview();
      loadAdminCustomers(loyCustPage);
    } else {
      const d = await res.json().catch(() => ({}));
      alert(d.detail || "Failed to update account status.");
    }
  } finally {
    btn.disabled = false;
  }
}

async function openCustomerDetailModal(id) {
  const modal = document.getElementById("loyaltyCustomerDetailModal");
  const body = document.getElementById("loyCustDetailBody");
  body.innerHTML = '<div style="padding:1.5rem; text-align:center; color:#64748b;">Loading customer activity...</div>';
  modal.classList.add("open");
  try {
    const res = await fetch(`/api/admin/loyalty/customers/${id}`, { cache: "no-store" });
    if (!res.ok) { body.textContent = "Failed to load customer history."; return; }
    const d = await res.json();
    const c = d.customer;
    document.getElementById("loyCustDetailTitle").textContent = `${c.name} (${Number(c.points_balance || 0).toLocaleString()} pts)`;
    const txHtml = (d.transactions || []).length
      ? d.transactions.map(t => `
          <div style="display:flex; justify-content:space-between; align-items:center; gap:0.75rem; padding:0.6rem 0; border-bottom:1px solid #f1f5f9; font-size:0.83rem;">
            <div>
              <div style="font-weight:600; color:#0f172a;">${esc(t.description)}</div>
              <div style="color:#94a3b8; font-size:0.74rem;">${esc(t.created_at ? new Date(t.created_at).toLocaleString("en-MY") : "")}</div>
            </div>
            <strong style="color:${t.points > 0 ? '#059669' : '#dc2626'}; white-space:nowrap;">${t.points > 0 ? '+' : ''}${Number(t.points).toLocaleString()} pts</strong>
          </div>`).join("")
      : '<div style="color:#94a3b8; font-size:0.82rem; padding:0.5rem 0;">No points ledger entries yet.</div>';

    const ordHtml = (d.orders || []).length
      ? d.orders.map(o => `
          <div style="display:flex; justify-content:space-between; align-items:center; gap:0.75rem; padding:0.6rem 0; border-bottom:1px solid #f1f5f9; font-size:0.83rem;">
            <div>
              <div><span class="bill-code" style="font-size:0.76rem; padding:0.15rem 0.45rem;">#${esc(o.bill_number)}</span> <span style="color:#334155; font-weight:600;">${esc(o.items_summary)}</span></div>
              <div style="color:#94a3b8; font-size:0.74rem; margin-top:0.15rem;">${esc(o.paid_at ? new Date(o.paid_at).toLocaleString("en-MY") : "")}</div>
            </div>
            <div style="text-align:right; white-space:nowrap;">
              <div style="font-weight:800; color:#0f172a;">RM ${Number(o.total).toFixed(2)}</div>
              <div style="font-size:0.74rem; font-weight:700; color:#059669;">+${Number(o.points_earned || 0).toLocaleString()} pts</div>
            </div>
          </div>`).join("")
      : '<div style="color:#94a3b8; font-size:0.82rem; padding:0.5rem 0;">No paid bills linked yet.</div>';

    body.innerHTML = `
      <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:12px; padding:0.85rem 1rem; display:flex; justify-content:space-between; flex-wrap:wrap; gap:0.5rem; font-size:0.82rem;">
        <div><span style="color:#64748b;">Email:</span> <strong>${esc(c.email)}</strong></div>
        <div><span style="color:#64748b;">Phone:</span> <strong>${esc(c.phone_number || "Not set")}</strong></div>
      </div>
      <div>
        <div style="font-size:0.76rem; font-weight:800; text-transform:uppercase; letter-spacing:0.05em; color:#64748b; margin-bottom:0.35rem;">Points Ledger History</div>
        ${txHtml}
      </div>
      <div>
        <div style="font-size:0.76rem; font-weight:800; text-transform:uppercase; letter-spacing:0.05em; color:#64748b; margin-bottom:0.35rem;">Linked Paid Bills</div>
        ${ordHtml}
      </div>`;
  } catch (e) {
    body.textContent = "Error loading customer details.";
  }
}

function closeCustomerDetailModal() {
  document.getElementById("loyaltyCustomerDetailModal").classList.remove("open");
}

// ---------- Redemption Tiers & Cooldown Rules ----------

