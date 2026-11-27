/* Shared Admin Shell: sidebar navigation, topbar, auth guard, modals, and utilities. */

const ADMIN_NAV_GROUPS = [
  {
    label: "Management",
    items: [
      {
        id: "dashboard",
        label: "Dashboard",
        href: "/admin/dashboard",
        icon: `<rect x="3" y="3" width="7" height="9" rx="1.5"></rect><rect x="14" y="3" width="7" height="5" rx="1.5"></rect><rect x="14" y="12" width="7" height="9" rx="1.5"></rect><rect x="3" y="16" width="7" height="5" rx="1.5"></rect>`,
      },
      {
        id: "bills",
        label: "Bills &amp; Receipts",
        href: "/admin/bills",
        icon: `<path d="M5 3h14v18l-3-2-2.5 2-1.5-2-1.5 2L8 19l-3 2V3z" stroke-linecap="round" stroke-linejoin="round"></path><path d="M9 8h6M9 12h6M9 16h4" stroke-linecap="round"></path>`,
        children: [
          { id: "bills", label: "AFRC Bills", href: "/admin/bills" },
          { id: "selfservice-bills", label: "Self-Checkout Bills", href: "/admin/selfservice-bills" },
        ],
      },
      {
        id: "menu",
        label: "Daily Menu",
        href: "/admin/menu",
        icon: `<path d="M4 3v7a3 3 0 003 3v8M7 3v6M10 3v7a3 3 0 01-3 3M16 3v18M16 3c2.5 0 4 2 4 5.5S18.5 14 16 14" stroke-linecap="round" stroke-linejoin="round"></path>`,
      },
      {
        id: "categories",
        label: "Categories",
        href: "/admin/categories",
        icon: `<path d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z" stroke-linecap="round" stroke-linejoin="round"></path>`,
      },
      {
        id: "restaurant",
        label: "Restaurant's",
        href: "/admin/restaurant",
        icon: `<path d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4" stroke-linecap="round" stroke-linejoin="round"></path>`,
      },
    ],
  },
  {
    label: "Loyalty &amp; Accounts",
    marginTop: "0.65rem",
    items: [
      {
        id: "customers",
        label: "Customers",
        href: "/admin/customers",
        icon: `<path d="M17 20h5v-1a4 4 0 00-5.356-3.772M17 20H7m10 0v-1c0-.653-.126-1.277-.356-1.849M7 20H2v-1a4 4 0 015.356-3.772M7 20v-1c0-.653.126-1.277.356-1.849m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" stroke-linecap="round" stroke-linejoin="round"></path>`,
      },
      {
        id: "points",
        label: "Points",
        href: "/admin/points",
        icon: `<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" stroke-linecap="round" stroke-linejoin="round"></polygon>`,
      },
      {
        id: "vouchers",
        label: "Vouchers",
        href: "/admin/vouchers",
        icon: `<path d="M15 5v2m0 4v2m0 4v2M5 5a2 2 0 00-2 2v3a2 2 0 110 4v3a2 2 0 002 2h14a2 2 0 002-2v-3a2 2 0 110-4V7a2 2 0 00-2-2H5z" stroke-linecap="round" stroke-linejoin="round"></path>`,
      },
    ],
  },
  {
    label: "System &amp; Logs",
    marginTop: "0.65rem",
    items: [
      {
        id: "feedbacks",
        label: "Feedbacks",
        href: "/admin/feedbacks",
        icon: `<path d="M11.049 2.927c.3-.921 1.603-.921 1.902 0l1.519 4.674a1 1 0 00.95.69h4.915c.969 0 1.371 1.24.588 1.81l-3.976 2.888a1 1 0 00-.363 1.118l1.518 4.674c.3.922-.755 1.688-1.538 1.118l-3.976-2.888a1 1 0 00-1.176 0l-3.976 2.888c-.783.57-1.838-.197-1.538-1.118l1.518-4.674a1 1 0 00-.363-1.118l-3.976-2.888c-.784-.57-.38-1.81.588-1.81h4.914a1 1 0 00.951-.69l1.519-4.674z" stroke-linecap="round" stroke-linejoin="round"></path>`,
      },
      {
        id: "audit",
        label: "Audit Log",
        href: "/admin/audit",
        icon: `<path d="M12 3l7 3v5c0 5-3.5 9.2-7 10.5C8.5 20.2 5 16 5 11V6l7-3z" stroke-linecap="round" stroke-linejoin="round"></path><path d="M9.5 12l1.8 1.8L15 10" stroke-linecap="round" stroke-linejoin="round"></path>`,
      },
      {
        id: "archives",
        label: "Archives",
        href: "/admin/archives",
        icon: `<path d="M5 8h14M5 8a2 2 0 110-4h14a2 2 0 110 4M5 8v10a2 2 0 002 2h10a2 2 0 002-2V8m-9 4h4" stroke-linecap="round" stroke-linejoin="round"></path>`,
      },
    ],
  },
];

