let allAdminCategories = [];
let currentMenuDishes = [];
let newDishSelectedCategories = [];
let activePickerDishId = null;
let pickerSearchQuery = "";

function getCategoryIcon(catName) {
  const found = allAdminCategories.find(c => (c.name || "").toLowerCase() === String(catName || "").toLowerCase());
  return (found && found.icon) ? found.icon : "restaurant";
}

async function loadCategories() {
  try {
    const res = await fetch("/api/categories?_t=" + Date.now(), { cache: "no-store" });
    if (!res.ok) return;
    allAdminCategories = await res.json();
    renderCategoriesDragList();
    renderNewDishCategoriesPicker();
  } catch (err) {
    console.error("Failed to load categories:", err);
  }
}

function renderNewDishCategoriesPicker() {
  const host = document.getElementById("newDishCategoriesPicker");
  if (!host) return;
  const validNames = new Set(allAdminCategories.map(c => c.name));
  newDishSelectedCategories = newDishSelectedCategories.filter(n => validNames.has(n));
  host.innerHTML = renderDishCategoryBox("new", newDishSelectedCategories);
}

function renderDishCategoryBox(dishId, selectedCategories) {
  const selected = Array.isArray(selectedCategories) ? selectedCategories.slice(0, 3) : [];

  return `
    <div class="cat-select-chips-container" data-dish-cat-chips="${dishId}">
      ${selected.map(cat => `
        <span class="cat-selected-chip" data-cat="${esc(cat)}">
          <span>${esc(cat)}</span>
          <button type="button" class="cat-selected-chip-remove" onclick="removeDishCategoryTag('${dishId}', '${esc(cat)}', event)" aria-label="Remove ${esc(cat)}">×</button>
        </span>
      `).join("")}
      <button type="button" class="cat-add-trigger-btn" onclick="openDishCategoryPicker('${dishId}')">
        <span>+ Add</span>
        <svg style="width:12px; height:12px;" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path d="M19 9l-7 7-7-7" stroke-linecap="round" stroke-linejoin="round"></path></svg>
      </button>
    </div>
  `;
}

function updateDishCategoryBoxes(dishId) {
  let selected = [];
  if (String(dishId) === "new") {
    selected = newDishSelectedCategories;
    const boxHost = document.getElementById("newDishCategoriesPicker");
    if (boxHost) {
      boxHost.innerHTML = renderDishCategoryBox("new", selected);
    }
    return;
  }
  const dishObj = currentMenuDishes.find(x => Number(x.id) === Number(dishId));
  if (dishObj && Array.isArray(dishObj.categories)) {
    selected = dishObj.categories.slice(0, 3);
  }
  document.querySelectorAll(`[data-dish-cat-chips="${dishId}"]`).forEach(host => {
    host.innerHTML = `
      ${selected.map(cat => `
        <span class="cat-selected-chip" data-cat="${esc(cat)}">
          <span>${esc(cat)}</span>
          <button type="button" class="cat-selected-chip-remove" onclick="removeDishCategoryTag('${dishId}', '${esc(cat)}', event)" aria-label="Remove ${esc(cat)}">×</button>
        </span>
      `).join("")}
      <button type="button" class="cat-add-trigger-btn" onclick="openDishCategoryPicker('${dishId}')">
        <span>+ Add</span>
        <svg style="width:12px; height:12px;" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path d="M19 9l-7 7-7-7" stroke-linecap="round" stroke-linejoin="round"></path></svg>
      </button>
    `;
  });
}

