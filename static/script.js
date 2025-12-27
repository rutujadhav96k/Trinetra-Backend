// ==========================================
// 1. MAP INITIALIZATION & LAYERS
// ==========================================
const map = L.map('map', {
    center: [19.1383, 77.3210],
    zoom: 14,
    zoomControl: false,
    attributionControl: false
});

const layers = {
    dark: L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png'),
    satellite: L.tileLayer('http://{s}.google.com/vt/lyrs=s,h&x={x}&y={y}&z={z}', { subdomains: ['mt0', 'mt1', 'mt2', 'mt3'] }),
    light: L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png')
};

// --- FIX: LOAD SAVED THEME ON STARTUP ---
// Check if user has a saved preference, otherwise default to 'dark'
const savedMode = localStorage.getItem('trinetra_map_mode') || 'dark';

// Apply it immediately (This function handles adding the layer, so we don't need to add it manually again)
setMapMode(savedMode);

// layers.dark.addTo(map);  <-- REMOVE THIS LINE completely
let droneMarkers = {};
let officerMarkers = {};
let drones = {};
let officers = {};
let selectedDroneId = null;
let selectedOfficerId = null;
let currentFilter = 'all';
let socket;

// Control Modes
let isGotoMode = false;
let isEmergencyMode = false;
let emergencyLayer = L.layerGroup().addTo(map);

// Sample Officer Data
const sampleOfficers = [

];

let dataSocket;
let videoSocket;
let lastFrameUrl = null;

function connectWebSocket() {
    // Use current host (works with both localhost and ngrok)
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host;

    // 1. DATA SOCKET
    const dataUrl = `${protocol}//${host}/ws/locations`;
    console.log('Connecting to WebSocket:', dataUrl);

    dataSocket = new WebSocket(dataUrl);

    dataSocket.onopen = () => {
        console.log('WebSocket connected successfully');
    };

    dataSocket.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            console.log('Received WebSocket data:', data);

            if (data.type === 'snapshot') {
                // Initial snapshot with both drones and officers
                if (data.drones) {
                    data.drones.forEach(drone => handleDroneUpdate(drone));
                }
                if (data.officers) {
                    data.officers.forEach(officer => handleOfficerUpdate(officer));
                }
            } else if (data.type === 'location_update' || data.type === 'status') {
                handleDroneUpdate(data);
            } else if (data.type === 'officer_location_update' || data.type === 'officer_status') {
                handleOfficerUpdate(data);
            }
        } catch (e) {
            console.error('Error parsing WebSocket message:', e);
        }
    };

    dataSocket.onerror = (error) => {
        console.error('WebSocket error:', error);
    };

    dataSocket.onclose = () => {
        console.log('WebSocket closed, reconnecting in 3s...');
        setTimeout(connectWebSocket, 3000);
    };

    // 2. VIDEO SOCKET
    const videoUrl = `${protocol}//${host}/ws/video/feed`;
    videoSocket = new WebSocket(videoUrl);
    videoSocket.binaryType = "blob";

    videoSocket.onmessage = (event) => {
        if (lastFrameUrl) {
            URL.revokeObjectURL(lastFrameUrl);
        }
        const url = URL.createObjectURL(event.data);
        lastFrameUrl = url;

        if (selectedDroneId) {
            const thumb = document.getElementById('drone-feed');
            const placeholder = document.querySelector('.no-signal-placeholder');

            if (thumb) {
                thumb.src = url;
                thumb.style.display = 'block';
                if (placeholder) placeholder.style.display = 'none';
            }
        }

        const modal = document.getElementById('video-modal');
        if (modal && !modal.classList.contains('hidden')) {
            const full = document.getElementById('full-drone-feed');
            if (full) full.src = url;
        }
    };
}

