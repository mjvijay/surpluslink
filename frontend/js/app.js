/**
 * SurplusLink Frontend Application (Vanilla JavaScript)
 * Matches all Flask REST API routes and handles SPA navigation, Leaflet maps,
 * freshness countdown bars, geolocation sorting, and 30-second auto-refresh.
 */

const state = {
  token: localStorage.getItem("surpluslink_token") || null,
  user: null,
  currentView: "home",
  listings: [],
  myListings: [],
  userCoords: null, // { lat, lng } when "Sort by My Distance" is active
  boardMap: null,
  boardMarkersLayer: null,
  pickerMap: null,
  pickerMarker: null,
  refreshTimer: null,
};

const FOOD_LABELS = {
  cooked: "Cooked Meals",
  bakery: "Bakery & Bread",
  produce: "Fresh Produce",
  packaged: "Packaged Food",
  dairy: "Dairy & Chilled",
};

// ---------------------------------------------------------------------------
// API Helper & Toast Notifications
// ---------------------------------------------------------------------------
async function apiRequest(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (state.token) {
    headers["Authorization"] = `Bearer ${state.token}`;
  }

  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));

  if (!response.ok) {
    throw new Error(data.error || `Request failed (${response.status})`);
  }
  return data;
}

function showToast(message, type = "info") {
  const container = document.getElementById("toast-container");
  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  container.appendChild(toast);

  setTimeout(() => {
    toast.remove();
  }, 4200);
}

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

// ---------------------------------------------------------------------------
// Time & Freshness Bar Calculation
// ---------------------------------------------------------------------------
function computeFreshness(createdAtIso, pickupByIso, status) {
  const now = Date.now();
  const end = new Date(pickupByIso).getTime();
  const start = createdAtIso ? new Date(createdAtIso).getTime() : end - 6 * 3600 * 1000;
  const remainingMs = end - now;

  if (status === "completed") {
    return { pct: 100, label: "Pickup Completed", colorClass: "fill-green", markerColor: "#16a34a" };
  }
  if (status === "cancelled") {
    return { pct: 0, label: "Cancelled", colorClass: "fill-grey", markerColor: "#64748b" };
  }
  if (remainingMs <= 0 || status === "expired") {
    return { pct: 0, label: "Expired", colorClass: "fill-red", markerColor: "#e11d48" };
  }

  const totalSpan = Math.max(end - start, 3600 * 1000);
  const pct = Math.max(6, Math.min(100, Math.round((remainingMs / totalSpan) * 100)));

  const totalMinutes = Math.floor(remainingMs / 60000);
  const hours = Math.floor(totalMinutes / 60);
  const mins = totalMinutes % 60;
  const timeText = hours > 0 ? `${hours}h ${mins}m left` : `${mins}m left`;

  const hoursLeft = remainingMs / (3600 * 1000);
  let colorClass = "fill-green";
  let markerColor = "#16a34a";

  if (hoursLeft <= 1.5) {
    colorClass = "fill-red";
    markerColor = "#e11d48";
  } else if (hoursLeft <= 4) {
    colorClass = "fill-amber";
    markerColor = "#f59e0b";
  }

  if (status === "claimed") {
    markerColor = "#2563eb";
  }

  return { pct, label: timeText, colorClass, markerColor };
}