let _activeAdminTab = "dashboard";
let _onRefreshPage = null;

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[ch]));
}

function escapeHtml(value) {
  return esc(value);
}

function showToast(msg) {
  const t = document.getElementById("toast");
  if (!t) return;
  t.textContent = msg;
  t.style.display = "block";
  clearTimeout(window._toastTimer);
  window._toastTimer = setTimeout(() => { t.style.display = "none"; }, 3000);
}

function toggleSidebar() {
  const sb = document.getElementById("sidebar");
  const bd = document.getElementById("sidebarBackdrop");
  if (!sb) return;
  const isOpen = sb.classList.toggle("open");
  if (bd) bd.classList.toggle("open", isOpen);
}

function closeSidebar() {
  const sb = document.getElementById("sidebar");
  const bd = document.getElementById("sidebarBackdrop");
  if (sb) sb.classList.remove("open");
  if (bd) bd.classList.remove("open");
}

function switchTab(tabId) {
  const slug = String(tabId || "dashboard").trim().toLowerCase();
  window.location.href = `/admin/${encodeURIComponent(slug)}`;
}

function viewPhoto(url) {
  const img = document.getElementById("photoModalImg");
  const modal = document.getElementById("photoModal");
  if (img) img.src = url;
  if (modal) modal.classList.add("open");
}

function hideBrokenThumb(img) {
  const btn = img && img.closest(".photo-thumb-btn");
  if (btn) {
    const span = document.createElement("span");
    span.style.cssText = "color:#94a3b8; font-size:0.78rem;";
    span.textContent = "No photo";
    btn.replaceWith(span);
  }
}

function formatDateTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-MY", {
    year: "numeric", month: "short", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  });
}

async function loadAdminUser() {
  try {
    const res = await fetch("/api/auth/status", { cache: "no-store" });
    if (res.status === 401) {
      window.location.href = `/login?next=/admin/${encodeURIComponent(_activeAdminTab)}`;
      return;
    }
    const data = await res.json();
    if (!data.is_authenticated) {
      window.location.href = `/login?next=/admin/${encodeURIComponent(_activeAdminTab)}`;
      return;
    }
    if (data.username) {
      const el = document.getElementById("currentAdminUsername");
      if (el) el.textContent = data.username;
    }
  } catch (e) {
    console.error("Auth status error:", e);
  }
}

async function logoutAdmin() {
  if (!confirm("Are you sure you want to sign out?")) return;
  try {
    await fetch("/api/auth/logout", { method: "POST" });
  } catch (e) {}
  window.location.href = "/login";
}

function openChangePasswordModal() {
  const form = document.getElementById("changePasswordForm");
  if (form) form.reset();
  const errEl = document.getElementById("passwordError");
  if (errEl) errEl.style.display = "none";
  closeSidebar();
  const modal = document.getElementById("passwordModal");
  if (modal) modal.classList.add("open");
}

function closeChangePasswordModal() {
  const modal = document.getElementById("passwordModal");
  if (modal) modal.classList.remove("open");
}

async function submitChangePassword(e) {
  e.preventDefault();
  const curPwd = document.getElementById("curPasswordInput").value;
  const newPwd = document.getElementById("newPasswordInput").value;
  const confirmPwd = document.getElementById("confirmPasswordInput").value;
  const errEl = document.getElementById("passwordError");

  if (newPwd !== confirmPwd) {
    errEl.textContent = "New passwords do not match.";
    errEl.style.display = "block";
    return;
  }
  if (newPwd.length < 6) {
    errEl.textContent = "New password must be at least 6 characters.";
    errEl.style.display = "block";
    return;
  }

  const btn = document.getElementById("savePasswordBtn");
  btn.disabled = true;
  btn.textContent = "Updating...";

  try {
    const res = await fetch("/api/auth/change-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ old_password: curPwd, new_password: newPwd }),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "Failed to update password.");
    }
    closeChangePasswordModal();
    showToast("Password updated successfully!");
  } catch (err) {
    errEl.textContent = err.message;
    errEl.style.display = "block";
  } finally {
    btn.disabled = false;
    btn.textContent = "Update Password";
  }
}

async function refreshAll() {
  await loadAdminUser();
  if (typeof _onRefreshPage === "function") {
    await _onRefreshPage();
  }
}