// ==========================================
// 4. MAP MARKER LOGIC
// ==========================================
function handleDroneUpdate(data) {
    const { drone_id, lat, lng } = data;
    drones[drone_id] = data;

    if (droneMarkers[drone_id]) {
        droneMarkers[drone_id].setLatLng([lat, lng]);
    } else {
        const icon = L.divIcon({
            className: 'tactical-marker',
            html: `
               <div class="drone-marker-inner">
                <svg width="48" height="48" viewBox="0 0 200 200" xmlns="http://www.w3.org/2000/svg">
                  <ellipse cx="100" cy="170" rx="55" ry="10" fill="rgba(0,0,0,0.2)" />
                  <rect x="25" y="75" width="70" height="16" rx="8" fill="#ff6f61"/>
                  <circle cx="30" cy="83" r="14" fill="#ff6f61"/>
                  <rect x="105" y="75" width="70" height="16" rx="8" fill="#ff6f61"/>
                  <circle cx="170" cy="83" r="14" fill="#ff6f61"/>
                  <ellipse cx="30" cy="70" rx="26" ry="4" fill="#1c1c1c"/>
                  <circle cx="30" cy="83" r="6" fill="#ffffff"/>
                  <ellipse cx="170" cy="70" rx="26" ry="4" fill="#1c1c1c"/>
                  <circle cx="170" cy="83" r="6" fill="#ffffff"/>
                  <rect x="65" y="55" width="70" height="45" rx="18" fill="#ff6f61"/>
                  <rect x="75" y="60" width="50" height="30" rx="12" fill="#ffffff"/>
                  <rect x="80" y="100" width="40" height="35" rx="8" fill="#ff6f61"/>
                  <rect x="88" y="108" width="24" height="24" rx="4" fill="#ffffff"/>
                  <circle cx="100" cy="120" r="8" fill="#000000"/>
                  <circle cx="102" cy="118" r="3" fill="#4fc3f7"/>
                  <path d="M78 135 Q75 155 70 165" stroke="#ff6f61" stroke-width="6" fill="none"/>
                  <path d="M122 135 Q125 155 130 165" stroke="#ff6f61" stroke-width="6" fill="none"/>
                </svg>
                <span class="marker-label">${drone_id}</span>
                </div>`,
            iconSize: [50, 50],
            iconAnchor: [25, 25]
        });

        droneMarkers[drone_id] = L.marker([lat, lng], { icon }).on('click', () => selectDrone(drone_id));
        if (currentFilter === 'all' || currentFilter === 'drones') droneMarkers[drone_id].addTo(map);
    }

    if (selectedDroneId === drone_id) updateCommandPanel(data);
}

