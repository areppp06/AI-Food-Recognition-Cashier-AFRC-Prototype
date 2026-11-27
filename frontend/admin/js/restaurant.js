// ==================== RESTAURANT SETTINGS & SWITCHES ====================
let currentRestaurantConfirmAction = null;
let currentTargetPhrase = "";


async function loadRestaurantSettings() {
  try {
    const res = await fetch("/api/admin/restaurant/settings");
    if (!res.ok) return;
    const data = await res.json();
    
    const csToggle = document.getElementById("toggleComingSoon");
    const csBadge = document.getElementById("badgeComingSoon");
    if (csToggle) csToggle.checked = !!data.coming_soon;
    if (csBadge) {
      if (data.coming_soon) {
        csBadge.textContent = "Active";
        csBadge.style.background = "#eff6ff";
        csBadge.style.color = "#1d4ed8";
      } else {
        csBadge.textContent = "Disabled";
        csBadge.style.background = "#e2e8f0";
        csBadge.style.color = "#475569";
      }
    }

    const maintToggle = document.getElementById("toggleMaintenance");
    const maintBadge = document.getElementById("badgeMaintenance");
    if (maintToggle) maintToggle.checked = !!data.maintenance_mode;
    if (maintBadge) {
      if (data.maintenance_mode) {
        maintBadge.textContent = "Active";
        maintBadge.style.background = "#fef3c7";
        maintBadge.style.color = "#b45309";
      } else {
        maintBadge.textContent = "Disabled";
        maintBadge.style.background = "#e2e8f0";
        maintBadge.style.color = "#475569";
      }
    }

    const emailToggle = document.getElementById("toggleEmailReceipt");
    const emailBadge = document.getElementById("badgeEmailReceipt");
    if (emailToggle) emailToggle.checked = !!data.email_receipt_enabled;
    if (emailBadge) {
      if (data.email_receipt_enabled) {
        emailBadge.textContent = "Active";
        emailBadge.style.background = "#dcfce7";
        emailBadge.style.color = "#15803d";
      } else {
        emailBadge.textContent = "Disabled";
        emailBadge.style.background = "#e2e8f0";
        emailBadge.style.color = "#475569";
      }
    }

    if (data.operation_hours_start) {
      document.getElementById("opHourStartInput").value = data.operation_hours_start;
    }
    if (data.operation_hours_end) {
      document.getElementById("opHourEndInput").value = data.operation_hours_end;
    }
    if (data.current_server_time) {
      document.getElementById("adminServerTimeDisplay").textContent = data.current_server_time;
    }
    const openBadge = document.getElementById("adminLiveOpenBadge");
    if (openBadge) {
      if (data.is_open) {
        openBadge.textContent = "Currently Open";
        openBadge.style.background = "#dcfce7";
        openBadge.style.color = "#15803d";
      } else {
        openBadge.textContent = "Currently Closed";
        openBadge.style.background = "#fee2e2";
        openBadge.style.color = "#b91c1c";
      }
    }
  } catch (e) {
    showToast("Failed to load restaurant settings");
  }
}

function onComingSoonClick(event) {
  event.preventDefault();
  event.stopPropagation();
  const toggle = document.getElementById("toggleComingSoon");
  if (toggle.checked) {
    if (confirm("Disable Coming Soon mode and restore normal customer kiosk?")) {
      updateRestaurantSetting({ coming_soon: false }, "Coming Soon mode disabled!");
    }
  } else {
    openRestaurantConfirmModal("coming_soon");
  }
}

function onMaintenanceClick(event) {
  event.preventDefault();
  event.stopPropagation();
  const toggle = document.getElementById("toggleMaintenance");
  if (toggle.checked) {
    if (confirm("Disable Maintenance mode and resume normal kiosk ordering?")) {
      updateRestaurantSetting({ maintenance_mode: false }, "Maintenance mode disabled!");
    }
  } else {
    openRestaurantConfirmModal("maintenance");
  }
}

async function onEmailReceiptToggle(event) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }
  const toggle = document.getElementById("toggleEmailReceipt");
  const nextVal = !toggle.checked;
  await updateRestaurantSetting({
    email_receipt_enabled: nextVal
  }, nextVal ? "Email receipt field enabled on bill page!" : "Email receipt field disabled & hidden on bill page!");
}