function formatLocalDateTime(isoString) {
  if (!isoString) return "—";
  const d = new Date(isoString);
  return d.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// ---------------------------------------------------------------------------
// View Navigation
// ---------------------------------------------------------------------------
function switchView(viewName) {
  if (viewName === "my" && !state.user) {
    openModal("auth-modal");
    return;
  }
  if (viewName === "admin" && (!state.user || state.user.role !== "admin")) {
    showToast("Admin access required.", "error");
    return;
  }

  state.currentView = viewName;

  document.querySelectorAll(".view-section").forEach((sec) => sec.classList.remove("active"));
  const targetSection = document.getElementById(`view-${viewName}`);
  if (targetSection) targetSection.classList.add("active");

  document.querySelectorAll(".nav-link").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.nav === viewName);
  });

  // Close mobile menu if open
  document.getElementById("main-nav").classList.remove("open");

  if (viewName === "home") {
    loadStats();
  } else if (viewName === "board") {
    initBoardMap();
    setTimeout(() => state.boardMap && state.boardMap.invalidateSize(), 100);
    loadBoardListings();
  } else if (viewName === "my") {
    loadMyActivity();
  } else if (viewName === "admin") {
    loadAdminDashboard();
  }
}

// ---------------------------------------------------------------------------
// Auth & Session Management
// ---------------------------------------------------------------------------
function updateAuthUI() {
  const userPill = document.getElementById("user-pill");
  const openAuthBtn = document.getElementById("open-auth-btn");
  const postBtn = document.getElementById("post-surplus-btn");
  const myPostBtn = document.getElementById("my-post-btn");
  const navMyBtn = document.getElementById("nav-my-btn");
  const navAdminBtn = document.getElementById("nav-admin-btn");

  if (state.user) {
    userPill.classList.remove("hidden");
    openAuthBtn.classList.add("hidden");
    navMyBtn.classList.remove("hidden");

    document.getElementById("user-pill-name").textContent = state.user.name;
    const roleEl = document.getElementById("user-pill-role");
    roleEl.textContent = state.user.role;
    roleEl.className = `role-badge role-${state.user.role}`;

    const canPost = state.user.role === "donor" || state.user.role === "admin";
    postBtn.classList.toggle("hidden", !canPost);
    myPostBtn.classList.toggle("hidden", !canPost);
    navAdminBtn.classList.toggle("hidden", state.user.role !== "admin");
  } else {
    userPill.classList.add("hidden");
    openAuthBtn.classList.remove("hidden");
    postBtn.classList.add("hidden");
    myPostBtn.classList.add("hidden");
    navMyBtn.classList.add("hidden");
    navAdminBtn.classList.add("hidden");
  }
}

async function restoreSession() {
  if (!state.token) {
    updateAuthUI();
    return;
  }
  try {
    const data = await apiRequest("/api/me");
    state.user = data.user;
  } catch {
    state.token = null;
    state.user = null;
    localStorage.removeItem("surpluslink_token");
  }
  updateAuthUI();
}

function logout() {
  state.token = null;
  state.user = null;
  localStorage.removeItem("surpluslink_token");
  updateAuthUI();
  showToast("You have been logged out.");
  switchView("home");
  loadBoardListings();
}

// ---------------------------------------------------------------------------
// Impact Stats
// ---------------------------------------------------------------------------
async function loadStats() {
  try {
    const stats = await apiRequest("/api/stats");
    document.getElementById("stat-meals").textContent = stats.meals_shared;
    document.getElementById("stat-pickups").textContent = stats.pickups_completed;
    document.getElementById("stat-listed").textContent = stats.listed_now;
    document.getElementById("stat-donors").textContent = stats.donors;
    document.getElementById("stat-receivers").textContent = stats.receivers;
    document.getElementById("nav-live-badge").textContent = stats.listed_now;
  } catch (err) {
    console.error("Failed to load stats:", err);
  }
}

// ---------------------------------------------------------------------------
// Leaflet Maps (Board Map & Modal Picker Map)
// ---------------------------------------------------------------------------
function initBoardMap() {
  if (state.boardMap) return;

  state.boardMap = L.map("board-map").setView([13.0674, 80.2376], 12);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(state.boardMap);

  state.boardMarkersLayer = L.layerGroup().addTo(state.boardMap);
}

function createColoredPin(color) {
  return L.divIcon({
    className: "custom-pin",
    html: `<div class="pin-bubble" style="background:${color};"></div>`,
    iconSize: [28, 28],
    iconAnchor: [14, 28],
    popupAnchor: [0, -28],
  });
}