// Handle Officer Location Updates
function handleOfficerUpdate(data) {
    console.log('🚔 OFFICER UPDATE RECEIVED:', data);

    let { officer_id, lat, lng, officer_name, badge_number, is_online } = data;

    // FIX: Extract lat/lng from last_location if not at root level (snapshot data)
    if ((!lat || !lng) && data.last_location) {
        console.log('📦 Extracting location from last_location object');
        console.log('📦 last_location content:', data.last_location);
        lat = data.last_location.lat;
        lng = data.last_location.lng;
        console.log('📦 Extracted lat:', lat, 'lng:', lng);
    }

    // Skip if no location data
    if (!lat || !lng) {
        console.warn('⚠️ No location data for officer:', officer_id);
        console.warn('⚠️ lat:', lat, 'lng:', lng);
        console.warn('⚠️ Full data:', data);
        return;
    }

    console.log(`📍 Officer ${officer_id} at ${lat}, ${lng}`);

    // 1. FIX: Merge data instead of overwriting (Purana data bacha rahega)
    if (officers[officer_id]) {
        // Sirf location aur status update karo, baki details waise hi rakho
        officers[officer_id].lat = lat;
        officers[officer_id].lng = lng;
        officers[officer_id].is_online = is_online;
        officers[officer_id].last_seen = data.timestamp || data.last_seen; // Update timestamp

        // Agar naye packet me name/badge hai to update karo, warna purana rehne do
        if (officer_name) officers[officer_id].officer_name = officer_name;
        if (badge_number) officers[officer_id].badge_number = badge_number;
    } else {
        // Naya officer hai to pura data store karo
        console.log(`✨ New officer detected: ${officer_id}`);
        officers[officer_id] = {
            ...data,
            lat: lat,  // Ensure lat/lng are at root level
            lng: lng
        };
    }

    // 2. Map Marker Update Logic
    if (officerMarkers[officer_id]) {
        console.log(`🔄 Updating existing marker for ${officer_id}`);
        officerMarkers[officer_id].setLatLng([lat, lng]);

        // Optional: Pulse effect update
        const el = officerMarkers[officer_id].getElement();
        if (el) {
            const statusColor = is_online ? '#00ff88' : '#ff3131';
            el.querySelector('.officer-circle').style.borderColor = is_online ? '#000' : '#ff3131';
        }

    } else {
        // Create new marker logic (Same as before)
        console.log(`➕ Creating new marker for ${officer_id}`);
        const initials = officer_name ? officer_name.split(' ').map(n => n[0]).join('').substring(0, 2).toUpperCase() : 'OF';

        const icon = L.divIcon({
            className: 'tactical-marker',
            html: `
                <div class="officer-marker-compact">
                    <div class="officer-circle" style="background: ${is_online ? 'linear-gradient(135deg, #00ff88 0%, #00d4ff 100%)' : '#555'}">
                        <span class="officer-initials">${initials}</span>
                    </div>
                    <div class="officer-pulse" style="border-color: ${is_online ? '#00ff88' : '#ff3131'}"></div>
                </div>`,
            iconSize: [20, 20],
            iconAnchor: [10, 10]
        });

        officerMarkers[officer_id] = L.marker([lat, lng], { icon })
            .on('click', () => selectOfficer(officer_id));

        if (currentFilter === 'all' || currentFilter === 'officers') {
            officerMarkers[officer_id].addTo(map);
            console.log(`✅ Marker added to map for ${officer_id}`);
        }
    }

    // 3. Panel Update: Sirf Location Text Update karo (Photo reload mat karo)
    if (selectedOfficerId === officer_id) {
        document.getElementById('officer-lat').innerText = lat ? lat.toFixed(6) : '--';
        document.getElementById('officer-lng').innerText = lng ? lng.toFixed(6) : '--';

        const statusEl = document.getElementById('officer-status');
        statusEl.innerText = is_online ? 'ONLINE' : 'OFFLINE';
        statusEl.style.color = is_online ? '#00ff88' : '#ff3131';
    }

    console.log(`✅ Officer ${officer_id} update complete`);
}

function initOfficers() {
    sampleOfficers.forEach(off => {
        const icon = L.divIcon({
            className: 'tactical-marker',
            html: `<div class="drone-marker-inner">
                    <span class="material-icons-round" style="color:#00f2ff; font-size:30px;">person_pin</span>
                    <span class="marker-label">${off.name}</span>
                   </div>`,
            iconSize: [50, 50], iconAnchor: [25, 25]
        });
        officerMarkers[off.id] = L.marker([off.lat, off.lng], { icon });
        if (currentFilter === 'all' || currentFilter === 'officers') officerMarkers[off.id].addTo(map);
    });
}

function filterMap(mode) {
    currentFilter = mode;
    document.querySelectorAll('.nav-btn').forEach(btn => btn.classList.remove('active'));
    document.getElementById(`nav-${mode}`).classList.add('active');

    Object.keys(droneMarkers).forEach(id => {
        if (mode === 'all' || mode === 'drones') map.addLayer(droneMarkers[id]);
        else map.removeLayer(droneMarkers[id]);
    });

    Object.keys(officerMarkers).forEach(id => {
        if (mode === 'all' || mode === 'officers') map.addLayer(officerMarkers[id]);
        else map.removeLayer(officerMarkers[id]);
    });
}

