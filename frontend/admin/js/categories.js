let allAdminCategories = [];
let newCatSelectedIcon = "restaurant";
let draggedCatId = null;

function showCategoryError(message) {
  const el = document.getElementById("categoryError");
  if (el) el.textContent = message || "";
}

function getCategoryIcon(catName) {
  const found = allAdminCategories.find(c => (c.name || "").toLowerCase() === String(catName || "").toLowerCase());
  return (found && found.icon) ? found.icon : "restaurant";
}

function selectNewCatIcon(icon, btn) {
  newCatSelectedIcon = icon || "restaurant";
  document.querySelectorAll("#newCatIconPicker .icon-choice-btn").forEach(b => {
    b.classList.toggle("active", b === btn || b.dataset.icon === newCatSelectedIcon);
  });
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

function renderCategoriesDragList() {
  const host = document.getElementById("categoriesDragList");
  if (!host) return;
  if (!allAdminCategories.length) {
    host.innerHTML = `<div class="empty-state-sm">No categories added yet. Create your first category above.</div>`;
    return;
  }
  host.innerHTML = allAdminCategories.map((cat, idx) => `
    <div class="cat-drag-item" draggable="true" data-cat-id="${cat.id}"
         ondragstart="onCategoryDragStart(event, ${cat.id})"
         ondragover="onCategoryDragOver(event)"
         ondragleave="onCategoryDragLeave(event)"
         ondrop="onCategoryDrop(event, ${cat.id})"
         ondragend="onCategoryDragEnd(event)">
      <span class="cat-drag-handle" title="Drag to reorder">⋮⋮</span>
      <span class="badge" style="background:#f1f5f9; color:#475569; font-weight:800; min-width:32px; text-align:center;">#${idx + 1}</span>
      <div class="cat-icon-box">
        <span class="material-symbols-outlined" style="font-size:20px;">${esc(cat.icon || "restaurant")}</span>
      </div>
      <div style="flex:1; min-width:0;">
        <div style="font-weight:700; color:#0f172a; font-size:0.94rem;">${esc(cat.name)}</div>
        <div style="font-size:0.76rem; color:#64748b;">${cat.dish_count || 0} menu items tagged (${cat.available_count || 0} available today)</div>
      </div>
      <div style="display:flex; align-items:center; gap:0.35rem; flex-wrap:wrap;">
        <button type="button" class="btn btn-outline btn-sm" style="padding:0.3rem 0.5rem;" ${idx === 0 ? "disabled" : ""} onclick="moveCategoryOrder(${idx}, -1)" title="Move up">↑</button>
        <button type="button" class="btn btn-outline btn-sm" style="padding:0.3rem 0.5rem;" ${idx === allAdminCategories.length - 1 ? "disabled" : ""} onclick="moveCategoryOrder(${idx}, 1)" title="Move down">↓</button>
        <button type="button" class="btn btn-outline btn-sm" data-id="${cat.id}" data-name="${esc(cat.name)}" onclick="editCategoryPrompt(this.dataset.id, this.dataset.name)">Edit</button>
        <button type="button" class="btn btn-danger btn-sm" data-id="${cat.id}" data-name="${esc(cat.name)}" onclick="deleteCategory(this.dataset.id, this.dataset.name)">Delete</button>
      </div>
    </div>
  `).join("");
}

async function editCategoryPrompt(catId, currentName) {
  const nextName = prompt("Edit Category Name:", currentName || "");
  if (nextName === null) return;
  const trimmed = nextName.trim();
  if (!trimmed || trimmed === currentName) return;
  showCategoryError("");
  try {
    const res = await fetch(`/api/admin/categories/${encodeURIComponent(catId)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: trimmed }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showCategoryError(data.detail || "Failed to update category.");
      return;
    }
    showToast(`Category renamed to "${trimmed}"`);
    await loadCategories();
    await loadMenu();
  } catch (err) {
    showCategoryError("Network error updating category.");
  }
}

function onCategoryDragStart(event, catId) {
  draggedCatId = Number(catId);
  if (event.dataTransfer) {
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", String(catId));
  }
  const el = event.currentTarget;
  if (el) setTimeout(() => el.classList.add("dragging"), 0);
}

function onCategoryDragOver(event) {
  event.preventDefault();
  if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
  const el = event.currentTarget;
  if (el && !el.classList.contains("dragging")) {
    el.classList.add("drag-over");
  }
}

function onCategoryDragLeave(event) {
  const el = event.currentTarget;
  if (el) el.classList.remove("drag-over");
}

async function onCategoryDrop(event, targetCatId) {
  event.preventDefault();
  const el = event.currentTarget;
  if (el) el.classList.remove("drag-over");
  const fromId = Number(draggedCatId || (event.dataTransfer && event.dataTransfer.getData("text/plain")));
  const toId = Number(targetCatId);
  if (!fromId || !toId || fromId === toId) return;

  const fromIdx = allAdminCategories.findIndex(c => Number(c.id) === fromId);
  const toIdx = allAdminCategories.findIndex(c => Number(c.id) === toId);
  if (fromIdx < 0 || toIdx < 0) return;

  const [moved] = allAdminCategories.splice(fromIdx, 1);
  allAdminCategories.splice(toIdx, 0, moved);
  renderCategoriesDragList();
  renderNewDishCategoriesPicker();
  await saveCategoriesOrder();
}

function onCategoryDragEnd(event) {
  draggedCatId = null;
  document.querySelectorAll(".cat-drag-item").forEach(item => {
    item.classList.remove("dragging", "drag-over");
  });
}

async function moveCategoryOrder(idx, delta) {
  const nextIdx = idx + delta;
  if (nextIdx < 0 || nextIdx >= allAdminCategories.length) return;
  const [moved] = allAdminCategories.splice(idx, 1);
  allAdminCategories.splice(nextIdx, 0, moved);
  renderCategoriesDragList();
  renderNewDishCategoriesPicker();
  await saveCategoriesOrder();
}

async function saveCategoriesOrder() {
  const badge = document.getElementById("catOrderSaveBadge");
  if (badge) {
    badge.style.color = "#f59e0b";
    badge.textContent = "Saving order...";
  }
  try {
    const res = await fetch("/api/admin/categories/reorder", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ category_ids: allAdminCategories.map(c => c.id) }),
    });
    if (!res.ok) throw new Error("Failed to save category order");
    const data = await res.json();
    allAdminCategories = Array.isArray(data) ? data : (data.categories || allAdminCategories);
    renderCategoriesDragList();
    renderNewDishCategoriesPicker();
    if (badge) {
      badge.style.color = "#10b981";
      badge.textContent = "Order synced ✓";
    }
    showToast("Category order updated");
    await loadMenu();
  } catch (err) {
    console.error("Category reorder error:", err);
    if (badge) {
      badge.style.color = "#ef4444";
      badge.textContent = "Failed to save order";
    }
    showToast("Failed to save category order");
  }
}

async function handleAddCategory(event) {
  event.preventDefault();
  showCategoryError("");
  const inp = document.getElementById("newCatNameInput");
  const name = (inp ? inp.value : "").trim();
  if (!name) {
    showCategoryError("Category name is required.");
    return;
  }
  const btn = document.getElementById("btnAddCategorySubmit");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/api/admin/categories", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, icon: newCatSelectedIcon || "restaurant" }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showCategoryError(data.detail || "Failed to add category.");
      return;
    }
    if (inp) inp.value = "";
    showToast(`Category "${data.name || name}" added`);
    await loadCategories();
    await loadMenu();
  } catch (err) {
    showCategoryError("Network error adding category.");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function deleteCategory(catId, catName) {
  if (!confirm(`Delete category "${catName}"?\n\nIt will be untagged from all menu items.`)) return;
  showCategoryError("");
  try {
    const res = await fetch(`/api/admin/categories/${encodeURIComponent(catId)}`, {
      method: "DELETE",
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showCategoryError(data.detail || "Failed to delete category.");
      return;
    }
    showToast(`Category "${catName}" deleted`);
    await loadCategories();
    await loadMenu();
  } catch (err) {
    showCategoryError("Network error deleting category.");
  }
}

