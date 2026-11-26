let ptsBalPage = 1;
let ptsUserSearchTimer = null;
let ptsSideSelectedUser = null;
let ptsSideAction = "add";
let loyEditingTierId = null;

function onPtsUserSearch() {
  clearTimeout(ptsUserSearchTimer);
  ptsUserSearchTimer = setTimeout(() => loadPointsBalances(1), 260);
}

async function loadPointsBalances(page = 1) {
  ptsBalPage = page;
  const search = (document.getElementById("ptsUserSearch")?.value || "").trim();
  const tbody = document.getElementById("ptsBalancesBody");
  const mList = document.getElementById("ptsBalancesMobileList");
  if (!tbody) return;

  try {
    const res = await fetch(`/api/admin/loyalty/customers?page=${page}&limit=15&sort=points&search=${encodeURIComponent(search)}`, { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json();
    const list = data.customers || [];

    if (!list.length) {
      tbody.innerHTML = '<tr><td colspan="4" style="text-align:center; padding:2.25rem 1rem; color:#94a3b8;">No customer accounts found.</td></tr>';
      if (mList) mList.innerHTML = '<div class="empty-state-sm">No customer accounts found.</div>';
    } else {
      tbody.innerHTML = list.map(u => {
        const bal = Number(u.points_balance || 0);
        const isSelected = ptsSideSelectedUser && ptsSideSelectedUser.id === u.id;
        const userJson = JSON.stringify({ id: u.id, name: u.name, email: u.email, points_balance: bal }).replace(/'/g, "&#39;");
        return `
          <tr class="${isSelected ? 'loy-selected-row' : ''}">
            <td style="font-weight:700; color:#0f172a;">${esc(u.name)}</td>
            <td style="font-size:0.84rem; color:#475569;">${esc(u.email)}</td>
            <td style="text-align:right; font-weight:800; color:${bal < 0 ? '#dc2626' : '#d97706'};">${bal.toLocaleString()} pts</td>
            <td style="text-align:center;">
              <button type="button" class="btn ${isSelected ? 'btn-dark' : 'btn-outline'} btn-sm" onclick='selectSideAdjustUser(${userJson})'>
                ${isSelected ? "Selected" : "Adjust"}
              </button>
            </td>
          </tr>
        `;
      }).join("");

      if (mList) {
        mList.innerHTML = list.map(u => {
          const bal = Number(u.points_balance || 0);
          const userJson = JSON.stringify({ id: u.id, name: u.name, email: u.email, points_balance: bal }).replace(/'/g, "&#39;");
          return `
            <div class="bill-mobile-card">
              <div style="display:flex; justify-content:space-between; align-items:center; gap:0.5rem;">
                <div style="min-width:0;">
                  <div style="font-weight:800; color:#0f172a;">${esc(u.name)}</div>
                  <div style="font-size:0.76rem; color:#64748b; overflow:hidden; text-overflow:ellipsis;">${esc(u.email)}</div>
                </div>
                <div style="font-weight:800; color:${bal < 0 ? '#dc2626' : '#d97706'}; white-space:nowrap;">${bal.toLocaleString()} pts</div>
              </div>
              <button type="button" class="btn btn-outline btn-sm" style="width:100%;" onclick='selectSideAdjustUser(${userJson})'>Adjust Points</button>
            </div>
          `;
        }).join("");
      }
    }

    document.getElementById("ptsBalancesPageInfo").textContent = `Page ${data.page} of ${data.total_pages} (${data.total} total)`;
    document.getElementById("ptsBalancesPageBtns").innerHTML = `
      <button type="button" class="page-btn" ${data.page <= 1 ? "disabled" : ""} onclick="loadPointsBalances(${data.page - 1})">Previous</button>
      <button type="button" class="page-btn" ${data.page >= data.total_pages ? "disabled" : ""} onclick="loadPointsBalances(${data.page + 1})">Next</button>
    `;
  } catch (e) {}
}

function selectSideAdjustUser(u) {
  ptsSideSelectedUser = u;
  document.getElementById("ptsSideEmptyState").style.display = "none";
  document.getElementById("ptsSideAdjustForm").style.display = "block";
  document.getElementById("ptsSideCustName").textContent = u.name;
  document.getElementById("ptsSideCustMeta").textContent = `${u.email} · Balance: ${Number(u.points_balance || 0).toLocaleString()} pts`;
  document.getElementById("ptsSideAmountInput").value = "";
  document.getElementById("ptsSideDescInput").value = "";
  setSideAdjMode("add");
  loadPointsBalances(ptsBalPage);
  const card = document.getElementById("ptsAdjustSideCard");
  if (card && window.innerWidth <= 1080) {
    card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

function clearSideAdjustUser() {
  ptsSideSelectedUser = null;
  document.getElementById("ptsSideAdjustForm").style.display = "none";
  document.getElementById("ptsSideEmptyState").style.display = "block";
  loadPointsBalances(ptsBalPage);
}

function setSideAdjMode(mode) {
  ptsSideAction = mode;
  const addBtn = document.getElementById("ptsSideBtnAdd");
  const dedBtn = document.getElementById("ptsSideBtnDeduct");
  const warn = document.getElementById("ptsSideDeductWarn");
  if (mode === "add") {
    addBtn.className = "adj-mode-btn active-add";
    dedBtn.className = "adj-mode-btn";
    if (warn) warn.style.display = "none";
  } else {
    addBtn.className = "adj-mode-btn";
    dedBtn.className = "adj-mode-btn active-deduct";
    if (warn) warn.style.display = "block";
  }
}

async function submitSideLoyaltyAdjust(event) {
  event.preventDefault();
  if (!ptsSideSelectedUser) return;
  const pts = parseInt(document.getElementById("ptsSideAmountInput").value, 10);
  const desc = document.getElementById("ptsSideDescInput").value.trim();
  if (!pts || pts <= 0 || !desc) return;

  const btn = document.getElementById("btnPtsSideSubmit");
  btn.disabled = true;
  btn.textContent = "Processing...";
  try {
    const res = await fetch("/api/admin/loyalty/points/adjust", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_id: ptsSideSelectedUser.id,
        action: ptsSideAction,
        points: pts,
        description: desc,
      }),
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(d.detail || "Failed to adjust points.");
      return;
    }
    const debtNote = d.debtPoints > 0 ? ` (Points debt: ${d.debtPoints})` : "";
    showToast(`Points updated. New balance: ${Number(d.balance).toLocaleString()} pts${debtNote}`);
    clearSideAdjustUser();
    loadAdminLoyaltyOverview();
    loadPointsBalances(ptsBalPage);
  } finally {
    btn.disabled = false;
    btn.textContent = "Confirm Adjustment";
  }
}

async function loadAdminTiers() {
  const tbody = document.getElementById("loyTiersBody");
  if (!tbody) return;
  try {
    const res = await fetch("/api/admin/loyalty/tiers", { cache: "no-store" });
    if (!res.ok) return;
    const tiers = await res.json();
    if (!tiers.length) {
      tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; padding:1.75rem; color:#94a3b8;">No redemption tiers configured.</td></tr>';
      return;
    }
    tbody.innerHTML = tiers.map(t => {
      const ptsReq = Number(t.points_required || 0);
      const discRm = Number(t.discount_amount || 0);
      const ptsPerRm = discRm > 0 ? Math.round(ptsReq / discRm) : 0;
      return `
        <tr>
          <td style="font-weight:800; color:#0f172a;">${ptsReq.toLocaleString()} pts</td>
          <td style="font-weight:800; color:#059669;">RM ${discRm.toFixed(2)}</td>
          <td style="font-size:0.82rem; color:#64748b;">${ptsPerRm} pts / RM 1</td>
          <td>${t.is_active ? '<span class="badge badge-paid">Active</span>' : '<span class="badge badge-cancelled">Inactive</span>'}</td>
          <td style="text-align:right;">
            <div style="display:inline-flex; gap:0.4rem;">
              <button type="button" class="btn btn-outline btn-sm" onclick='openTierModal(${JSON.stringify(t)})'>Edit</button>
              <button type="button" class="btn btn-danger btn-sm" onclick="deleteAdminTier(${t.id})">Delete</button>
            </div>
          </td>
        </tr>
      `;
    }).join("");
  } catch (e) {}
}

function openTierModal(tier = null) {
  loyEditingTierId = tier ? tier.id : null;
  document.getElementById("loyTierModalTitle").textContent = tier ? "Edit Redemption Tier" : "Add Redemption Tier";
  document.getElementById("loyTierPointsInput").value = tier ? tier.points_required : "";
  document.getElementById("loyTierDiscInput").value = tier ? tier.discount_amount : "";
  document.getElementById("loyTierActiveInput").checked = tier ? !!tier.is_active : true;
  document.getElementById("loyaltyTierModal").classList.add("open");
}

function closeTierModal() {
  loyEditingTierId = null;
  document.getElementById("loyaltyTierModal").classList.remove("open");
}

async function saveTierModal(event) {
  event.preventDefault();
  const payload = {
    points_required: parseInt(document.getElementById("loyTierPointsInput").value, 10),
    discount_amount: parseFloat(document.getElementById("loyTierDiscInput").value),
    is_active: document.getElementById("loyTierActiveInput").checked,
  };
  const url = loyEditingTierId ? `/api/admin/loyalty/tiers/${loyEditingTierId}` : "/api/admin/loyalty/tiers";
  const method = loyEditingTierId ? "PUT" : "POST";
  const res = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok) { alert(d.detail || "Could not save tier."); return; }
  closeTierModal();
  showToast("Redemption tier saved");
  loadAdminTiers();
}

async function deleteAdminTier(id) {
  if (!confirm("Delete this redemption tier?")) return;
  const res = await fetch(`/api/admin/loyalty/tiers/${id}`, { method: "DELETE" });
  if (res.ok) {
    showToast("Tier deleted");
    loadAdminTiers();
  }
}

function onCooldownConfirmInput() {
  const inp = document.getElementById("loyCooldownConfirmInput");
  const btn = document.getElementById("btnSaveCooldownRule");
  if (btn && inp) {
    btn.disabled = inp.value.trim() !== "CONFIRM";
  }
}

async function saveLoyaltyCooldown(event) {
  event.preventDefault();
  const confirmInp = document.getElementById("loyCooldownConfirmInput");
  if (!confirmInp || confirmInp.value.trim() !== "CONFIRM") return;
  const hrs = parseInt(document.getElementById("loyCooldownInput").value, 10) || 0;
  const btn = document.getElementById("btnSaveCooldownRule");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/api/admin/loyalty/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ voucher_cooldown_hours: hrs }),
    });
    if (res.ok) {
      confirmInp.value = "";
      showToast("Redemption cooldown rule updated");
    } else {
      const d = await res.json().catch(() => ({}));
      alert(d.detail || "Failed to update cooldown rule.");
      onCooldownConfirmInput();
    }
  } catch (e) {
    onCooldownConfirmInput();
  }
}

// ---------- Vouchers Tab ----------