function removeDishCategoryTag(dishId, catName, event) {
  if (event) {
    event.stopPropagation();
    event.preventDefault();
  }
  if (!catName) return;

  if (String(dishId) === "new") {
    const idx = newDishSelectedCategories.indexOf(catName);
    if (idx >= 0) newDishSelectedCategories.splice(idx, 1);
    updateDishCategoryBoxes("new");
    if (String(activePickerDishId) === "new") renderPickerCategoriesList();
    return;
  }

  let dishObj = currentMenuDishes.find(x => Number(x.id) === Number(dishId));
  if (!dishObj) {
    dishObj = { id: Number(dishId), categories: getDishSelectedCategories(dishId) };
    currentMenuDishes.push(dishObj);
  }
  if (!Array.isArray(dishObj.categories)) dishObj.categories = [];

  const idx = dishObj.categories.indexOf(catName);
  if (idx >= 0) {
    dishObj.categories.splice(idx, 1);
  }
  updateDishCategoryBoxes(dishId);
  if (String(activePickerDishId) === String(dishId)) {
    renderPickerCategoriesList();
  }
  flushAutoSaveDish(dishId);
}

function openDishCategoryPicker(dishId) {
  activePickerDishId = dishId;
  pickerSearchQuery = "";
  const searchInp = document.getElementById("catPickerSearchInput");
  if (searchInp) searchInp.value = "";
  const clearBtn = document.getElementById("catPickerClearSearch");
  if (clearBtn) clearBtn.style.display = "none";

  renderPickerCategoriesList();

  const modal = document.getElementById("dishCategoryPickerModal");
  if (modal) modal.classList.add("open");
}

function closeDishCategoryPicker() {
  const modal = document.getElementById("dishCategoryPickerModal");
  if (modal) modal.classList.remove("open");
  activePickerDishId = null;
}

function onFilterPickerCategories(val) {
  pickerSearchQuery = (val || "").trim().toLowerCase();
  const clearBtn = document.getElementById("catPickerClearSearch");
  if (clearBtn) clearBtn.style.display = pickerSearchQuery ? "block" : "none";
  renderPickerCategoriesList();
}

function clearPickerSearch() {
  pickerSearchQuery = "";
  const searchInp = document.getElementById("catPickerSearchInput");
  if (searchInp) {
    searchInp.value = "";
    searchInp.focus();
  }
  const clearBtn = document.getElementById("catPickerClearSearch");
  if (clearBtn) clearBtn.style.display = "none";
  renderPickerCategoriesList();
}

function getActivePickerSelectedCategories() {
  if (String(activePickerDishId) === "new") {
    return newDishSelectedCategories;
  }
  const dishObj = currentMenuDishes.find(x => Number(x.id) === Number(activePickerDishId));
  if (dishObj && Array.isArray(dishObj.categories)) {
    return dishObj.categories;
  }
  return [];
}

function renderPickerCategoriesList() {
  const listHost = document.getElementById("catPickerList");
  const countBadge = document.getElementById("catPickerCountBadge");
  if (!listHost) return;

  const selected = getActivePickerSelectedCategories();
  if (countBadge) {
    countBadge.textContent = `${selected.length}/3 selected`;
  }

  const catNames = allAdminCategories.map(c => c.name);
  selected.forEach(s => {
    if (!catNames.includes(s)) catNames.push(s);
  });

  const filtered = catNames.filter(name => {
    if (!pickerSearchQuery) return true;
    return name.toLowerCase().includes(pickerSearchQuery);
  });

  if (!filtered.length) {
    listHost.innerHTML = `
      <div style="text-align:center; padding:2.5rem 1rem; color:#64748b; font-size:0.9rem;">
        ${catNames.length === 0 ? "No categories created yet." : "No categories match your search."}
      </div>
    `;
    return;
  }

  listHost.innerHTML = filtered.map(name => {
    const isSel = selected.includes(name);
    return `
      <div class="cat-picker-item ${isSel ? "selected" : ""}" onclick="togglePickerCategory('${esc(name)}')">
        <div class="cat-picker-item-left">
          <span class="cat-picker-item-name">${esc(name)}</span>
        </div>
        <div class="cat-picker-radio ${isSel ? "checked" : ""}">
          ${isSel ? '<span class="cat-picker-checkmark">✓</span>' : ""}
        </div>
      </div>
    `;
  }).join("");
}