function toggleNavSubmenu(id) {
  const wrap = document.getElementById("nav-sub-" + id);
  const btn = document.getElementById("nav-parent-" + id);
  if (!wrap) return;
  const open = wrap.style.display !== "none";
  wrap.style.display = open ? "none" : "flex";
  if (btn) btn.setAttribute("aria-expanded", open ? "false" : "true");
}

function renderAdminSidebar(activeTab) {
  const sidebar = document.getElementById("sidebar");
  if (!sidebar) return;

  const groupsHtml = ADMIN_NAV_GROUPS.map(g => {
    const labelStyle = g.marginTop ? ` style="margin-top:${g.marginTop};"` : "";
    const linksHtml = g.items.map(item => {
      if (Array.isArray(item.children) && item.children.length) {
        const hasActiveChild = item.children.some(c => c.id === activeTab);
        const subHtml = item.children.map(c => `
          <a class="nav-btn${c.id === activeTab ? " active" : ""}" data-tab="${c.id}" href="${c.href}" style="padding-left:2.35rem; font-size:0.84rem;">
            <span class="nav-btn-left">
              <span style="width:6px;height:6px;border-radius:999px;background:${c.id === activeTab ? "#ff5e36" : "#cbd5e1"};display:inline-block;"></span>
              <span>${c.label}</span>
            </span>
          </a>
        `).join("");
        return `
          <button type="button" id="nav-parent-${item.id}" class="nav-btn${hasActiveChild ? " active" : ""}" aria-expanded="${hasActiveChild ? "true" : "false"}" onclick="toggleNavSubmenu('${item.id}')" style="width:100%; justify-content:space-between;">
            <span class="nav-btn-left">
              <svg class="nav-icon" fill="none" stroke="currentColor" stroke-width="1.9" viewBox="0 0 24 24" aria-hidden="true">${item.icon}</svg>
              <span>${item.label}</span>
            </span>
            <svg style="width:14px;height:14px;opacity:0.7;flex-shrink:0;" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" stroke-linecap="round" stroke-linejoin="round"></path></svg>
          </button>
          <div id="nav-sub-${item.id}" style="display:${hasActiveChild ? "flex" : "none"}; flex-direction:column; gap:2px; margin:2px 0 4px;">
            ${subHtml}
          </div>
        `;
      }
      return `
        <a class="nav-btn${item.id === activeTab ? " active" : ""}" data-tab="${item.id}" href="${item.href}">
          <span class="nav-btn-left">
            <svg class="nav-icon" fill="none" stroke="currentColor" stroke-width="1.9" viewBox="0 0 24 24" aria-hidden="true">${item.icon}</svg>
            <span>${item.label}</span>
          </span>
        </a>
      `;
    }).join("");
    return `<div class="nav-label"${labelStyle}>${g.label}</div>${linksHtml}`;
  }).join("");

  sidebar.innerHTML = `
    <div class="nav-group">${groupsHtml}</div>
    <div class="sidebar-footer">
      <div class="nav-label">Quick Links</div>
      <a class="ext-link" href="/" target="_blank">
        <svg class="nav-icon" fill="none" stroke="currentColor" stroke-width="1.9" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M4 8a2 2 0 012-2h2l1.5-2h5L16 6h2a2 2 0 012 2v9a2 2 0 01-2 2H6a2 2 0 01-2-2V8z" stroke-linecap="round" stroke-linejoin="round"></path>
          <circle cx="12" cy="12.5" r="3.2"></circle>
        </svg>
        <span>Customer Camera View</span>
      </a>
      <a class="ext-link" href="/self" target="_blank">
        <svg class="nav-icon" fill="none" stroke="currentColor" stroke-width="1.9" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M3 5h18M5 5v14a2 2 0 002 2h10a2 2 0 002-2V5M9 11h6M9 15h4" stroke-linecap="round" stroke-linejoin="round"></path>
        </svg>
        <span>Self-Service Kiosk</span>
      </a>
    </div>
    <div style="margin-top:auto; padding:1.25rem 1rem 1rem; border-top:1px solid #e2e8f0;">
      <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:0.75rem;">
        <div style="display:flex; align-items:center; gap:0.5rem; overflow:hidden;">
          <div style="width:28px; height:28px; border-radius:50%; background:rgba(255,94,54,0.12); border:1px solid rgba(255,94,54,0.3); display:flex; align-items:center; justify-content:center; color:#ff5e36; font-weight:700; font-size:0.75rem;">A</div>
          <span id="currentAdminUsername" style="font-size:0.825rem; font-weight:700; color:#0f172a; white-space:nowrap; text-overflow:ellipsis; overflow:hidden;">Admin</span>
        </div>
        <button type="button" onclick="openChangePasswordModal()" title="Change Admin Password" style="background:transparent; border:none; color:#64748b; cursor:pointer; padding:0.25rem;" aria-label="Change Password">
          <svg style="width:16px; height:16px;" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <path d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" stroke-linecap="round" stroke-linejoin="round"></path>
          </svg>
        </button>
      </div>
      <button type="button" class="btn btn-outline btn-sm" onclick="logoutAdmin()" style="width:100%; justify-content:center; font-size:0.8rem; color:#ef4444; border-color:rgba(239,68,68,0.25); background:#ffffff;">
        <svg style="width:14px; height:14px; margin-right:0.35rem;" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
          <path d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" stroke-linecap="round" stroke-linejoin="round"></path>
        </svg>
        Sign Out
      </button>
    </div>
  `;
}