function renderBoardMarkers(listings) {
  if (!state.boardMap || !state.boardMarkersLayer) return;
  state.boardMarkersLayer.clearLayers();

  const bounds = [];

  listings.forEach((item) => {
    const freshness = computeFreshness(item.created_at, item.pickup_by, item.status);
    const marker = L.marker([item.lat, item.lng], {
      icon: createColoredPin(freshness.markerColor),
    });

    const distHtml =
      item.distance_km !== null ? ` &bull; <strong>${item.distance_km} km</strong>` : "";

    marker.bindPopup(`
      <div style="min-width:190px;">
        <strong>${escapeHtml(item.title)}</strong><br/>
        <span>${item.quantity} servings (${escapeHtml(FOOD_LABELS[item.food_type] || item.food_type)})</span><br/>
        <small>${escapeHtml(item.address)}${distHtml}</small><br/>
        <small><strong>${freshness.label}</strong></small>
      </div>
    `);

    marker.addTo(state.boardMarkersLayer);
    bounds.push([item.lat, item.lng]);
  });

  if (state.userCoords) {
    const userMarker = L.circleMarker([state.userCoords.lat, state.userCoords.lng], {
      radius: 8,
      color: "#0f3d2e",
      fillColor: "#fbbf24",
      fillOpacity: 1,
      weight: 3,
    }).bindPopup("<strong>Your Location</strong>");
    userMarker.addTo(state.boardMarkersLayer);
    bounds.push([state.userCoords.lat, state.userCoords.lng]);
  }

  if (bounds.length > 0) {
    state.boardMap.fitBounds(bounds, { padding: [35, 35], maxZoom: 14 });
  }
}

function initPickerMap(lat = 13.0674, lng = 80.2376) {
  if (!state.pickerMap) {
    state.pickerMap = L.map("picker-map").setView([lat, lng], 13);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap",
    }).addTo(state.pickerMap);

    state.pickerMarker = L.marker([lat, lng], { draggable: true }).addTo(state.pickerMap);

    state.pickerMap.on("click", (e) => {
      setPickerCoords(e.latlng.lat, e.latlng.lng);
    });

    state.pickerMarker.on("dragend", () => {
      const pos = state.pickerMarker.getLatLng();
      setPickerCoords(pos.lat, pos.lng);
    });
  } else {
    state.pickerMap.setView([lat, lng], 13);
    setPickerCoords(lat, lng);
  }

  setTimeout(() => state.pickerMap.invalidateSize(), 120);
}

function setPickerCoords(lat, lng) {
  const roundedLat = Number(lat).toFixed(5);
  const roundedLng = Number(lng).toFixed(5);
  document.getElementById("listing-lat").value = roundedLat;
  document.getElementById("listing-lng").value = roundedLng;
  document.getElementById("lat-display").textContent = roundedLat;
  document.getElementById("lng-display").textContent = roundedLng;
  if (state.pickerMarker) {
    state.pickerMarker.setLatLng([roundedLat, roundedLng]);
  }
}