function togglePickerCategory(catName) {
  if (!catName || activePickerDishId === null) return;

  if (String(activePickerDishId) === "new") {
    const idx = newDishSelectedCategories.indexOf(catName);
    if (idx >= 0) {
      newDishSelectedCategories.splice(idx, 1);
    } else {
      if (newDishSelectedCategories.length >= 3) {
        showToast("Each menu item can tag up to 3 categories.");
        return;
      }
      newDishSelectedCategories.push(catName);
    }
    updateDishCategoryBoxes("new");
    renderPickerCategoriesList();
    return;
  }

  let dishObj = currentMenuDishes.find(x => Number(x.id) === Number(activePickerDishId));
  if (!dishObj) {
    dishObj = { id: Number(activePickerDishId), categories: getDishSelectedCategories(activePickerDishId) };
    currentMenuDishes.push(dishObj);
  }
  if (!Array.isArray(dishObj.categories)) dishObj.categories = [];

  const idx = dishObj.categories.indexOf(catName);
  if (idx >= 0) {
    dishObj.categories.splice(idx, 1);
  } else {
    if (dishObj.categories.length >= 3) {
      showToast("Each menu item can tag up to 3 categories.");
      return;
    }
    dishObj.categories.push(catName);
  }

  updateDishCategoryBoxes(activePickerDishId);
  renderPickerCategoriesList();
  flushAutoSaveDish(activePickerDishId);
}

function getDishSelectedCategories(dishId) {
  if (String(dishId) === "new") {
    return Array.isArray(newDishSelectedCategories) ? newDishSelectedCategories.slice(0, 3) : [];
  }
  const dishObj = currentMenuDishes.find(x => Number(x.id) === Number(dishId));
  if (dishObj && Array.isArray(dishObj.categories)) {
    return dishObj.categories.slice(0, 3);
  }
  const host = document.querySelector(`[data-dish-cat-chips="${dishId}"]`);
  if (!host) return [];
  return [...host.querySelectorAll(".cat-selected-chip")].map(b => b.dataset.cat).filter(Boolean).slice(0, 3);
}

function showError(message) {
  document.getElementById("error").textContent = message || "";
}

function toggleAddDishForm() {
  if (window.innerWidth > 767) return;
  const panel = document.getElementById("addDishPanel");
  if (!panel) return;
  panel.classList.toggle("open");
  const badge = document.getElementById("addDishToggleBadge");
  if (badge) {
    badge.textContent = panel.classList.contains("open") ? "Tap to close" : "Tap to open";
  }
}

function onMobileSwitchToggle(dishId, checkbox) {
  const wrap = checkbox.closest(".switch-wrap");
  if (wrap) {
    const lbl = wrap.querySelector(".switch-label");
    if (lbl) {
      lbl.textContent = checkbox.checked ? "Available" : "Unavailable";
      lbl.classList.toggle("active", checkbox.checked);
    }
  }
  flushAutoSaveDish(dishId);
}