// ==========================================
// 5. SELECTION & COMMAND PANEL
// ==========================================
function selectDrone(id) {
    // Close officer panel if open
    deselectOfficer();

    selectedDroneId = id;
    document.getElementById('command-panel').classList.remove('hidden');
    if (droneMarkers[id]) map.flyTo(droneMarkers[id].getLatLng(), 18);
    isGotoMode = false;
    document.getElementById('btn-goto').classList.remove('active');
    document.getElementById('goto-instruction').classList.add('hidden');
    if (drones[id]) updateCommandPanel(drones[id]);
}

function deselectDrone() {
    selectedDroneId = null;
    document.getElementById('command-panel').classList.add('hidden');
    isGotoMode = false;
    document.getElementById('map').style.cursor = 'default';
}

async function selectOfficer(id) {
    // Close drone panel if open
    if (selectedDroneId) {
        selectedDroneId = null;
        document.getElementById('command-panel').classList.add('hidden');
    }

    selectedOfficerId = id;
    document.getElementById('officer-panel').classList.remove('hidden');

    // Zoom to officer
    if (officerMarkers[id]) map.flyTo(officerMarkers[id].getLatLng(), 18);

    // Show Loading State inside panel initially
    document.getElementById('officer-name').innerText = "FETCHING DATA...";
    document.getElementById('officer-photo').src = ""; // Clear old photo
    document.querySelector('.no-photo-placeholder').style.display = 'flex';

    try {
        // Fetch REAL officer data from backend
        const response = await fetch(`/api/officer/${id}/details`);

        if (response.ok) {
            const officerData = await response.json();

            // CRITICAL FIX: Merge fetched details into the global officers object
            // Isse ye hoga ki live location aane par ye details delete nahi hongi
            officers[id] = { ...officers[id], ...officerData };

            updateOfficerPanel(officers[id]);
        } else {
            console.warn(`API error, using fallback data`);
            if (officers[id]) updateOfficerPanel(officers[id]);
        }
    } catch (error) {
        console.error('Failed to fetch officer details:', error);
        // Fallback to whatever data we have
        if (officers[id]) updateOfficerPanel(officers[id]);
    }
}

function deselectOfficer() {
    selectedOfficerId = null;
    document.getElementById('officer-panel').classList.add('hidden');
}

function updateOfficerPanel(data) {
    const {
        officer_id, officer_name, badge_number, rank,
        station_name, district, mobile_number,
        lat, lng, is_online, photo_path, last_seen
    } = data;

    document.getElementById('officer-name').innerText = officer_name || "UNKNOWN OFFICER";
    document.getElementById('officer-status').innerText = is_online ? 'ONLINE' : 'OFFLINE';
    document.getElementById('officer-status').style.color = is_online ? '#00ff88' : '#ff3131';

    document.getElementById('officer-badge').innerText = badge_number || 'N/A';
    document.getElementById('officer-rank').innerText = rank || 'N/A';
    document.getElementById('officer-station').innerText = station_name || 'N/A';
    document.getElementById('officer-district').innerText = district || 'N/A';
    document.getElementById('officer-mobile').innerText = mobile_number || 'N/A';

    // REMOVED LAT/LNG updates
    // Added Time Update
    if (document.getElementById('officer-last-seen')) {
        const timeStr = last_seen ? new Date(last_seen).toLocaleTimeString() : 'Unknown';
        document.getElementById('officer-last-seen').innerText = timeStr;
    }

    // Photo Logic (Keep this robust)
    const photoEl = document.getElementById('officer-photo');
    const placeholder = document.querySelector('.no-photo-placeholder');

    if (photo_path && photo_path.length > 5) {
        const src = photo_path.startsWith('http') || photo_path.startsWith('/')
            ? photo_path
            : `/${photo_path}`;
        photoEl.src = src;
        photoEl.style.display = 'block';
        if (placeholder) placeholder.style.display = 'none';

        photoEl.onerror = function () {
            this.style.display = 'none';
            if (placeholder) placeholder.style.display = 'flex';
        };
    } else {
        photoEl.style.display = 'none';
        if (placeholder) placeholder.style.display = 'flex';
    }
}

