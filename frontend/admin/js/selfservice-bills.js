let _ssSearchTimer = null;

function debouncedLoadSsBills() {
  clearTimeout(_ssSearchTimer);
  _ssSearchTimer = setTimeout(loadSsBills, 240);
}

function fmtSsDate(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "-";
  return d.toLocaleString("en-MY", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function ssStatusBadge(status) {
  const st = String(status || "pending").toLowerCase();
  const styles = {
    paid: "background:#dcfce7; color:#166534; border:1px solid #bbf7d0;",
    pending: "background:#fef3c7; color:#92400e; border:1px solid #fde68a;",
    void: "background:#fee2e2; color:#991b1b; border:1px solid #fecaca;",
    cancelled: "background:#f1f5f9; color:#475569; border:1px solid #e2e8f0;",
    failed: "background:#fee2e2; color:#991b1b; border:1px solid #fecaca;",
  };
  const css = styles[st] || styles.cancelled;
  return `<span style="display:inline-block; padding:0.2rem 0.55rem; border-radius:999px; font-size:0.72rem; font-weight:700; text-transform:uppercase; ${css}">${esc(st)}</span>`;
}

async function loadSsBills() {
  const q = (document.getElementById("ssSearchInput")?.value || "").trim();
  const status = document.getElementById("ssStatusFilter")?.value || "all";
  const params = new URLSearchParams({ status, q, limit: "150", _t: String(Date.now()) });
  const tbody = document.getElementById("ssBillsTbody");
  try {
    const res = await fetch("/api/admin/selfservice-bills?" + params.toString(), { cache: "no-store" });
    if (!res.ok) throw new Error("Failed to load bills");
    const data = await res.json();
    const sum = data.summary || {};
    document.getElementById("ssPaidRev").textContent = "RM " + Number(sum.paid_revenue || 0).toFixed(2);
    document.getElementById("ssPaidSub").textContent = `${sum.paid_count || 0} paid orders`;
    document.getElementById("ssPendingCnt").textContent = String(sum.pending_count || 0);
    document.getElementById("ssVoidCnt").textContent = String(sum.void_count || 0);

    const bills = data.bills || [];
    if (!bills.length) {
      tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; padding:2rem; color:#64748b;">No self-checkout bills found.</td></tr>`;
      return;
    }

    tbody.innerHTML = bills.map((b) => {
      const itemsHtml = (b.items || []).map((it) => {
        const ppp = Number(it.pieces_per_portion || 1);
        const pc = ppp > 1 ? ` <small style="color:#64748b;">(${ppp} pcs)</small>` : "";
        return `<div><strong>${esc(it.name)}</strong> &times; ${Number(it.quantity || 1)}${pc}</div>`;
      }).join("") || "<span style='color:#94a3b8;'>-</span>";

      const metaHtml = `
        <div>${esc(b.receipt_email || "-")}</div>
        ${b.chip_purchase_id ? `<div style="font-size:0.72rem; color:#64748b; font-family:monospace;">CHIP: ${esc(b.chip_purchase_id)}</div>` : ""}
      `;

      const actions = [];
      if (b.status !== "paid") {
        actions.push(`<button type="button" class="btn btn-sm" style="background:#16a34a; color:#fff; padding:0.28rem 0.6rem; font-size:0.75rem;" onclick="updateSsBillStatus('${esc(b.id)}', 'paid')">Mark Paid</button>`);
      }
      if (b.status !== "void") {
        actions.push(`<button type="button" class="btn btn-outline btn-sm" style="color:#dc2626; border-color:#fecaca; padding:0.28rem 0.6rem; font-size:0.75rem;" onclick="updateSsBillStatus('${esc(b.id)}', 'void')">Void</button>`);
      }

      return `
        <tr>
          <td style="font-weight:800; color:#0f172a; white-space:nowrap;">${esc(b.bill_number || b.id.slice(0, 8))}</td>
          <td style="font-size:0.82rem; line-height:1.4;">${itemsHtml}</td>
          <td style="font-weight:800; color:#ff5e36; white-space:nowrap;">RM ${Number(b.total || 0).toFixed(2)}</td>
          <td style="font-size:0.8rem;">${metaHtml}</td>
          <td>${ssStatusBadge(b.status)}</td>
          <td style="font-size:0.78rem; color:#475569; white-space:nowrap;">${esc(fmtSsDate(b.created_at))}</td>
          <td style="white-space:nowrap;"><div style="display:flex; gap:0.35rem;">${actions.join("")}</div></td>
        </tr>
      `;
    }).join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; padding:2rem; color:#ef4444;">Failed to load self-checkout bills.</td></tr>`;
  }
}

async function updateSsBillStatus(billId, status) {
  const reason = status === "void" ? (prompt("Void reason (optional):") || "Voided by admin") : "";
  try {
    const res = await fetch("/api/admin/selfservice-bills/" + encodeURIComponent(billId) + "/status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status, reason }),
    });
    if (!res.ok) throw new Error("Update failed");
    showToast(`Bill updated to ${status}`);
    await loadSsBills();
  } catch (e) {
    showToast("Could not update bill status");
  }
}