async function loadMenu() {
  if (!allAdminCategories.length) {
    await loadCategories();
  }
  const res = await fetch("/api/menu?_t=" + Date.now(), { cache: "no-store" });
  const menu = await res.json();
  currentMenuDishes = Array.isArray(menu) ? menu : [];
  const tbody = document.getElementById("menuBody");
  const mobileList = document.getElementById("menuMobileList");

  if (tbody) {
    tbody.innerHTML = currentMenuDishes.map(d => `
      <tr data-dish-id="${d.id}">
        <td style="font-weight:700;">
          <div>${esc(d.name)}</div>
          <div style="font-size:0.7rem; color:#64748b; font-weight:400;">AI Vision Name</div>
        </td>
        <td style="min-width:160px;">
          <input type="text" class="input-box alias" placeholder="Same as AI name" value="${esc(d.alias || "")}" data-id="${d.id}" oninput="triggerAutoSaveDish(${d.id})" onblur="flushAutoSaveDish(${d.id})" title="Customer-facing alias name">
        </td>
        <td style="min-width:180px;">${renderDishCategoryBox(d.id, d.categories)}</td>
        <td><textarea class="input-box desc" maxlength="500" rows="2" placeholder="What one serving looks like" data-id="${d.id}" oninput="triggerAutoSaveDish(${d.id})" onblur="flushAutoSaveDish(${d.id})">${esc(d.description || "")}</textarea></td>
        <td style="width:110px;"><input type="number" min="1" max="100" step="1" class="input-box pieces" required value="${d.pieces_per_portion || 1}" data-id="${d.id}" oninput="triggerAutoSaveDish(${d.id})" onblur="flushAutoSaveDish(${d.id})"></td>
        <td style="width:190px;"><div class="price-stepper-wrap"><button type="button" class="price-step-btn" onclick="stepDishPrice(${d.id}, -1)" aria-label="Decrease price by 1">−</button><div class="input-with-prefix"><span class="currency-prefix">RM</span><input type="number" step="1" min="0" value="${Math.round(d.price) == d.price ? Number(d.price).toFixed(0) : Number(d.price).toFixed(2)}" data-id="${d.id}" class="input-box prefix-input price" oninput="triggerAutoSaveDish(${d.id})" onblur="flushAutoSaveDish(${d.id})"></div><button type="button" class="price-step-btn" onclick="stepDishPrice(${d.id}, 1)" aria-label="Increase price by 1">+</button></div></td>
        <td style="width:100px; text-align:center;"><input type="checkbox" style="width:18px; height:18px;" ${d.available ? "checked" : ""} data-id="${d.id}" class="avail" onchange="flushAutoSaveDish(${d.id})"></td>
        <td style="width:80px;"><button type="button" class="btn btn-danger btn-sm" data-id="${d.id}" data-name="${esc(d.name)}" onclick="removeDish(this)">Remove</button></td>
      </tr>
    `).join("");
    const noRow = document.createElement("tr");
    noRow.id = "menuNoMatchRow";
    noRow.style.display = "none";
    noRow.innerHTML = '<td colspan="8" style="text-align:center; padding:2.5rem 1rem; color:#94a3b8; font-style:italic;">No dishes match your search query.</td>';
    tbody.appendChild(noRow);
  }

  if (mobileList) {
    if (!currentMenuDishes.length) {
      mobileList.innerHTML = '<div style="text-align:center; color:#94a3b8; padding:2rem;">No menu items added yet.</div>';
    } else {
      mobileList.innerHTML = currentMenuDishes.map(d => `
        <div class="menu-mobile-card" data-dish-id="${d.id}">
          <div class="mmc-head">
            <div>
              <div class="mmc-name">${esc(d.name)}</div>
              <div style="font-size:0.7rem; color:#64748b;">AI Vision Name</div>
            </div>
            <label class="switch-wrap" title="Toggle availability">
              <span class="switch-label ${d.available ? 'active' : ''}">${d.available ? 'Available' : 'Unavailable'}</span>
              <span class="switch-toggle">
                <input type="checkbox" class="avail switch-input" ${d.available ? "checked" : ""} data-id="${d.id}" onchange="onMobileSwitchToggle(${d.id}, this)">
                <span class="switch-slider"></span>
              </span>
            </label>
          </div>
          <label class="card-input-label" style="margin-top:0.35rem;">
            <span>Customer Alias (Human Friendly Name)</span>
            <input type="text" class="input-box alias" placeholder="Same as AI name" value="${esc(d.alias || "")}" data-id="${d.id}" oninput="triggerAutoSaveDish(${d.id})" onblur="flushAutoSaveDish(${d.id})">
          </label>
          <div style="margin-top:0.45rem;">
            ${renderDishCategoryBox(d.id, d.categories)}
          </div>
          <div class="mmc-grid">
            <label class="card-input-label">
              <span>Price</span>
              <div class="price-stepper-wrap">
                <button type="button" class="price-step-btn" onclick="stepDishPrice(${d.id}, -1)" aria-label="Decrease price by 1">−</button>
                <div class="input-with-prefix">
                  <span class="currency-prefix">RM</span>
                  <input type="number" step="1" min="0" value="${Math.round(d.price) == d.price ? Number(d.price).toFixed(0) : Number(d.price).toFixed(2)}" data-id="${d.id}" class="input-box prefix-input price" oninput="triggerAutoSaveDish(${d.id})" onblur="flushAutoSaveDish(${d.id})">
                </div>
                <button type="button" class="price-step-btn" onclick="stepDishPrice(${d.id}, 1)" aria-label="Increase price by 1">+</button>
              </div>
            </label>
            <label class="card-input-label">
              <span>Pieces / Portion</span>
              <input type="number" min="1" max="100" step="1" value="${d.pieces_per_portion || 1}" data-id="${d.id}" class="input-box pieces" required oninput="triggerAutoSaveDish(${d.id})" onblur="flushAutoSaveDish(${d.id})">
            </label>
          </div>
          <label class="card-input-label" style="margin-top:0.35rem;">
            <span>Description for AI Vision</span>
            <textarea class="input-box desc" maxlength="500" rows="2" placeholder="What one serving looks like..." data-id="${d.id}" oninput="triggerAutoSaveDish(${d.id})" onblur="flushAutoSaveDish(${d.id})">${esc(d.description || "")}</textarea>
          </label>
          <div class="mmc-foot">
            <button type="button" class="btn btn-outline btn-sm" style="color:#dc2626; border-color:#fecaca;" data-id="${d.id}" data-name="${esc(d.name)}" onclick="removeDish(this)">
              <svg style="width:13px; height:13px;" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" stroke-linecap="round" stroke-linejoin="round"></path></svg>
              <span>Delete Dish</span>
            </button>
          </div>
        </div>
      `).join("");
      const noMobile = document.createElement("div");
      noMobile.id = "menuMobileNoMatch";
      noMobile.style.display = "none";
      noMobile.style.padding = "2.5rem 1rem";
      noMobile.style.textAlign = "center";
      noMobile.style.color = "#94a3b8";
      noMobile.style.fontStyle = "italic";
      noMobile.textContent = "No dishes match your search query.";
      mobileList.appendChild(noMobile);
    }
  }
  filterMenuItems();
}