function updateCommandPanel(data) {
    document.getElementById('cmd-drone-id').innerText = data.drone_id;
    document.getElementById('cmd-lat').innerText = data.lat.toFixed(6);
    document.getElementById('cmd-lng').innerText = data.lng.toFixed(6);

    const batt = data.battery || 0;
    const bar = document.getElementById('cmd-batt-bar');
    if (bar) {
        bar.style.width = batt + '%';
        if (batt < 20) bar.style.backgroundColor = 'var(--emergency-red)';
        else if (batt < 50) bar.style.backgroundColor = '#ffbd21';
        else bar.style.backgroundColor = 'var(--accent-cyan)';
    }
}

// ==========================================
// 6. COMMAND FUNCTIONS (RTL, LAND, GOTO)
// ==========================================
function sendEmergencyCommand(command) {
    if (!selectedDroneId || !socket || socket.readyState !== WebSocket.OPEN) return;
    socket.send(JSON.stringify({
        drone_id: selectedDroneId,
        action: command,
        timestamp: Date.now()
    }));
}

function toggleGotoMode() {
    if (!selectedDroneId) return;
    isGotoMode = !isGotoMode;
    const btn = document.getElementById('btn-goto');
    const instr = document.getElementById('goto-instruction');

    if (isGotoMode) {
        btn.classList.add('active');
        btn.innerHTML = '<span class="material-icons-round">gps_fixed</span> <span>WAITING FOR TARGET...</span>';
        instr.classList.remove('hidden');
        document.getElementById('map').style.cursor = 'crosshair';
    } else {
        btn.classList.remove('active');
        btn.innerHTML = '<span class="material-icons-round">location_searching</span> <span>SET TARGET LOCATION</span>';
        instr.classList.add('hidden');
        document.getElementById('map').style.cursor = 'default';
    }
}

// ==========================================
// 7. EMERGENCY DISPATCH MODE
// ==========================================
function toggleEmergencyMode() {
    isEmergencyMode = !isEmergencyMode;
    const btn = document.getElementById('btn-emergency');
    if (isEmergencyMode) {
        btn.classList.add('active');
        document.getElementById('map').style.cursor = 'crosshair';
    } else {
        btn.classList.remove('active');
        document.getElementById('map').style.cursor = 'default';
    }
}

function handleEmergencyDispatch(targetLatLng) {
    let nearestDrone = null;
    let minDistance = Infinity;

    Object.values(drones).forEach(drone => {
        const dist = map.distance([drone.lat, drone.lng], targetLatLng);
        if (dist < minDistance) {
            minDistance = dist;
            nearestDrone = drone;
        }
    });

    if (nearestDrone) {
        emergencyLayer.clearLayers();
        const targetIcon = L.divIcon({
            className: 'custom-div-icon',
            html: `<div class="target-marker-icon"><span class="material-icons-round">crisis_alert</span></div>`,
            iconSize: [40, 40],
            iconAnchor: [20, 20]
        });
        L.marker(targetLatLng, { icon: targetIcon }).addTo(emergencyLayer);

        const line = L.polyline([
            [nearestDrone.lat, nearestDrone.lng],
            [targetLatLng.lat, targetLatLng.lng]
        ], {
            className: 'emergency-path-line'
        }).addTo(emergencyLayer);

        if (socket && socket.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({
                drone_id: nearestDrone.drone_id,
                action: "EMERGENCY_GOTO",
                target_lat: targetLatLng.lat,
                target_lng: targetLatLng.lng
            }));
            map.fitBounds(line.getBounds(), { padding: [80, 80] });
        }
    } else {
        alert("NO ACTIVE DRONES AVAILABLE FOR DISPATCH");
    }
}