// ---------------------------------------------------------------------------
// Card Rendering & Listing Actions
// ---------------------------------------------------------------------------
function buildListingCardHTML(item) {
  const freshness = computeFreshness(item.created_at, item.pickup_by, item.status);
  const categoryLabel = FOOD_LABELS[item.food_type] || item.food_type;
  const user = state.user;

  const distanceBadge =
    item.distance_km !== null
      ? `<span class="distance-pill">📍 ${item.distance_km} km away</span>`
      : "";

  // Private phone contact box (only returned by server when claimed & authorized)
  let contactBoxHtml = "";
  if (item.donor_phone || item.receiver_phone) {
    contactBoxHtml = `
      <div class="contact-box">
        <div><strong>🔓 Verified Pickup Contacts Unlocked</strong></div>
        ${
          item.donor_phone
            ? `<div>Donor: <strong>${escapeHtml(item.donor_name)}</strong> (${escapeHtml(
                item.donor_phone
              )})</div>`
            : ""
        }
        ${
          item.receiver_phone
            ? `<div>Claimed by: <strong>${escapeHtml(item.receiver_name)}</strong> (${escapeHtml(
                item.receiver_phone
              )})</div>`
            : ""
        }
      </div>
    `;
  }

  // Action buttons based on role & listing status
  const buttons = [];

  if (item.status === "available") {
    if (!user) {
      buttons.push(
        `<button type="button" class="btn btn-sm btn-primary" data-action="login-to-claim">Sign in to Claim</button>`
      );
    } else if (user.role === "receiver") {
      buttons.push(
        `<button type="button" class="btn btn-sm btn-mango" data-action="claim" data-id="${item.id}">Claim Listing</button>`
      );
    }
    if (user && (user.id === item.donor_id || user.role === "admin")) {
      buttons.push(
        `<button type="button" class="btn btn-sm btn-outline" data-action="edit" data-id="${item.id}">Edit</button>`
      );
      buttons.push(
        `<button type="button" class="btn btn-sm btn-ghost" data-action="cancel" data-id="${item.id}">Cancel</button>`
      );
    }
  } else if (item.status === "claimed") {
    if (user && (user.id === item.claimed_by || user.role === "admin")) {
      buttons.push(
        `<button type="button" class="btn btn-sm btn-primary" data-action="complete" data-id="${item.id}">✓ Mark Picked Up</button>`
      );
      buttons.push(
        `<button type="button" class="btn btn-sm btn-outline" data-action="release" data-id="${item.id}">Release Claim</button>`
      );
    } else if (user && user.id === item.donor_id) {
      buttons.push(
        `<button type="button" class="btn btn-sm btn-primary" data-action="complete" data-id="${item.id}">✓ Confirm Handed Over</button>`
      );
      buttons.push(
        `<button type="button" class="btn btn-sm btn-ghost" data-action="cancel" data-id="${item.id}">Cancel</button>`
      );
    }
  }

  if (user && (user.id === item.donor_id || user.role === "admin")) {
    buttons.push(
      `<button type="button" class="btn btn-sm btn-ghost" data-action="delete" data-id="${item.id}">Delete</button>`
    );
  }

  const donorDisplay = item.donor_org
    ? `${escapeHtml(item.donor_name)} (${escapeHtml(item.donor_org)})`
    : escapeHtml(item.donor_name);

  return `
    <article class="listing-card" data-listing-id="${item.id}">
      <div class="card-top">
        <div class="card-badges">
          <span class="food-tag tag-${escapeHtml(item.food_type)}">${escapeHtml(categoryLabel)}</span>
          <span class="status-pill status-${escapeHtml(item.status)}">${escapeHtml(item.status)}</span>
          ${distanceBadge}
        </div>
        <strong>${item.quantity} Servings</strong>
      </div>

      <h3 class="card-title">${escapeHtml(item.title)}</h3>
      <div class="card-meta">
        <span>By <strong>${donorDisplay}</strong></span> &bull;
        <span>📍 ${escapeHtml(item.address)}</span>
      </div>

      ${item.description ? `<p class="card-desc">${escapeHtml(item.description)}</p>` : ""}

      <div class="freshness-wrap">
        <div class="freshness-labels">
          <span>Pickup by ${formatLocalDateTime(item.pickup_by)}</span>
          <span>${freshness.label}</span>
        </div>
        <div class="freshness-track">
          <div class="freshness-fill ${freshness.colorClass}" style="width: ${freshness.pct}%"></div>
        </div>
      </div>

      ${contactBoxHtml}

      <div class="card-actions">
        <button type="button" class="btn btn-sm btn-ghost" data-action="focus-map" data-lat="${item.lat}" data-lng="${item.lng}">
          🗺️ View on Map
        </button>
        <div class="card-action-btns">
          ${buttons.join("")}
        </div>
      </div>
    </article>
  `;
}