function filterMenuItems() {
  const input = document.getElementById("menuSearchInput");
  const query = (input ? input.value : "").trim().toLowerCase();
  const clearBtn = document.getElementById("clearMenuSearchBtn");
  if (clearBtn) clearBtn.style.display = query ? "block" : "none";

  const rows = document.querySelectorAll("#menuBody tr[data-dish-id]");
  const cards = document.querySelectorAll("#menuMobileList .menu-mobile-card[data-dish-id]");
  
  let totalCount = rows.length;
  let matchCount = 0;

  rows.forEach(row => {
    const dishId = row.dataset.dishId;
    const dishName = (row.querySelector("td:first-child div:first-child")?.textContent || "").toLowerCase();
    const aliasInp = row.querySelector(".alias");
    const descInp = row.querySelector(".desc");
    const aliasVal = (aliasInp ? aliasInp.value : "").toLowerCase();
    const descVal = (descInp ? descInp.value : "").toLowerCase();
    const catVal = getDishSelectedCategories(dishId).join(" ").toLowerCase();
    
    const isMatch = !query || dishName.includes(query) || aliasVal.includes(query) || descVal.includes(query) || catVal.includes(query);
    row.style.display = isMatch ? "" : "none";
    if (isMatch) matchCount++;
  });

  cards.forEach(card => {
    const dishId = card.dataset.dishId;
    const dishName = (card.querySelector(".mmc-name")?.textContent || "").toLowerCase();
    const aliasInp = card.querySelector(".alias");
    const descInp = card.querySelector(".desc");
    const aliasVal = (aliasInp ? aliasInp.value : "").toLowerCase();
    const descVal = (descInp ? descInp.value : "").toLowerCase();
    const catVal = getDishSelectedCategories(dishId).join(" ").toLowerCase();
    
    const isMatch = !query || dishName.includes(query) || aliasVal.includes(query) || descVal.includes(query) || catVal.includes(query);
    card.style.display = isMatch ? "" : "none";
  });

  const noMatchRow = document.getElementById("menuNoMatchRow");
  if (noMatchRow) {
    noMatchRow.style.display = (totalCount > 0 && matchCount === 0) ? "" : "none";
  }
  const noMatchMobile = document.getElementById("menuMobileNoMatch");
  if (noMatchMobile) {
    noMatchMobile.style.display = (totalCount > 0 && matchCount === 0) ? "" : "none";
  }

  const countEl = document.getElementById("menuItemsCount");
  if (countEl) {
    if (!query) {
      countEl.textContent = `${totalCount} ${totalCount === 1 ? "dish" : "dishes"}`;
    } else {
      countEl.textContent = `Found ${matchCount} of ${totalCount} ${totalCount === 1 ? "dish" : "dishes"}`;
    }
  }
}