map.on('click', (e) => {
    if (isGotoMode && selectedDroneId && socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({
            drone_id: selectedDroneId,
            action: "GOTO",
            target_lat: e.latlng.lat,
            target_lng: e.latlng.lng
        }));
        L.popup().setLatLng(e.latlng).setContent("Coordinates Transmitted").openOn(map);
        toggleGotoMode();
        return;
    }
    if (isEmergencyMode) {
        handleEmergencyDispatch(e.latlng);
        toggleEmergencyMode();
    }
});

// ==========================================
// 9. VIDEO MODAL LOGIC
// ==========================================
function openVideoModal() {
    if (!selectedDroneId) return;
    const modal = document.getElementById('video-modal');
    const fullFeed = document.getElementById('full-drone-feed');
    const thumbnailFeed = document.getElementById('drone-feed');
    const modalTitle = document.getElementById('modal-drone-id');

    fullFeed.src = thumbnailFeed.src;
    modalTitle.innerText = selectedDroneId;
    modal.classList.remove('hidden');
}

function closeVideoModal() {
    document.getElementById('video-modal').classList.add('hidden');
}

document.addEventListener('keydown', (e) => {
    if (e.key === "Escape") closeVideoModal();
});

function setMapMode(mode) {
    // 1. Remove all potential layers
    if (map.hasLayer(layers.dark)) map.removeLayer(layers.dark);
    if (map.hasLayer(layers.satellite)) map.removeLayer(layers.satellite);
    if (map.hasLayer(layers.light)) map.removeLayer(layers.light);

    // 2. Add the selected layer
    if (layers[mode]) {
        layers[mode].addTo(map);
    }

    // 3. Update Body Classes for UI styling
    document.body.classList.remove('light-mode', 'satellite-mode');
    if (mode === 'light') {
        document.body.classList.add('light-mode');
    } else if (mode === 'satellite') {
        document.body.classList.add('satellite-mode');
    }

    // 4. SAVE TO STORAGE (The Fix)
    localStorage.setItem('trinetra_map_mode', mode);
}

function toggleUserMenu(event) {
    if (event) event.stopPropagation();
    document.getElementById('user-menu').classList.toggle('hidden');
}

window.addEventListener('click', function (e) {
    const menu = document.getElementById('user-menu');
    const btn = document.getElementById('profile-btn');
    if (!menu.classList.contains('hidden') && !menu.contains(e.target) && !btn.contains(e.target)) {
        menu.classList.add('hidden');
    }
});

setInterval(() => {
    document.getElementById('clock').innerText = new Date().toLocaleTimeString('en-US', { hour12: false });
}, 1000);

// ==========================================
// 11. REGISTRATION MANAGEMENT (NEW)
// ==========================================
let currentDossierId = null;
let allRequestsData = [];

setInterval(() => {
    pollRequests();
}, 10000);

async function pollRequests() {
    try {
        const response = await fetch('/api/admin/requests');
        const data = await response.json();
        const badge = document.getElementById('request-badge');
        if (data.requests.length > 0) {
            badge.innerText = data.requests.length;
            badge.classList.remove('hidden');
        } else {
            badge.classList.add('hidden');
        }
        allRequestsData = data.requests;
    } catch (e) { }
}

async function toggleAccessRequests() {
    const modal = document.getElementById('requests-modal');
    modal.classList.toggle('hidden');
    if (!modal.classList.contains('hidden')) {
        await fetchAndRenderRequests();
    }
}