function renderAdminTopbar(title) {
  const topbar = document.getElementById("topbar");
  if (!topbar) return;
  topbar.innerHTML = `
    <div class="topbar-left">
      <button class="menu-toggle" aria-label="Toggle navigation" onclick="toggleSidebar()">
        <svg class="nav-icon" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M4 6h16M4 12h16M4 18h16" stroke-linecap="round"></path>
        </svg>
      </button>
      <h1 class="page-title" id="pageTitle">${esc(title)}</h1>
    </div>
    <div class="topbar-actions">
      <button class="btn btn-outline" onclick="refreshAll()">
        <svg class="btn-icon" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M21 12a9 9 0 11-2.64-6.36M21 4v5h-5" stroke-linecap="round" stroke-linejoin="round"></path>
        </svg>
        <span>Refresh Data</span>
      </button>
    </div>
  `;
}

function ensureAdminCommonModals() {
  if (!document.getElementById("photoModal")) {
    const lb = document.createElement("div");
    lb.className = "lightbox";
    lb.id = "photoModal";
    lb.onclick = function () { this.classList.remove("open"); };
    lb.innerHTML = `<img alt="Plate photo" id="photoModalImg" src="">`;
    document.body.appendChild(lb);
  }

  if (!document.getElementById("passwordModal")) {
    const pw = document.createElement("div");
    pw.className = "modal-backdrop";
    pw.id = "passwordModal";
    pw.onclick = function (e) { if (e.target === this) closeChangePasswordModal(); };
    pw.innerHTML = `
      <div class="refund-modal-card" style="max-width: 420px;" role="dialog" aria-modal="true">
        <div class="refund-modal-head">
          <div class="refund-modal-title"><span>Change Admin Password</span></div>
          <button type="button" class="refund-modal-close" onclick="closeChangePasswordModal()" aria-label="Close">&times;</button>
        </div>
        <form id="changePasswordForm" onsubmit="submitChangePassword(event)">
          <div class="refund-modal-body" style="padding:1.25rem;">
            <div style="margin-bottom:1rem;">
              <label style="display:block; font-size:0.8rem; font-weight:600; margin-bottom:0.35rem; color:#475569;">Current Password</label>
              <input type="password" id="curPasswordInput" required class="input-box" style="width:100%;" placeholder="••••••••">
            </div>
            <div style="margin-bottom:1rem;">
              <label style="display:block; font-size:0.8rem; font-weight:600; margin-bottom:0.35rem; color:#475569;">New Password (min 6 chars)</label>
              <input type="password" id="newPasswordInput" required minlength="6" class="input-box" style="width:100%;" placeholder="••••••••">
            </div>
            <div style="margin-bottom:0.5rem;">
              <label style="display:block; font-size:0.8rem; font-weight:600; margin-bottom:0.35rem; color:#475569;">Confirm New Password</label>
              <input type="password" id="confirmPasswordInput" required minlength="6" class="input-box" style="width:100%;" placeholder="••••••••">
            </div>
            <div id="passwordError" style="color:#ef4444; font-size:0.8rem; margin-top:0.6rem; display:none;"></div>
          </div>
          <div class="refund-modal-foot">
            <button type="button" class="btn btn-outline" onclick="closeChangePasswordModal()">Cancel</button>
            <button type="submit" class="btn btn-primary" id="savePasswordBtn">Update Password</button>
          </div>
        </form>
      </div>
    `;
    document.body.appendChild(pw);
  }

  if (!document.getElementById("toast")) {
    const t = document.createElement("div");
    t.className = "toast";
    t.id = "toast";
    document.body.appendChild(t);
  }
}

function initAdminShell({ activeTab, title, onRefresh }) {
  _activeAdminTab = activeTab || "dashboard";
  _onRefreshPage = onRefresh || null;
  document.title = `${title} — AFRC Admin`;
  renderAdminSidebar(_activeAdminTab);
  renderAdminTopbar(title);
  ensureAdminCommonModals();
  refreshAll();
}