function clearMenuSearch() {
  const input = document.getElementById("menuSearchInput");
  if (input) {
    input.value = "";
    input.focus();
  }
  filterMenuItems();
}

const _menuDebounceTimers = {};

function setAutoSaveIndicator(state) {
  const el = document.getElementById("menuAutoSaveStatus");
  if (!el) return;
  if (state === "saving") {
    el.style.color = "#f59e0b";
    el.innerHTML = `
      <svg style="width:14px; height:14px; animation: spin 1s linear infinite;" fill="none" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="3" stroke-dasharray="32" stroke-linecap="round"></circle></svg>
      <span>Saving...</span>
    `;
  } else if (state === "saved") {
    el.style.color = "#10b981";
    el.innerHTML = `
      <svg style="width:14px; height:14px;" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M5 13l4 4L19 7" stroke-linecap="round" stroke-linejoin="round"></path></svg>
      <span>All changes saved</span>
    `;
  } else if (state === "error") {
    el.style.color = "#ef4444";
    el.innerHTML = `
      <svg style="width:14px; height:14px;" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" stroke-linecap="round" stroke-linejoin="round"></path></svg>
      <span>Failed to save</span>
    `;
  }
}

function stepPriceInput(inputId, delta) {
  const el = typeof inputId === "string" ? document.getElementById(inputId) : inputId;
  if (!el) return;
  let cur = Math.round(parseFloat(el.value) || 0);
  let next = Math.max(0, cur + delta);
  el.value = next;
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

function stepDishPrice(dishId, delta) {
  const inputs = document.querySelectorAll(`.input-box.price[data-id="${dishId}"]`);
  inputs.forEach(inp => {
    let cur = Math.round(parseFloat(inp.value) || 0);
    let next = Math.max(0, cur + delta);
    inp.value = next;
  });
  flushAutoSaveDish(dishId);
}

function triggerAutoSaveDish(dishId) {
  setAutoSaveIndicator("saving");
  if (_menuDebounceTimers[dishId]) {
    clearTimeout(_menuDebounceTimers[dishId]);
  }
  _menuDebounceTimers[dishId] = setTimeout(() => {
    saveDishById(dishId);
  }, 600);
}

function flushAutoSaveDish(dishId) {
  if (_menuDebounceTimers[dishId]) {
    clearTimeout(_menuDebounceTimers[dishId]);
    delete _menuDebounceTimers[dishId];
  }
  saveDishById(dishId);
}

async function saveDishById(dishId) {
  const row = document.querySelector(`tr[data-dish-id="${dishId}"]`);
  const card = document.querySelector(`.menu-mobile-card[data-dish-id="${dishId}"]`);
  const container = (window.innerWidth <= 767 && card) ? card : (row || card);
  if (!container) return;

  const aliasInput = container.querySelector(".alias");
  const priceInput = container.querySelector(".price");
  const descInput = container.querySelector(".desc");
  const piecesInput = container.querySelector(".pieces");
  const availInput = container.querySelector(".avail");

  // Sync to other container if present
  const other = container === card ? row : card;
  if (other) {
    const a = other.querySelector(".alias"); if (a && aliasInput) a.value = aliasInput.value;
    const p = other.querySelector(".price"); if (p && priceInput) p.value = priceInput.value;
    const d = other.querySelector(".desc"); if (d && descInput) d.value = descInput.value;
    const pc = other.querySelector(".pieces"); if (pc && piecesInput) pc.value = piecesInput.value;
    const av = other.querySelector(".avail"); if (av && availInput) av.checked = availInput.checked;
    if (other === card && availInput) {
      const lbl = card.querySelector(".switch-label");
      if (lbl) {
        lbl.textContent = availInput.checked ? "Available" : "Unavailable";
        lbl.classList.toggle("active", availInput.checked);
      }
    }
  }

  let priceRaw = (priceInput ? priceInput.value : "").trim().replace(',', '.');
  if (priceRaw.startsWith('.')) priceRaw = '0' + priceRaw;
  const priceVal = parseFloat(priceRaw);
  if (isNaN(priceVal) || priceVal < 0.10) {
    showError("Price must be at least RM 0.10.");
    setAutoSaveIndicator("error");
    return;
  }
  if (!/^\d+(\.\d{1,2})?$/.test(priceRaw)) {
    showError("Price can have at most 2 decimal places (e.g. 1.50).");
    setAutoSaveIndicator("error");
    return;
  }

  const dishPayload = [{
    id: parseInt(dishId),
    alias: aliasInput ? aliasInput.value.trim() : "",
    price: parseFloat(priceVal.toFixed(2)),
    description: descInput ? descInput.value : "",
    pieces_per_portion: piecesInput ? piecesInput.value : "1",
    categories: getDishSelectedCategories(dishId),
    available: availInput ? availInput.checked : true
  }];

  setAutoSaveIndicator("saving");
  try {
    const res = await fetch("/api/menu", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(dishPayload)
    });
    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      showError(errData.detail || "Save failed");
      throw new Error("Save failed");
    }
    setAutoSaveIndicator("saved");
  } catch (err) {
    console.error("Auto-save error:", err);
    setAutoSaveIndicator("error");
  }
}