function openRestaurantConfirmModal(actionType) {
  currentRestaurantConfirmAction = actionType;
  const modal = document.getElementById("restaurantConfirmModal");
  const title = document.getElementById("restaurantConfirmTitle");
  const desc = document.getElementById("restaurantConfirmDesc");
  const phraseEl = document.getElementById("restaurantConfirmPhrase");
  const inputEl = document.getElementById("restaurantConfirmInput");
  const confirmBtn = document.getElementById("restaurantConfirmBtn");

  inputEl.value = "";
  confirmBtn.disabled = true;

  if (actionType === "coming_soon") {
    title.textContent = "Enable Coming Soon Feature";
    desc.textContent = "Enabling this will immediately redirect all visitors from the main page (/) to the Coming Soon page. To confirm, type the phrase below in ALL CAPS:";
    currentTargetPhrase = "COMFIRM ENABLE COOMING SOON";
    phraseEl.textContent = currentTargetPhrase;
    confirmBtn.textContent = "Confirm & Enable Coming Soon";
  } else if (actionType === "maintenance") {
    title.textContent = "Enable Maintenance Mode";
    desc.textContent = "Enabling this will immediately redirect all visitors from the main page (/) to the Maintenance page and temporarily pause customer ordering. To confirm, type the phrase below in ALL CAPS:";
    currentTargetPhrase = "COMFIRM ENABLE MAINTAINANCE MODE";
    phraseEl.textContent = currentTargetPhrase;
    confirmBtn.textContent = "Confirm & Enable Maintenance";
  }

  modal.classList.add("open");
  setTimeout(() => inputEl.focus(), 100);
}

function closeRestaurantConfirmModal() {
  document.getElementById("restaurantConfirmModal").classList.remove("open");
  currentRestaurantConfirmAction = null;
  currentTargetPhrase = "";
  loadRestaurantSettings();
}

function checkRestaurantConfirmMatch() {
  const inputEl = document.getElementById("restaurantConfirmInput");
  const confirmBtn = document.getElementById("restaurantConfirmBtn");
  const val = inputEl.value.trim().toUpperCase();
  
  let isMatch = false;
  if (currentRestaurantConfirmAction === "coming_soon") {
    isMatch = (val === "COMFIRM ENABLE COOMING SOON" || val === "CONFIRM ENABLE COMING SOON");
  } else if (currentRestaurantConfirmAction === "maintenance") {
    isMatch = (val === "COMFIRM ENABLE MAINTAINANCE MODE" || val === "CONFIRM ENABLE MAINTENANCE MODE");
  }

  confirmBtn.disabled = !isMatch;
}

async function executeRestaurantConfirmAction() {
  const confirmBtn = document.getElementById("restaurantConfirmBtn");
  if (confirmBtn.disabled) return;
  
  if (currentRestaurantConfirmAction === "coming_soon") {
    document.getElementById("restaurantConfirmModal").classList.remove("open");
    await updateRestaurantSetting({ coming_soon: true }, "Coming Soon mode enabled!");
  } else if (currentRestaurantConfirmAction === "maintenance") {
    document.getElementById("restaurantConfirmModal").classList.remove("open");
    await updateRestaurantSetting({ maintenance_mode: true }, "Maintenance mode enabled!");
  }
}

async function updateRestaurantSetting(updates, msg) {
  try {
    const res = await fetch("/api/admin/restaurant/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(updates)
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showToast(err.detail || "Failed to update settings");
      loadRestaurantSettings();
      return;
    }
    showToast(msg || "Settings updated successfully");
    loadRestaurantSettings();
  } catch (e) {
    showToast("Network error updating settings");
    loadRestaurantSettings();
  }
}

async function handleSaveOperationHours(event) {
  if (event) event.preventDefault();
  const start = document.getElementById("opHourStartInput").value;
  const end = document.getElementById("opHourEndInput").value;
  if (!start || !end) {
    alert("Please specify both opening and closing times.");
    return;
  }
  const btn = document.getElementById("btnSaveOpHours");
  if (btn) btn.disabled = true;
  try {
    await updateRestaurantSetting({
      operation_hours_start: start,
      operation_hours_end: end
    }, "Operating hours saved successfully!");
  } finally {
    if (btn) btn.disabled = false;
  }
}

function setHourPreset(start, end) {
  document.getElementById("opHourStartInput").value = start;
  document.getElementById("opHourEndInput").value = end;
}