async function fetchAndRenderRequests() {
    const grid = document.getElementById('requests-grid');
    // Using a sleek loading text
    grid.innerHTML = '<div style="color:#666; padding:10px; font-family:Rajdhani; text-align:center;">SCANNING NETWORK FOR REQUESTS...</div>';

    try {
        const response = await fetch('/api/admin/requests');
        const data = await response.json();
        allRequestsData = data.requests;

        grid.innerHTML = '';

        if (data.requests.length === 0) {
            grid.innerHTML = '<div style="color:#444; padding:20px; text-align:center; font-family:Rajdhani;">// NO PENDING TRANSMISSIONS</div>';
            return;
        }

        data.requests.forEach(req => {
            const card = document.createElement('div');
            card.className = 'request-card'; // This class now triggers the horizontal row style

            // Fallback image logic
            const photoUrl = req.photo_path ? `/${req.photo_path}` : 'static/placeholder_cop.png';

            // In script.js inside fetchAndRenderRequests()
            card.innerHTML = `
    <img src="${photoUrl}" class="req-avatar-small">
    
    <div class="req-info">
        <h4>${req.full_name}</h4>
        <span>${req.rank} // BADGE: ${req.badge_number}</span>
    </div>
    
    <button class="view-btn" onclick="openDossier('${req.request_id}')">
        REVIEW
    </button>
`;
            grid.appendChild(card);
        });
    } catch (e) {
        grid.innerHTML = '<div style="color:#ff3131; text-align:center; padding:10px;">CONNECTION ERROR // RETRYING</div>';
    }
}
function openDossier(requestId) {
    const req = allRequestsData.find(r => r.request_id === requestId);
    if (!req) return;

    currentDossierId = requestId;

    document.getElementById('dossier-id').innerText = req.service_id;
    document.getElementById('dossier-photo').src = req.photo_path ? `/${req.photo_path}` : '';
    document.getElementById('dossier-name').innerText = req.full_name;
    document.getElementById('dossier-rank').innerText = req.rank.toUpperCase();
    document.getElementById('dossier-badge').innerText = `BADGE: ${req.badge_number}`;

    document.getElementById('dossier-station').innerText = req.station_name;
    document.getElementById('dossier-district').innerText = req.district;
    document.getElementById('dossier-email').innerText = req.official_email;
    document.getElementById('dossier-mobile').innerText = req.mobile_number;

    document.getElementById('link-idcard').href = `/${req.id_card_path}`;
    document.getElementById('link-pdf').href = `/${req.pdf_path}`;

    document.getElementById('dossier-modal').classList.remove('hidden');
}

function closeDossier() {
    document.getElementById('dossier-modal').classList.add('hidden');
    currentDossierId = null;
}

// --- CUSTOM CONFIRMATION MODAL ---
function openConfirmModal() {
    document.getElementById('confirm-modal').classList.remove('hidden');
}

function closeConfirmModal() {
    document.getElementById('confirm-modal').classList.add('hidden');
}

async function executeApproval() {
    if (!currentDossierId) return;

    const confirmBtn = document.querySelector('.tactical-btn.confirm');
    confirmBtn.innerText = "AUTHORIZING...";

    try {
        const response = await fetch(`/api/admin/approve/${currentDossierId}`, { method: 'POST' });
        const data = await response.json();

        if (data.status === 'approved') {
            closeConfirmModal();
            closeDossier();
            showNotification(`OFFICER ${data.officer_id} AUTHORIZED`, 'success');
            await fetchAndRenderRequests();
        }
    } catch (e) {
        showNotification('Authorization Failed', 'error');
    } finally {
        confirmBtn.innerText = "GRANT ACCESS";
    }
}

function showNotification(msg, type) {
    const div = document.createElement('div');
    div.style.position = 'fixed';
    div.style.bottom = '20px';
    div.style.left = '50%';
    div.style.transform = 'translateX(-50%)';
    div.style.background = type === 'success' ? '#00f2ff' : '#ff3131';
    div.style.color = 'black';
    div.style.padding = '10px 20px';
    div.style.borderRadius = '4px';
    div.style.fontWeight = 'bold';
    div.style.fontFamily = 'Rajdhani, sans-serif';
    div.style.zIndex = '20000';
    div.style.boxShadow = '0 0 20px rgba(0,0,0,0.5)';
    div.innerText = msg;
    document.body.appendChild(div);

    setTimeout(() => div.remove(), 3000);
}

// Initialize
initOfficers();
connectWebSocket();