async function addDish(event) {
  event.preventDefault();
  showError("");
  const name = document.getElementById("newName").value.trim();
  const alias = (document.getElementById("newAlias")?.value || "").trim();
  const priceInput = document.getElementById("newPrice");
  let priceRaw = (priceInput?.value || "").trim().replace(',', '.');
  if (priceRaw.startsWith('.')) priceRaw = '0' + priceRaw;
  const priceVal = parseFloat(priceRaw);

  if (isNaN(priceVal) || priceVal < 0.10) {
    showError("Price must be at least RM 0.10.");
    if (priceInput) priceInput.focus();
    return;
  }
  if (!/^\d+(\.\d{1,2})?$/.test(priceRaw)) {
    showError("Price can have at most 2 decimal places (e.g. 1.50).");
    if (priceInput) priceInput.focus();
    return;
  }

  const body = {
    name,
    alias,
    price: priceVal.toFixed(2),
    description: document.getElementById("newDescription").value,
    pieces_per_portion: document.getElementById("newPieces").value,
    categories: newDishSelectedCategories,
    available: true
  };
  const res = await fetch("/api/menu/item", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    showError(data.detail || "Could not add the dish.");
    return;
  }
  document.getElementById("newName").value = "";
  if (document.getElementById("newAlias")) document.getElementById("newAlias").value = "";
  document.getElementById("newPrice").value = "1";
  document.getElementById("newDescription").value = "";
  document.getElementById("newPieces").value = "1";
  newDishSelectedCategories = [];
  renderNewDishCategoriesPicker();
  showToast("Dish added to menu");
  const panel = document.getElementById("addDishPanel");
  if (panel && window.innerWidth <= 767) {
    panel.classList.remove("open");
    const badge = document.getElementById("addDishToggleBadge");
    if (badge) badge.textContent = "Tap to open";
  }
  await loadMenu();
}