// ---------------------------------------------------------------------------
// Load Board Listings & My Activity
// ---------------------------------------------------------------------------
async function loadBoardListings() {
  const q = document.getElementById("filter-q").value.trim();
  const foodType = document.getElementById("filter-food-type").value;
  const status = document.getElementById("filter-status").value;

  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (foodType) params.set("food_type", foodType);
  if (status) params.set("status", status);
  if (state.userCoords) {
    params.set("lat", state.userCoords.lat);
    params.set("lng", state.userCoords.lng);
  }

  try {
    const data = await apiRequest(`/api/listings?${params.toString()}`);
    state.listings = data.listings || [];

    const cardsContainer = document.getElementById("board-cards");
    const emptyBox = document.getElementById("board-empty");

    if (state.listings.length === 0) {
      cardsContainer.innerHTML = "";
      emptyBox.classList.remove("hidden");
    } else {
      emptyBox.classList.add("hidden");
      cardsContainer.innerHTML = state.listings.map(buildListingCardHTML).join("");
    }

    renderBoardMarkers(state.listings);
    loadStats();
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function loadMyActivity() {
  if (!state.user) return;
  try {
    const data = await apiRequest("/api/my");
    state.myListings = data.listings || [];

    const container = document.getElementById("my-cards");
    const emptyBox = document.getElementById("my-empty");

    if (state.myListings.length === 0) {
      container.innerHTML = "";
      emptyBox.classList.remove("hidden");
    } else {
      emptyBox.classList.add("hidden");
      container.innerHTML = state.myListings.map(buildListingCardHTML).join("");
    }
  } catch (err) {
    showToast(err.message, "error");
  }
}

// ---------------------------------------------------------------------------
// Admin Dashboard
// ---------------------------------------------------------------------------
async function loadAdminDashboard() {
  if (!state.user || state.user.role !== "admin") return;

  try {
    const statusFilter = document.getElementById("admin-status-filter").value || "all";
    const [usersData, listingsData] = await Promise.all([
      apiRequest("/api/admin/users"),
      apiRequest(`/api/listings?status=${encodeURIComponent(statusFilter)}`),
    ]);

    const users = usersData.users || [];
    const listings = listingsData.listings || [];

    document.getElementById("admin-users-count").textContent = users.length;
    document.getElementById("admin-listings-count").textContent = listings.length;

    document.getElementById("admin-users-tbody").innerHTML = users
      .map(
        (u) => `
      <tr>
        <td>#${u.id}</td>
        <td><strong>${escapeHtml(u.name)}</strong></td>
        <td><span class="role-badge role-${escapeHtml(u.role)}">${escapeHtml(u.role)}</span></td>
        <td>${escapeHtml(u.org || "—")}</td>
        <td>${escapeHtml(u.email)}</td>
        <td>${escapeHtml(u.phone)}</td>
        <td>${u.listings_posted} posted / ${u.listings_claimed} claimed</td>
        <td>${formatLocalDateTime(u.created_at)}</td>
      </tr>
    `
      )
      .join("");

    document.getElementById("admin-listings-tbody").innerHTML = listings
      .map(
        (l) => `
      <tr>
        <td>#${l.id}</td>
        <td>
          <strong>${escapeHtml(l.title)}</strong><br/>
          <small>${escapeHtml(FOOD_LABELS[l.food_type] || l.food_type)} &bull; ${escapeHtml(
          l.address
        )}</small>
        </td>
        <td>${l.quantity}</td>
        <td>${escapeHtml(l.donor_name)}</td>
        <td>${escapeHtml(l.receiver_name || "—")}</td>
        <td>${formatLocalDateTime(l.pickup_by)}</td>
        <td><span class="status-pill status-${escapeHtml(l.status)}">${escapeHtml(l.status)}</span></td>
        <td>
          <div class="card-action-btns">
            ${
              l.status === "available" || l.status === "claimed"
                ? `<button type="button" class="btn btn-sm btn-outline" data-action="cancel" data-id="${l.id}">Cancel</button>`
                : ""
            }
            <button type="button" class="btn btn-sm btn-chili" data-action="delete" data-id="${l.id}">Delete</button>
          </div>
        </td>
      </tr>
    `
      )
      .join("");
  } catch (err) {
    showToast(err.message, "error");
  }
}

// ---------------------------------------------------------------------------
// Listing Lifecycle Action Handler (Claim, Release, Complete, Cancel, Delete, Edit)
// ---------------------------------------------------------------------------
async function handleListingAction(action, id, extraDataset = {}) {
  if (action === "login-to-claim") {
    openModal("auth-modal");
    return;
  }

  if (action === "focus-map") {
    const lat = parseFloat(extraDataset.lat);
    const lng = parseFloat(extraDataset.lng);
    switchView("board");
    if (state.boardMap && !isNaN(lat) && !isNaN(lng)) {
      state.boardMap.setView([lat, lng], 15);
    }
    return;
  }

  if (action === "edit") {
    const item =
      state.listings.find((x) => x.id === Number(id)) ||
      state.myListings.find((x) => x.id === Number(id));
    if (item) openListingModal(item);
    return;
  }

  try {
    if (action === "claim") {
      await apiRequest(`/api/listings/${id}/claim`, { method: "POST" });
      showToast("Listing claimed! Donor contact details are now unlocked.", "success");
    } else if (action === "release") {
      await apiRequest(`/api/listings/${id}/release`, { method: "POST" });
      showToast("Claim released. The listing is available for others again.");
    } else if (action === "complete") {
      await apiRequest(`/api/listings/${id}/complete`, { method: "POST" });
      showToast("Pickup marked as completed! Thank you for rescuing surplus food.", "success");
    } else if (action === "cancel") {
      await apiRequest(`/api/listings/${id}/cancel`, { method: "POST" });
      showToast("Listing cancelled.");
    } else if (action === "delete") {
      await apiRequest(`/api/listings/${id}`, { method: "DELETE" });
      showToast("Listing deleted.");
    }

    // Refresh active views
    loadBoardListings();
    if (state.currentView === "my") loadMyActivity();
    if (state.currentView === "admin") loadAdminDashboard();
  } catch (err) {
    showToast(err.message, "error");
    loadBoardListings();
  }
}

// ---------------------------------------------------------------------------
// Modals (Auth & Post/Edit Listing)
// ---------------------------------------------------------------------------
function openModal(modalId) {
  document.getElementById(modalId).classList.remove("hidden");
}

function closeModal(modalId) {
  document.getElementById(modalId).classList.add("hidden");
}

function toLocalDatetimeInputValue(dateObj) {
  const pad = (n) => String(n).padStart(2, "0");
  return (
    `${dateObj.getFullYear()}-${pad(dateObj.getMonth() + 1)}-${pad(dateObj.getDate())}` +
    `T${pad(dateObj.getHours())}:${pad(dateObj.getMinutes())}`
  );
}

function openListingModal(listing = null) {
  if (!state.user) {
    openModal("auth-modal");
    return;
  }

  const form = document.getElementById("listing-form");
  form.reset();

  const now = new Date();
  const minDate = new Date(now.getTime() + 10 * 60 * 1000);
  const maxDate = new Date(now.getTime() + 48 * 3600 * 1000);
  const defaultDeadline = new Date(now.getTime() + 4 * 3600 * 1000);

  const pickupInput = document.getElementById("listing-pickup-by");
  pickupInput.min = toLocalDatetimeInputValue(minDate);
  pickupInput.max = toLocalDatetimeInputValue(maxDate);

  if (listing) {
    document.getElementById("listing-modal-title").textContent = "Edit Surplus Listing";
    document.getElementById("listing-submit-btn").textContent = "Save Changes";
    document.getElementById("listing-id").value = listing.id;
    document.getElementById("listing-title").value = listing.title;
    document.getElementById("listing-food-type").value = listing.food_type;
    document.getElementById("listing-quantity").value = listing.quantity;
    document.getElementById("listing-description").value = listing.description || "";
    document.getElementById("listing-address").value = listing.address;
    pickupInput.value = toLocalDatetimeInputValue(new Date(listing.pickup_by));
    openModal("listing-modal");
    initPickerMap(listing.lat, listing.lng);
  } else {
    document.getElementById("listing-modal-title").textContent = "Post Surplus Food";
    document.getElementById("listing-submit-btn").textContent = "Publish Listing";
    document.getElementById("listing-id").value = "";
    pickupInput.value = toLocalDatetimeInputValue(defaultDeadline);
    openModal("listing-modal");
    initPickerMap(13.0674, 80.2376);
  }
}

// ---------------------------------------------------------------------------
// Event Listeners & Initialization
// ---------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", async () => {
  await restoreSession();
  loadStats();
  loadBoardListings();

  // Auto-refresh live listings & stats every 30 seconds
  state.refreshTimer = setInterval(() => {
    loadStats();
    if (state.currentView === "board") loadBoardListings();
    if (state.currentView === "my" && state.user) loadMyActivity();
  }, 30000);

  // Navigation buttons
  document.body.addEventListener("click", (e) => {
    const navTrigger = e.target.closest("[data-nav]");
    if (navTrigger) {
      e.preventDefault();
      switchView(navTrigger.dataset.nav);
      return;
    }

    const closeTrigger = e.target.closest("[data-close-modal]");
    if (closeTrigger) {
      closeModal(closeTrigger.dataset.closeModal);
      return;
    }

    const actionBtn = e.target.closest("[data-action]");
    if (actionBtn) {
      handleListingAction(actionBtn.dataset.action, actionBtn.dataset.id, actionBtn.dataset);
    }
  });

  // Mobile menu toggle
  document.getElementById("mobile-menu-btn").addEventListener("click", () => {
    document.getElementById("main-nav").classList.toggle("open");
  });

  // Auth modal triggers
  document.getElementById("open-auth-btn").addEventListener("click", () => openModal("auth-modal"));
  document.getElementById("logout-btn").addEventListener("click", logout);

  // Hero & Header Post Surplus triggers
  document.getElementById("post-surplus-btn").addEventListener("click", () => openListingModal());
  document.getElementById("my-post-btn").addEventListener("click", () => openListingModal());
  document.getElementById("hero-donate-btn").addEventListener("click", () => {
    if (state.user && (state.user.role === "donor" || state.user.role === "admin")) {
      openListingModal();
    } else {
      openModal("auth-modal");
    }
  });

  // Login / Register tab switching
  const tabLogin = document.getElementById("tab-login");
  const tabRegister = document.getElementById("tab-register");
  const loginForm = document.getElementById("login-form");
  const registerForm = document.getElementById("register-form");

  tabLogin.addEventListener("click", () => {
    tabLogin.classList.add("active");
    tabRegister.classList.remove("active");
    loginForm.classList.remove("hidden");
    registerForm.classList.add("hidden");
  });

  tabRegister.addEventListener("click", () => {
    tabRegister.classList.add("active");
    tabLogin.classList.remove("active");
    registerForm.classList.remove("hidden");
    loginForm.classList.add("hidden");
  });

  // Login Form Submit
  loginForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const email = document.getElementById("login-email").value.trim();
      const password = document.getElementById("login-password").value;
      const data = await apiRequest("/api/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      state.token = data.token;
      state.user = data.user;
      localStorage.setItem("surpluslink_token", data.token);
      updateAuthUI();
      closeModal("auth-modal");
      showToast(`Welcome back, ${data.user.name}!`, "success");
      loadBoardListings();
    } catch (err) {
      showToast(err.message, "error");
    }
  });

  // Register Form Submit
  registerForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const role = document.querySelector('input[name="reg-role"]:checked').value;
      const payload = {
        name: document.getElementById("reg-name").value.trim(),
        org: document.getElementById("reg-org").value.trim(),
        email: document.getElementById("reg-email").value.trim(),
        phone: document.getElementById("reg-phone").value.trim(),
        password: document.getElementById("reg-password").value,
        role,
      };
      const data = await apiRequest("/api/register", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      state.token = data.token;
      state.user = data.user;
      localStorage.setItem("surpluslink_token", data.token);
      updateAuthUI();
      closeModal("auth-modal");
      showToast(`Account created! Welcome, ${data.user.name}.`, "success");
      switchView("board");
    } catch (err) {
      showToast(err.message, "error");
    }
  });

  // Create / Update Listing Form Submit
  document.getElementById("listing-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const id = document.getElementById("listing-id").value;
    const pickupLocal = document.getElementById("listing-pickup-by").value;
    const pickupIso = pickupLocal ? new Date(pickupLocal).toISOString() : "";

    const payload = {
      title: document.getElementById("listing-title").value.trim(),
      food_type: document.getElementById("listing-food-type").value,
      quantity: parseInt(document.getElementById("listing-quantity").value, 10),
      description: document.getElementById("listing-description").value.trim(),
      address: document.getElementById("listing-address").value.trim(),
      pickup_by: pickupIso,
      lat: parseFloat(document.getElementById("listing-lat").value),
      lng: parseFloat(document.getElementById("listing-lng").value),
    };

    try {
      if (id) {
        await apiRequest(`/api/listings/${id}`, {
          method: "PUT",
          body: JSON.stringify(payload),
        });
        showToast("Listing updated!", "success");
      } else {
        await apiRequest("/api/listings", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        showToast("Surplus food listing published!", "success");
      }
      closeModal("listing-modal");
      loadBoardListings();
      if (state.currentView === "my") loadMyActivity();
    } catch (err) {
      showToast(err.message, "error");
    }
  });

  // "Use My Location" inside Post/Edit Modal
  document.getElementById("use-my-location-btn").addEventListener("click", () => {
    if (!navigator.geolocation) {
      showToast("Geolocation is not supported by your browser.", "error");
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        const { latitude, longitude } = pos.coords;
        setPickerCoords(latitude, longitude);
        if (state.pickerMap) state.pickerMap.setView([latitude, longitude], 15);
        showToast("Pickup pin updated to your current location!", "success");
      },
      () => showToast("Could not retrieve your location. Please tap the map manually.", "error")
    );
  });

  // Live Board Filter Controls
  document.getElementById("filter-q").addEventListener("input", loadBoardListings);
  document.getElementById("filter-food-type").addEventListener("change", loadBoardListings);
  document.getElementById("filter-status").addEventListener("change", loadBoardListings);
  document.getElementById("board-refresh-btn").addEventListener("click", loadBoardListings);

  // "Sort by My Distance" on Live Board
  document.getElementById("board-locate-btn").addEventListener("click", () => {
    if (!navigator.geolocation) {
      showToast("Geolocation is not supported by your browser.", "error");
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        state.userCoords = {
          lat: pos.coords.latitude,
          lng: pos.coords.longitude,
        };
        showToast("Listings sorted by distance from your location!", "success");
        loadBoardListings();
      },
      () => showToast("Location permission denied. Showing default urgency order.", "error")
    );
  });

  // Admin Controls
  document.getElementById("admin-refresh-btn").addEventListener("click", loadAdminDashboard);
  document.getElementById("admin-status-filter").addEventListener("change", loadAdminDashboard);
});