async function removeDish(button) {
  const id = button.dataset.id;
  const name = button.dataset.name;
  if (!confirm(`Remove ${name} from the menu?`)) return;
  showError("");
  const res = await fetch(`/api/menu/${id}`, { method: "DELETE" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    showError(data.detail || "Could not remove the dish.");
    return;
  }
  showToast("Dish removed");
  await loadMenu();
}

async function saveMenu() {
  showError("");
  const isMobile = window.innerWidth <= 767;
  const cards = [...document.querySelectorAll("#menuMobileList .menu-mobile-card")];
  const rows = [...document.querySelectorAll("#menuBody tr")];
  const items = (isMobile && cards.length) ? cards : rows;

  for (const r of items) {
    const priceEl = r.querySelector(".price");
    const pRaw = (priceEl ? priceEl.value : "").trim();
    const pVal = parseFloat(pRaw);
    if (isNaN(pVal) || pVal < 0.10) {
      showError("Each dish price must be at least RM 0.10.");
      return;
    }
    if (!/^\d+(\.\d{1,2})?$/.test(pRaw)) {
      showError("Dish price can have at most 2 decimal places (e.g. 1.50).");
      return;
    }
  }

  const dishes = items.map(r => {
    const priceEl = r.querySelector(".price");
    const id = r.dataset.dishId || (priceEl ? priceEl.dataset.id : 0);
    const pVal = parseFloat(priceEl ? priceEl.value : 0) || 0;
    return {
      id: parseInt(id),
      alias: r.querySelector(".alias") ? r.querySelector(".alias").value.trim() : "",
      price: parseFloat(pVal.toFixed(2)),
      description: r.querySelector(".desc") ? r.querySelector(".desc").value : "",
      pieces_per_portion: r.querySelector(".pieces") ? r.querySelector(".pieces").value : "1",
      categories: getDishSelectedCategories(id),
      available: r.querySelector(".avail") ? r.querySelector(".avail").checked : true
    };
  });
  setAutoSaveIndicator("saving");
  const res = await fetch("/api/menu", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(dishes)
  });
  if (!res.ok) {
    showError("Could not save menu changes.");
    setAutoSaveIndicator("error");
    return;
  }
  setAutoSaveIndicator("saved");
  showToast("Menu changes saved!");
}

function exportMenuCsv() {
  window.location.href = "/api/admin/menu/export";
}

function triggerImportMenuCsv() {
  const proceed = confirm(
    "⚠️ WARNING: OVERWRITE MENU\n\n" +
    "Importing a CSV will COMPLETELY OVERWRITE and replace all current dishes in the menu.\n\n" +
    "Are you sure you want to proceed?"
  );
  if (!proceed) return;

  const fileInput = document.getElementById("menuCsvFileInput");
  if (fileInput) {
    fileInput.value = "";
    fileInput.click();
  }
}

async function handleMenuCsvFileSelected(event) {
  const file = event.target.files && event.target.files[0];
  if (!file) return;

  const btnImport = document.getElementById("btnImportMenuCsv");
  const origHtml = btnImport ? btnImport.innerHTML : "";
  if (btnImport) {
    btnImport.disabled = true;
    btnImport.innerHTML = `<span>Importing...</span>`;
  }

  showToast("Uploading and overwriting menu...");

  try {
    const formData = new FormData();
    formData.append("file", file);

    const res = await fetch("/api/admin/menu/import", {
      method: "POST",
      body: formData,
    });

    const data = await res.json();
    if (!res.ok) {
      alert("CSV Import Failed:\n\n" + (data.detail || "Unknown error occurred"));
      showToast(data.detail || "Import failed");
    } else {
      showToast(data.message || `Menu imported successfully (${data.count} items).`);
      await loadMenu();
    }
  } catch (err) {
    alert("Network or server error during CSV import:\n\n" + err.message);
    showToast("Error importing menu CSV");
  } finally {
    if (btnImport) {
      btnImport.disabled = false;
      btnImport.innerHTML = origHtml;
    }
    event.target.value = "";
  }
}
