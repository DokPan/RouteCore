let map = null;
let splitMap = null;
let routeLayers = [];
let markerLayers = [];
let scMarkers = [];
let splitRouteLayers = [];
let splitMarkerLayers = [];
let currentPlanData = null;
let contextData = null;
let activeInspectorOrderId = null;
let activeSheetStaffId = 'ST-001';
let activeSplitStaffId = 'all';
let activeSplitCenterId = 'all';
let currentActiveView = 'split';
let currentShiftMinutes = 630;
let offlineStreetsGeoJsonPromise = null;
let currentValidationData = null;
let routeSheetReqSeq = 0;
let engineerMapReqSeq = 0;
const selectedUnassignedIds = new Set();
let orderBoardItems = [];
let orderBoardCategory = 'ALL';
let pickingOrderLocation = false;
let pickedLocationMarker = null;

function getActivePlanDate() {
  if (currentPlanData && currentPlanData.date) return currentPlanData.date;
  if (contextData && contextData.active_date) return contextData.active_date;
  return new Date().toISOString().slice(0, 10);
}

function isOrderConfirmedTomorrow(orderId) {
  if (!orderId || !currentPlanData || !currentPlanData.unassigned) return false;
  const uMatch = currentPlanData.unassigned.find(u => u.order_id === orderId);
  return Boolean(
    uMatch && (
      uMatch.suggested_action === 'CONFIRMED_FOR_TOMORROW' ||
      (uMatch.reason_text && uMatch.reason_text.includes(RU_TEXT.T_0103))
    )
  );
}

function getUnavailableTooltipText(route) {
  if (!route) return '';
  const reasonStr = (route.unavailable_reason != null && String(route.unavailable_reason).trim() !== '')
    ? String(route.unavailable_reason).trim()
    : (route.explanation || RU_TEXT.T_0223);
  const rawComment = route.unavailable_comment != null ? String(route.unavailable_comment).trim() : '';
  const commentStr = rawComment !== '' ? rawComment : '-';
  return `${RU_TEXT.T_0229}\n${RU_TEXT.T_0230}${reasonStr}\n${RU_TEXT.T_0231}${commentStr}`;
}

function getTimezoneOffsetSuffix() {
  const sample = (currentPlanData && currentPlanData.as_of) || '';
  const match = String(sample).match(/([+-]\d{2}:\d{2})$/);
  return match ? match[1] : '+05:00';
}

function formatMinutesToHHMM(totalMin) {
  const clamped = Math.max(0, Math.min(23 * 60 + 59, Math.round(totalMin)));
  const hh = String(Math.floor(clamped / 60)).padStart(2, '0');
  const mm = String(clamped % 60).padStart(2, '0');
  return `${hh}:${mm}`;
}

function getCurrentEventTimestamp(overrideHHMM = null) {
  const dateStr = getActivePlanDate();
  const hhmm = overrideHHMM || formatMinutesToHHMM(currentShiftMinutes);
  const candidate = `${dateStr}T${hhmm}:00${getTimezoneOffsetSuffix()}`;
  const asOf = currentPlanData && currentPlanData.as_of;
  if (asOf) {
    const candMs = Date.parse(candidate);
    const asOfMs = Date.parse(asOf);
    if (!Number.isNaN(candMs) && !Number.isNaN(asOfMs) && candMs < asOfMs) {
      return asOf;
    }
  }
  return candidate;
}

function getNextWorkdayIso() {
  if (contextData && contextData.next_workday) return contextData.next_workday;
  const parts = getActivePlanDate().split('-').map(Number);
  const dt = new Date(Date.UTC(parts[0], (parts[1] || 1) - 1, parts[2] || 1));
  dt.setUTCDate(dt.getUTCDate() + 1);
  while (dt.getUTCDay() === 0 || dt.getUTCDay() === 6) dt.setUTCDate(dt.getUTCDate() + 1);
  return dt.toISOString().slice(0, 10);
}

function getNextWorkdayLabel() {
  if (contextData && contextData.next_workday_ru) return contextData.next_workday_ru;
  const [y, m, d] = getNextWorkdayIso().split('-');
  return `${d}.${m}.${y}`;
}

function getNextWorkdayShortLabel() {
  return getNextWorkdayLabel().slice(0, 5);
}

function isStopCompleted(stop) {
  return Boolean(stop && (stop.status === 'COMPLETED' || stop.status === 'DONE'));
}

function attachOfflineStreetsLayer(mapInstance) {
  if (!mapInstance || typeof L === 'undefined') return;
  const container = mapInstance.getContainer();
  if (container) {
    container.style.background = '#f1f5f9';
  }
  if (!mapInstance.getPane('street-base')) {
    const pane = mapInstance.createPane('street-base');
    pane.style.zIndex = 200;
  }
  if (mapInstance._offlineStreetsAttached) return;
  mapInstance._offlineStreetsAttached = true;

  if (!offlineStreetsGeoJsonPromise) {
    offlineStreetsGeoJsonPromise = fetch('/assets/ekaterinburg-streets.geojson')
      .then(r => (r.ok ? r.json() : null))
      .catch(() => null);
  }
  offlineStreetsGeoJsonPromise.then(geo => {
    if (!geo || !mapInstance) return;
    L.geoJSON(geo, {
      pane: 'street-base',
      style: (feature) => {
        const cls = (feature && feature.properties && feature.properties.class) || '';
        const isMajor = cls === 'primary' || cls === 'trunk' || cls === 'secondary';
        return {
          color: isMajor ? '#cbd5e1' : '#e2e8f0',
          weight: isMajor ? 2.2 : 1.2,
          opacity: 0.9
        };
      }
    }).addTo(mapInstance);
  });
}

function performUndo(token) {
  if (!token) return;
  fetch(`/api/v1/ui/undo/${encodeURIComponent(token)}`, { method: 'POST' })
    .then(async res => {
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      return data;
    })
    .then(() => {
      showToast(RU_TEXT.T_0001, RU_TEXT.T_0002, 'info', 3000);
      loadAllData();
      if (activeInspectorOrderId) {
        setTimeout(() => openInspector(activeInspectorOrderId), 250);
      }
      if (currentActiveView === 'sheet') {
        setTimeout(() => loadRouteSheetData(activeSheetStaffId), 250);
      }
    })
    .catch(err => showToast(RU_TEXT.T_0003, String(err.message || err), 'error'));
}

function renderDispatcherValidationDetails(data) {
  const panel = document.getElementById('dispatcher-plan-changes');
  if (!panel) return;
  const violations = Array.isArray(data && data.violations) ? data.violations : [];
  const changes = Array.isArray(data && data.changes) ? data.changes : [];

  const status = document.createElement('p');
  status.className = `dispatcher-validation-status ${violations.length === 0 ? 'ok' : 'invalid'}`;
  status.textContent = `Проверка H1–H10. Нарушений: ${violations.length}. Визитов в слоте: ${data && data.in_slot != null ? data.in_slot : 0}.`;
  panel.replaceChildren(status);

  if (violations.length > 0) {
    const violationsBox = document.createElement('details');
    violationsBox.open = true;
    const caption = document.createElement('summary');
    caption.textContent = `Причины нарушений (${violations.length})`;
    violationsBox.append(caption);
    for (const item of violations) {
      const row = document.createElement('p');
      if (typeof item === 'string') {
        row.textContent = item;
      } else {
        const extra = item.rule === 'H6' ? 'Нарушена последовательность остановок или не хватает времени на дорогу' : '';
        row.textContent = [item.rule, item.staff_id, item.order_id, extra].filter(Boolean).join(' / ');
      }
      violationsBox.append(row);
    }
    panel.append(violationsBox);
  }

  if (changes.length > 0) {
    const details = document.createElement('details');
    const title = document.createElement('summary');
    title.textContent = `Изменения в визитах (${changes.length})`;
    details.append(title);
    const visitLabel = visit => {
      if (!visit) return 'без визита';
      const staffName = (contextData && contextData.staff && contextData.staff[visit[0]] && contextData.staff[visit[0]].display_name) || visit[0];
      const startHm = visit[1] ? String(visit[1]).slice(11, 16) : '';
      const endHm = visit[2] ? String(visit[2]).slice(11, 16) : '';
      return `${staffName}, ${startHm} – ${endHm}`;
    };
    for (const change of changes) {
      const row = document.createElement('p');
      row.textContent = `${change.order_id}. Было: ${visitLabel(change.before)}. Стало: ${visitLabel(change.after)}.`;
      details.append(row);
    }
    panel.append(details);
  }
}

function refreshValidationBadge() {
  const badge = document.getElementById('plan-validation-badge');
  const requestedPlan = currentPlanData;
  fetch('/api/v1/ui/validation')
    .then(async r => {
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
      return d;
    })
    .then(data => {
      if (requestedPlan && currentPlanData && currentPlanData !== requestedPlan) return;
      currentValidationData = data;
      const violations = Array.isArray(data.violations) ? data.violations : [];
      if (badge) {
        if (violations.length === 0) {
          badge.className = 'validation-badge valid';
          badge.innerText = RU_TEXT.T_0004;
          badge.title = `Все жесткие ограничения H1–H10 соблюдены (в графике: ${data.on_time || 0}, в слоте: ${data.in_slot || 0}). Нажмите для деталей.`;
        } else {
          badge.className = 'validation-badge invalid';
          badge.innerText = `Валидатор H1–H10: ${violations.length} наруш.`;
          badge.title = violations.slice(0, 5).map(v => typeof v === 'string' ? v : `${v.rule || 'H'}: ${v.order_id || v.staff_id || ''}`).join('\n');
        }
      }
      renderDispatcherValidationDetails(data);
      if (currentPlanData) {
        renderKPIs(currentPlanData);
      }
    })
    .catch(err => {
      if (badge) {
        badge.className = 'validation-badge';
        badge.innerText = RU_TEXT.T_0005;
      }
      const panel = document.getElementById('dispatcher-plan-changes');
      if (panel) panel.textContent = String((err && err.message) || err || 'Ошибка загрузки отчёта H1–H10');
    });
}

const DISTRICT_COORDS = {
  'SC-01': [56.8890, 60.6120],
  'SC-02': [56.8335, 60.6010],
  'SC-03': [56.8520, 60.5390],
  'SC-04': [56.8580, 60.6480],
  'SC-05': [56.7990, 60.6150]
};

const DISTRICT_NAMES = {
  'SC-01': RU_TEXT.T_0006,
  'SC-02': RU_TEXT.T_0007,
  'SC-03': RU_TEXT.T_0008,
  'SC-04': RU_TEXT.T_0009,
  'SC-05': RU_TEXT.T_0010
};

const CLIENT_NAMES = [
  RU_TEXT.T_0011, RU_TEXT.T_0012, RU_TEXT.T_0013,
  RU_TEXT.T_0014, RU_TEXT.T_0015, RU_TEXT.T_0016,
  RU_TEXT.T_0017, RU_TEXT.T_0018, RU_TEXT.T_0019,
  RU_TEXT.T_0020, RU_TEXT.T_0021, RU_TEXT.T_0022
];

function showToast(title, text = '', type = 'info', durationMs = 3500, undoToken = null) {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const effectiveDuration = undoToken ? Math.max(durationMs, 10000) : durationMs;
  const card = document.createElement('div');
  card.className = `toast-card ${type}`;

  card.innerHTML = `
    <div class="toast-content">
      <div class="toast-title">${title}</div>
      ${text ? `<div class="toast-text">${text}</div>` : ''}
      ${undoToken ? `<div class="toast-actions"><button type="button" class="toast-undo-btn">Отменить (10 с)</button></div>` : ''}
    </div>
    <button type="button" class="toast-close-btn" title="${RU_TEXT.T_0023}">&times;</button>
  `;

  const undoBtn = card.querySelector('.toast-undo-btn');
  if (undoBtn && undoToken) {
    undoBtn.addEventListener('click', () => {
      card.classList.add('toast-hiding');
      setTimeout(() => card.remove(), 200);
      performUndo(undoToken);
    });
  }

  const closeBtn = card.querySelector('.toast-close-btn');
  if (closeBtn) {
    closeBtn.addEventListener('click', () => {
      card.classList.add('toast-hiding');
      setTimeout(() => card.remove(), 250);
    });
  }

  container.appendChild(card);

  setTimeout(() => {
    if (card.parentNode) {
      card.classList.add('toast-hiding');
      setTimeout(() => card.remove(), 250);
    }
  }, effectiveDuration);
}

let pendingConfirmCallback = null;

function showConfirmDialog(title, message, confirmBtnText = 'Подтвердить', onConfirm) {
  const modal = document.getElementById('modal-confirm-action');
  const titleEl = document.getElementById('modal-confirm-title');
  const msgEl = document.getElementById('modal-confirm-msg');
  const okBtn = document.getElementById('btn-confirm-ok');

  if (!modal || !titleEl || !msgEl || !okBtn) {
    if (confirm(message)) {
      if (onConfirm) onConfirm();
    }
    return;
  }

  titleEl.innerText = title;
  msgEl.innerText = message;
  okBtn.innerText = confirmBtnText;
  pendingConfirmCallback = onConfirm;

  modal.classList.add('active');
}

function initConfirmModal() {
  const modal = document.getElementById('modal-confirm-action');
  const closeBtn = document.getElementById('btn-confirm-close');
  const cancelBtn = document.getElementById('btn-confirm-cancel');
  const okBtn = document.getElementById('btn-confirm-ok');

  const close = () => {
    if (modal) modal.classList.remove('active');
    pendingConfirmCallback = null;
  };

  if (closeBtn) closeBtn.addEventListener('click', close);
  if (cancelBtn) cancelBtn.addEventListener('click', close);
  if (okBtn) {
    okBtn.addEventListener('click', () => {
      const cb = pendingConfirmCallback;
      close();
      if (cb) cb();
    });
  }
}

function getOrderDistrict(orderMeta) {
  if (!orderMeta) return 'SC-01';
  if (orderMeta.service_center_id) return orderMeta.service_center_id;
  if (orderMeta.lat && orderMeta.lon) {
    let bestSc = 'SC-01';
    let minDist = Infinity;
    for (const [scId, coords] of Object.entries(DISTRICT_COORDS)) {
      const d = Math.hypot(orderMeta.lat - coords[0], orderMeta.lon - coords[1]);
      if (d < minDist) {
        minDist = d;
        bestSc = scId;
      }
    }
    return bestSc;
  }
  return 'SC-01';
}

document.addEventListener('DOMContentLoaded', () => {
  initMap();
  initViewSwitcher();
  initSubTabs();
  initEventListeners();
  initModals();
  initConfirmModal();
  initInspector();
  initMobileGanttSync();
  initResponsiveMapResizer();
  initGanttDistrictFilter();
  loadAllData();
});

function initMobileGanttSync() {
  const chart = document.getElementById('gantt-chart');
  const ruler = document.querySelector('.gantt-header-ruler');
  if (chart && ruler) {
    let isSyncingChart = false;
    let isSyncingRuler = false;

    chart.addEventListener('scroll', () => {
      if (!isSyncingChart) {
        isSyncingRuler = true;
        ruler.scrollLeft = chart.scrollLeft;
      }
      isSyncingChart = false;
    });

    ruler.addEventListener('scroll', () => {
      if (!isSyncingRuler) {
        isSyncingChart = true;
        chart.scrollLeft = ruler.scrollLeft;
      }
      isSyncingRuler = false;
    });
  }
}

function initResponsiveMapResizer() {
  window.addEventListener('resize', () => {
    if (map && currentActiveView === 'map') map.invalidateSize();
    if (splitMap && currentActiveView === 'split') splitMap.invalidateSize();
    if (window.engineerRouteMap && currentActiveView === 'sheet') window.engineerRouteMap.invalidateSize();
  });

  window.addEventListener('orientationchange', () => {
    setTimeout(() => {
      if (map && currentActiveView === 'map') map.invalidateSize();
      if (splitMap && currentActiveView === 'split') splitMap.invalidateSize();
      if (window.engineerRouteMap && currentActiveView === 'sheet') window.engineerRouteMap.invalidateSize();
    }, 250);
  });
}

function initMap() {
  const mapEl = document.getElementById('map');
  if (!mapEl) return;

  map = L.map('map', {
    attributionControl: true
  }).setView([56.8450, 60.6050], 12);

  map.attributionControl.setPrefix('<span style="color:#7700FF; font-weight:700;">ПАО Ростелеком</span> | Сервисный центр Екатеринбург');

  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 18,
    attribution: 'OpenStreetMap'
  }).addTo(map);
  attachOfflineStreetsLayer(map);
}

function initViewSwitcher() {
  document.querySelectorAll('.view-tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const targetView = btn.dataset.view;
      switchView(targetView);
    });
  });
}

function switchView(viewName) {
  currentActiveView = viewName;
  document.querySelectorAll('.view-tab-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.view === viewName);
  });
  document.querySelectorAll('.view-page').forEach(p => {
    p.classList.toggle('active', p.id === `view-${viewName}`);
  });

  const dispToolbar = document.getElementById('dispatcher-toolbar');
  const engToolbar = document.getElementById('engineer-toolbar');
  if (currentRole === 'engineer' || viewName === 'sheet') {
    if (dispToolbar) dispToolbar.style.display = 'none';
    if (engToolbar) engToolbar.style.display = 'flex';
    populateEngineerRoleSelect();
  } else {
    if (dispToolbar) dispToolbar.style.display = 'flex';
    if (engToolbar) engToolbar.style.display = 'none';
  }

  if (viewName === 'map') {
    setTimeout(() => { if (map) map.invalidateSize(); }, 80);
  } else if (viewName === 'split') {
    initSplitMap();
    setTimeout(() => { if (splitMap) splitMap.invalidateSize(); }, 80);
  } else if (viewName === 'sheet') {
    const engSelect = document.getElementById('engineer-lk-staff-select');
    const targetStaff = activeSheetStaffId || (engSelect && engSelect.value ? engSelect.value : 'ST-001');
    if (engSelect && engSelect.value !== targetStaff) {
      engSelect.value = targetStaff;
    }
    loadRouteSheetData(targetStaff);
  } else if (viewName === 'kpi') {
    if (currentPlanData) renderKpiDashboard(currentPlanData);
  }
}

let currentRole = 'dispatcher';

function switchRole(roleName) {
  currentRole = roleName;
  const btnDisp = document.getElementById('btn-role-dispatcher');
  const btnEng = document.getElementById('btn-role-engineer');
  const dispToolbar = document.getElementById('dispatcher-toolbar');
  const engToolbar = document.getElementById('engineer-toolbar');
  const kpiBar = document.getElementById('main-kpi-bar');
  const navBar = document.querySelector('.view-nav-bar');

  if (roleName === 'engineer') {
    if (btnEng) btnEng.classList.add('active');
    if (btnDisp) btnDisp.classList.remove('active');
    if (dispToolbar) dispToolbar.style.display = 'none';
    if (engToolbar) engToolbar.style.display = 'flex';
    if (kpiBar) kpiBar.style.display = 'none';
    if (navBar) navBar.style.display = 'none';

    populateEngineerRoleSelect();
    switchView('sheet');
    showToast('Режим инженера', 'Включен персональный интерфейс выездного мастера (ЛК)', 'info', 3000);
  } else {
    if (btnDisp) btnDisp.classList.add('active');
    if (btnEng) btnEng.classList.remove('active');
    if (dispToolbar) dispToolbar.style.display = 'flex';
    if (engToolbar) engToolbar.style.display = 'none';
    if (kpiBar) kpiBar.style.display = 'flex';
    if (navBar) navBar.style.display = 'flex';

    switchView('map');
    showToast('Режим диспетчера', 'Включен полный АРМ логиста (все 40 бригад города)', 'info', 3000);
  }
}

function populateEngineerRoleSelect() {
  const select = document.getElementById('engineer-lk-staff-select');
  if (!select || !currentPlanData) return;
  const routes = currentPlanData.routes || [];
  let currVal = activeSheetStaffId || 'ST-001';
  if (routes.length && !routes.some(r => r.staff_id === currVal)) {
    currVal = routes[0].staff_id;
    activeSheetStaffId = currVal;
  }
  select.innerHTML = '';
  routes.forEach(r => {
    const sMeta = (contextData && contextData.staff) ? contextData.staff[r.staff_id] : null;
    const sName = sMeta ? sMeta.display_name : r.staff_id;
    const opt = document.createElement('option');
    opt.value = r.staff_id;
    opt.innerText = `${r.staff_id}: ${sName}`;
    if (r.staff_id === currVal) opt.selected = true;
    select.appendChild(opt);
  });
  select.value = currVal;
}

function initSubTabs() {
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const parent = btn.closest('.split-side-pane') || document;
      parent.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      parent.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
      btn.classList.add('active');
      const tabId = `tab-${btn.dataset.tab}`;
      const content = document.getElementById(tabId);
      if (content) content.classList.add('active');
    });
  });
}

function initEventListeners() {
  const btnRoleDisp = document.getElementById('btn-role-dispatcher');
  const btnRoleEng = document.getElementById('btn-role-engineer');
  if (btnRoleDisp) {
    btnRoleDisp.addEventListener('click', () => switchRole('dispatcher'));
  }
  if (btnRoleEng) {
    btnRoleEng.addEventListener('click', () => switchRole('engineer'));
  }

  const engLkSelect = document.getElementById('engineer-lk-staff-select');
  if (engLkSelect) {
    engLkSelect.addEventListener('change', (e) => {
      activeSheetStaffId = e.target.value;
      const pageSelect = document.getElementById('page-sheet-staff-select');
      if (pageSelect) pageSelect.value = e.target.value;
      loadRouteSheetData(e.target.value);
    });
  }

  const btnLkPrint = document.getElementById('btn-lk-print-sheet');
  if (btnLkPrint) {
    btnLkPrint.addEventListener('click', () => {
      const staffSelect = document.getElementById('engineer-lk-staff-select');
      const staffId = staffSelect ? staffSelect.value : activeSheetStaffId;
      if (staffId && staffId !== activeSheetStaffId) {
        loadRouteSheetData(staffId);
      }
      setTimeout(() => window.print(), 100);
    });
  }

  const btnLkDownloadPdf = document.getElementById('btn-lk-download-pdf');
  if (btnLkDownloadPdf) {
    btnLkDownloadPdf.addEventListener('click', () => {
      const staffSelect = document.getElementById('engineer-lk-staff-select');
      const staffId = staffSelect ? staffSelect.value : activeSheetStaffId;
      if (staffId && staffId !== activeSheetStaffId) {
        loadRouteSheetData(staffId);
      }
      setTimeout(() => downloadSheetPdf(), 100);
    });
  }

  const btnLkXlsx = document.getElementById('btn-lk-download-xlsx');
  if (btnLkXlsx) {
    btnLkXlsx.addEventListener('click', () => {
      const staffSelect = document.getElementById('engineer-lk-staff-select');
      const staffId = (staffSelect ? staffSelect.value : activeSheetStaffId) || 'ST-001';
      window.location.href = `/api/v1/export/route/${encodeURIComponent(staffId)}.xlsx`;
    });
  }

  const btnLkCsv = document.getElementById('btn-lk-download-csv');
  if (btnLkCsv) {
    btnLkCsv.addEventListener('click', () => {
      const staffSelect = document.getElementById('engineer-lk-staff-select');
      const staffId = (staffSelect ? staffSelect.value : activeSheetStaffId) || 'ST-001';
      window.location.href = `/api/v1/export/route/${encodeURIComponent(staffId)}.csv`;
    });
  }

  const btnLkRestock = document.getElementById('btn-lk-restock');
  if (btnLkRestock) {
    btnLkRestock.addEventListener('click', () => {
      const staffSelect = document.getElementById('engineer-lk-staff-select');
      const staffId = (staffSelect ? staffSelect.value : activeSheetStaffId) || 'ST-001';
      requestEngineerRestock(staffId);
    });
  }

  const btnLkCompleteRestock = document.getElementById('btn-lk-complete-restock');
  if (btnLkCompleteRestock) {
    btnLkCompleteRestock.addEventListener('click', () => {
      const staffSelect = document.getElementById('engineer-lk-staff-select');
      const staffId = (staffSelect ? staffSelect.value : activeSheetStaffId) || 'ST-001';
      completeEngineerRestock(staffId);
    });
  }

  const btnReplan = document.getElementById('btn-replan');
  if (btnReplan) {
    btnReplan.addEventListener('click', () => {
      btnReplan.innerText = 'Оптимизация...';
      btnReplan.disabled = true;

      fetch('/api/v1/plan', { method: 'POST' })
        .then(async res => {
          const data = await res.json().catch(() => ({}));
          if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
          return data;
        })
        .then(data => {
          renderAll(data);
          btnReplan.innerText = 'Спланировать смену';
          btnReplan.disabled = false;
          showToast('План смены сформирован', `Успешно оптимизировано маршрутов: ${data.routes.length}`, 'success');
        })
        .catch(err => {
          showToast('Ошибка планирования', String(err.message || err), 'error');
          btnReplan.innerText = 'Спланировать смену';
          btnReplan.disabled = false;
        });
    });
  }

  const validationBadge = document.getElementById('plan-validation-badge');
  if (validationBadge) {
    const openValidationView = () => {
      switchView('kpi');
      const card = document.getElementById('kpi-validation-card');
      if (card) setTimeout(() => card.scrollIntoView({ behavior: 'smooth', block: 'start' }), 60);
    };
    validationBadge.addEventListener('click', openValidationView);
    validationBadge.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        openValidationView();
      }
    });
  }

  const btnAutoPairVls = document.getElementById('btn-auto-pair-vls');
  if (btnAutoPairVls) {
    btnAutoPairVls.addEventListener('click', async () => {
      const prevText = btnAutoPairVls.innerText;
      btnAutoPairVls.disabled = true;
      btnAutoPairVls.innerText = 'Подбор ВЛС...';
      try {
        const res = await fetch('/api/v1/ui/joint-visits/auto-pair', { method: 'POST' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
        await loadAllData();
        if (activeInspectorOrderId) openInspector(activeInspectorOrderId);
        if (data.paired_count > 0) {
          showToast(
            'Экипажи ВЛС сформированы',
            `Укомплектовано парных выездов: ${data.paired_count}${data.skipped_count ? ` (пропущено без свободного окна: ${data.skipped_count})` : ''}`,
            'success',
            10000,
            data.undo_token || null
          );
        } else {
          showToast(
            'Подбор пар ВЛС',
            data.already_paired_count > 0
              ? `Новых свободных пар не найдено (уже укомплектовано ВЛС: ${data.already_paired_count})`
              : 'Подходящих пар со свободным общим окном не найдено',
            'info'
          );
        }
      } catch (err) {
        showToast('Ошибка автоподбора ВЛС', String(err.message || err), 'error');
      } finally {
        btnAutoPairVls.disabled = false;
        btnAutoPairVls.innerText = prevText;
      }
    });
  }

  const btnSim = document.getElementById('btn-sim-day');
  if (btnSim) {
    btnSim.addEventListener('click', runActiveDaySimulation);
  }

  const btnReset = document.getElementById('btn-reset');
  if (btnReset) {
    btnReset.addEventListener('click', async () => {
      const prevText = btnReset.innerText;
      btnReset.disabled = true;
      btnReset.innerText = 'Сброс...';
      try {
        const res = await fetch('/api/v1/reset-demo', { method: 'POST' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);

        currentValidationData = null;
        activeInspectorOrderId = null;
        const btnCloseInsp = document.getElementById('btn-close-inspector');
        if (btnCloseInsp) btnCloseInsp.click();

        const distFilter = document.getElementById('filter-district');
        if (distFilter) distFilter.value = 'all';
        const crewFilter = document.getElementById('filter-crew');
        if (crewFilter) crewFilter.value = 'all';
        const ganttSearch = document.getElementById('gantt-search');
        if (ganttSearch) ganttSearch.value = '';
        const ganttDist = document.getElementById('gantt-district-select');
        if (ganttDist) ganttDist.value = 'all';
        const unassSearch = document.getElementById('unassigned-search');
        if (unassSearch) unassSearch.value = '';
        const unassDist = document.getElementById('unassigned-filter-district');
        if (unassDist) unassDist.value = 'all';
        activeSplitStaffId = 'all';
        activeSplitCenterId = 'all';
        const splitCenterSel = document.getElementById('split-center-select');
        if (splitCenterSel) splitCenterSel.value = 'all';
        selectedUnassignedIds.clear();
        currentShiftMinutes = 630;
        syncGanttShiftTimeControls();

        document.querySelectorAll('.modal.active').forEach(m => m.classList.remove('active'));
        await loadAllData();
        showToast(
          'Сброс выполнен',
          `Данные и маршруты возвращены к исходному набору (${data.orders || 360} заявок)`,
          'success'
        );
      } catch (err) {
        showToast('Ошибка сброса', String(err.message || err), 'error');
      } finally {
        btnReset.disabled = false;
        btnReset.innerText = prevText;
      }
    });
  }

  const distSelect = document.getElementById('filter-district');
  if (distSelect) {
    distSelect.addEventListener('change', () => {
      updateCrewFilterByDistrict();
      applyFilters();
    });
  }

  const crewSelect = document.getElementById('filter-crew');
  if (crewSelect) {
    crewSelect.addEventListener('change', () => {
      const selectedStaff = crewSelect.value;
      updateSidebarEngineerCard(selectedStaff);
      applyFilters();
    });
  }

  const btnSbSheet = document.getElementById('btn-sb-route-sheet');
  if (btnSbSheet) {
    btnSbSheet.addEventListener('click', () => {
      const selectedCrew = document.getElementById('filter-crew').value;
      const targetStaff = (selectedCrew && selectedCrew !== 'all') ? selectedCrew : (activeSheetStaffId || 'ST-001');
      openRouteSheet(targetStaff);
    });
  }

  const btnSbGantt = document.getElementById('btn-sb-to-gantt');
  if (btnSbGantt) {
    btnSbGantt.addEventListener('click', () => {
      const selectedCrew = document.getElementById('filter-crew').value;
      const targetStaff = (selectedCrew && selectedCrew !== 'all') ? selectedCrew : (activeSheetStaffId || 'ST-001');
      switchView('gantt');
      highlightGanttStaff(targetStaff);
    });
  }

  const btnSbIncident = document.getElementById('btn-sb-incident');
  if (btnSbIncident) {
    btnSbIncident.addEventListener('click', simulateEngineerIncident);
  }

  const btnOpenTraffic = document.getElementById('btn-open-traffic');
  const modalTraffic = document.getElementById('modal-traffic');
  const btnCloseTraffic = document.getElementById('btn-close-traffic');
  const btnCloseTrafficFooter = document.getElementById('btn-close-traffic-footer');
  const btnApplyTraffic = document.getElementById('btn-apply-traffic-replan');

  if (btnOpenTraffic && modalTraffic) {
    btnOpenTraffic.addEventListener('click', () => {
      loadTrafficProfile();
      modalTraffic.classList.add('active');
    });
  }

  if (btnCloseTraffic && modalTraffic) {
    btnCloseTraffic.addEventListener('click', () => {
      modalTraffic.classList.remove('active');
    });
  }

  if (btnCloseTrafficFooter && modalTraffic) {
    btnCloseTrafficFooter.addEventListener('click', () => {
      modalTraffic.classList.remove('active');
    });
  }

  if (modalTraffic) {
    modalTraffic.addEventListener('click', (e) => {
      if (e.target === modalTraffic) {
        modalTraffic.classList.remove('active');
      }
    });
  }

  if (btnApplyTraffic) {
    btnApplyTraffic.addEventListener('click', applyTrafficProfileAndReplan);
  }

  const chkMasterTraffic = document.getElementById('chk-traffic-master');
  if (chkMasterTraffic) {
    chkMasterTraffic.addEventListener('change', (e) => {
      currentTrafficEnabled = e.target.checked;
      updateTrafficMasterStatusLabel();
      renderTrafficHoursChips();
    });
  }

  const applyTrafficPreset = (preset) => {
    currentTrafficEnabled = true;
    const m = document.getElementById('chk-traffic-master');
    if (m) m.checked = true;
    const b = document.getElementById('chk-bridge-penalty');
    if (preset === 'morning') {
      currentHourlyFactors = {
        8: 1.35, 9: 1.4, 10: 1.25, 11: 1.0, 12: 1.0, 13: 1.0,
        14: 1.0, 15: 1.0, 16: 1.0, 17: 1.0, 18: 1.0, 19: 1.0, 20: 1.0
      };
      currentDisabledHours = new Set([11, 12, 13, 14, 15, 16, 17, 18, 19, 20]);
      currentBridgeEnabled = false;
    } else if (preset === 'evening') {
      currentHourlyFactors = {
        8: 1.0, 9: 1.0, 10: 1.0, 11: 1.0, 12: 1.0, 13: 1.0,
        14: 1.0, 15: 1.0, 16: 1.0, 17: 1.15, 18: 1.45, 19: 1.4, 20: 1.2
      };
      currentDisabledHours = new Set([8, 9, 10, 11, 12, 13, 14, 15, 16]);
      currentBridgeEnabled = false;
    } else if (preset === 'jam') {
      currentHourlyFactors = {
        8: 1.4, 9: 1.5, 10: 1.3, 11: 1.15, 12: 1.15, 13: 1.15,
        14: 1.15, 15: 1.2, 16: 1.3, 17: 1.45, 18: 1.6, 19: 1.5, 20: 1.25
      };
      currentDisabledHours.clear();
      currentBridgeEnabled = true;
    }
    if (b) b.checked = currentBridgeEnabled;
    updateTrafficMasterStatusLabel();
    renderTrafficHoursChips();
  };

  const btnPresetMorning = document.getElementById('btn-traffic-preset-morning');
  if (btnPresetMorning) btnPresetMorning.addEventListener('click', () => applyTrafficPreset('morning'));
  const btnPresetEvening = document.getElementById('btn-traffic-preset-evening');
  if (btnPresetEvening) btnPresetEvening.addEventListener('click', () => applyTrafficPreset('evening'));
  const btnPresetJam = document.getElementById('btn-traffic-preset-jam');
  if (btnPresetJam) btnPresetJam.addEventListener('click', () => applyTrafficPreset('jam'));

  const btnEnableAll = document.getElementById('btn-traffic-enable-all');
  if (btnEnableAll) {
    btnEnableAll.addEventListener('click', () => {
      currentTrafficEnabled = true;
      currentDisabledHours.clear();
      const m = document.getElementById('chk-traffic-master');
      if (m) m.checked = true;
      updateTrafficMasterStatusLabel();
      renderTrafficHoursChips();
    });
  }

  const btnDisableAll = document.getElementById('btn-traffic-disable-all');
  if (btnDisableAll) {
    btnDisableAll.addEventListener('click', () => {
      currentDisabledHours = new Set(Object.keys(currentHourlyFactors).map(Number));
      renderTrafficHoursChips();
    });
  }

  const btnResetTraffic = document.getElementById('btn-traffic-reset-default');
  if (btnResetTraffic) {
    btnResetTraffic.addEventListener('click', () => {
      currentHourlyFactors = {
        8: 1.0, 9: 1.0, 10: 1.0, 11: 1.0, 12: 1.0, 13: 1.0,
        14: 1.0, 15: 1.0, 16: 1.0, 17: 1.0, 18: 1.0, 19: 1.0, 20: 1.0
      };
      currentDisabledHours.clear();
      currentTrafficEnabled = false;
      currentBridgeEnabled = false;
      const m = document.getElementById('chk-traffic-master');
      if (m) m.checked = false;
      const b = document.getElementById('chk-bridge-penalty');
      if (b) b.checked = false;
      updateTrafficMasterStatusLabel();
      renderTrafficHoursChips();
    });
  }

  const chkBridge = document.getElementById('chk-bridge-penalty');
  if (chkBridge) {
    chkBridge.addEventListener('change', (e) => {
      currentBridgeEnabled = e.target.checked;
      renderTrafficHoursChips();
    });
  }

  const btnExportAllCsv = document.getElementById('btn-export-all-csv');
  if (btnExportAllCsv) {
    btnExportAllCsv.addEventListener('click', () => {
      window.location.href = '/api/v1/export/routes.csv';
    });
  }

  const btnExportAllXlsx = document.getElementById('btn-export-all-xlsx');
  if (btnExportAllXlsx) {
    btnExportAllXlsx.addEventListener('click', () => {
      window.location.href = '/api/v1/export/routes.xlsx';
    });
  }

  const btnRefreshKpi = document.getElementById('btn-refresh-kpi-view');
  if (btnRefreshKpi) {
    btnRefreshKpi.addEventListener('click', () => {
      if (currentPlanData) {
        renderKpiDashboard(currentPlanData);
        showToast('Показатели KPI обновлены', 'Все метрики пересчитаны на основе текущего плана смены', 'success', 2500);
      }
    });
  }

  const btnExportKpi = document.getElementById('btn-export-kpi-summary');
  if (btnExportKpi) {
    btnExportKpi.addEventListener('click', () => {
      exportKpiSummaryCsv();
    });
  }

  const btnExportKpiXlsx = document.getElementById('btn-export-kpi-xlsx');
  if (btnExportKpiXlsx) {
    btnExportKpiXlsx.addEventListener('click', () => {
      window.location.href = '/api/v1/export/routes.xlsx';
    });
  }

  const pageStaffSelect = document.getElementById('page-sheet-staff-select');
  if (pageStaffSelect) {
    pageStaffSelect.addEventListener('change', (e) => {
      loadRouteSheetData(e.target.value);
    });
  }

  const btnPagePrint = document.getElementById('btn-page-print-sheet');
  if (btnPagePrint) {
    btnPagePrint.addEventListener('click', () => {
      window.print();
    });
  }

  const btnPageDownloadPdf = document.getElementById('btn-page-download-pdf');
  if (btnPageDownloadPdf) {
    btnPageDownloadPdf.addEventListener('click', downloadSheetPdf);
  }

  const btnPageDownloadXlsx = document.getElementById('btn-page-download-xlsx');
  if (btnPageDownloadXlsx) {
    btnPageDownloadXlsx.addEventListener('click', () => {
      window.location.href = `/api/v1/export/route/${encodeURIComponent(activeSheetStaffId || 'ST-001')}.xlsx`;
    });
  }

  const btnPageCsv = document.getElementById('btn-page-download-csv');
  if (btnPageCsv) {
    btnPageCsv.addEventListener('click', () => {
      window.location.href = `/api/v1/export/route/${activeSheetStaffId || 'ST-001'}.csv`;
    });
  }

  const btnInspSheet = document.getElementById('btn-insp-route-sheet');
  if (btnInspSheet) {
    btnInspSheet.addEventListener('click', () => {
      const staffId = document.getElementById('insp-staff-id').innerText;
      if (staffId && staffId !== '-') {
        openRouteSheet(staffId);
      } else {
        const selectedTarget = document.getElementById('reassign-select').value;
        if (selectedTarget) {
          openRouteSheet(selectedTarget);
        } else {
          openRouteSheet('ST-001');
        }
      }
    });
  }

  const unassignedSearch = document.getElementById('unassigned-search');
  if (unassignedSearch) {
    unassignedSearch.addEventListener('input', filterUnassignedTable);
  }

  const unassignedDistSelect = document.getElementById('unassigned-filter-district');
  if (unassignedDistSelect) {
    unassignedDistSelect.addEventListener('change', filterUnassignedTable);
  }

  const btnBulkReschedule = document.getElementById('btn-bulk-reschedule');
  if (btnBulkReschedule) {
    btnBulkReschedule.addEventListener('click', bulkRescheduleAll);
  }

  const btnBulkRescheduleSelected = document.getElementById('btn-bulk-reschedule-selected');
  if (btnBulkRescheduleSelected) {
    btnBulkRescheduleSelected.addEventListener('click', bulkRescheduleSelected);
  }

  const unassignedSelectAll = document.getElementById('unassigned-select-all');
  if (unassignedSelectAll) {
    unassignedSelectAll.addEventListener('change', (e) => {
      const checked = e.target.checked;
      document.querySelectorAll('#unassigned-rows tr').forEach(row => {
        if (row.style.display === 'none') return;
        const chk = row.querySelector('.unassigned-row-chk');
        if (!chk || chk.disabled) return;
        chk.checked = checked;
        const oid = chk.dataset.orderId;
        if (checked) selectedUnassignedIds.add(oid);
        else selectedUnassignedIds.delete(oid);
      });
      updateSelectedUnassignedControls();
    });
  }

  const btnAddDutyCrew = document.getElementById('btn-add-duty-crew');
  if (btnAddDutyCrew) {
    btnAddDutyCrew.addEventListener('click', addDutyCrews);
  }

  const splitCenterSelect = document.getElementById('split-center-select');
  const splitCrewSelect = document.getElementById('split-crew-select');
  if (splitCenterSelect) {
    splitCenterSelect.addEventListener('change', (e) => {
      activeSplitCenterId = e.target.value || 'all';
      activeSplitStaffId = 'all';
      if (splitCrewSelect) splitCrewSelect.value = 'all';
      if (currentPlanData) {
        populateSplitCrewSelect(currentPlanData.routes, currentPlanData.alerts || []);
        renderRoutesOnSplitMap(currentPlanData, 'all');
      }
    });
  }

  if (splitCrewSelect) {
    splitCrewSelect.addEventListener('change', (e) => {
      renderRoutesOnSplitMap(currentPlanData, e.target.value);
    });
  }

  const btnSplitReset = document.getElementById('btn-split-reset-view');
  if (btnSplitReset) {
    btnSplitReset.addEventListener('click', () => {
      activeSplitCenterId = 'all';
      activeSplitStaffId = 'all';
      if (splitCenterSelect) splitCenterSelect.value = 'all';
      if (splitCrewSelect) splitCrewSelect.value = 'all';
      if (currentPlanData) {
        populateSplitCrewSelect(currentPlanData.routes, currentPlanData.alerts || []);
        renderRoutesOnSplitMap(currentPlanData, 'all');
      }
      if (splitMap) splitMap.setView([56.8450, 60.6050], 12);
    });
  }

  const ganttTimeSlider = document.getElementById('gantt-shift-time');
  if (ganttTimeSlider) {
    ganttTimeSlider.addEventListener('input', (e) => {
      currentShiftMinutes = Number(e.target.value) || 630;
      syncGanttShiftTimeControls();
      applyMapTimeDimming();
    });
  }

  document.querySelectorAll('.sidebar-mode-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const mode = btn.dataset.sbMode;
      document.querySelectorAll('.sidebar-mode-btn').forEach(b => b.classList.toggle('active', b === btn));
      const filtersPanel = document.getElementById('sidebar-filters-panel');
      const boardPanel = document.getElementById('sidebar-order-board-panel');
      if (filtersPanel) filtersPanel.style.display = mode === 'filters' ? '' : 'none';
      if (boardPanel) boardPanel.style.display = mode === 'board' ? 'flex' : 'none';
      if (mode === 'board') refreshOrderBoard();
    });
  });

  const boardSearch = document.getElementById('board-search');
  if (boardSearch) boardSearch.addEventListener('input', renderOrderBoardList);
  const boardStatus = document.getElementById('board-status-filter');
  if (boardStatus) boardStatus.addEventListener('change', renderOrderBoardList);
  const boardHeightOnly = document.getElementById('board-height-only');
  if (boardHeightOnly) boardHeightOnly.addEventListener('change', renderOrderBoardList);
  document.querySelectorAll('#board-cat-row .board-cat-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      orderBoardCategory = btn.dataset.cat || 'ALL';
      document.querySelectorAll('#board-cat-row .board-cat-btn').forEach(b => b.classList.toggle('active', b === btn));
      renderOrderBoardList();
    });
  });

  const searchInput = document.getElementById('gantt-search');
  if (searchInput) {
    searchInput.addEventListener('input', () => {
      filterGanttRows();
    });
  }
}

function initGanttDistrictFilter() {
  const container = document.getElementById('gantt-district-filter');
  if (container) {
    container.addEventListener('click', (e) => {
      const btn = e.target.closest('.gantt-dist-btn');
      if (!btn) return;
      container.querySelectorAll('.gantt-dist-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      filterGanttRows();
    });
  }

  const distSelect = document.getElementById('gantt-district-select');
  if (distSelect) {
    distSelect.addEventListener('change', () => {
      filterGanttRows();
    });
  }
}

function filterGanttRows() {
  const searchInput = document.getElementById('gantt-search');
  const q = searchInput ? searchInput.value.toLowerCase().trim() : '';

  const distSelect = document.getElementById('gantt-district-select');
  const activeBtn = document.querySelector('.gantt-dist-btn.active');
  const dist = distSelect ? distSelect.value : (activeBtn ? activeBtn.dataset.district : 'all');

  document.querySelectorAll('.gantt-row').forEach(row => {
    const text = row.innerText.toLowerCase();
    const rowDist = row.dataset.district;
    const matchSearch = !q || text.includes(q);
    const matchDist = (dist === 'all' || rowDist === dist);
    row.style.display = (matchSearch && matchDist) ? 'flex' : 'none';
  });
}

function highlightGanttStaff(staffId) {
  setTimeout(() => {
    document.querySelectorAll('.gantt-row').forEach(row => {
      if (row.dataset.staffId === staffId) {
        row.classList.add('active-row');
        row.scrollIntoView({ behavior: 'smooth', block: 'center' });
      } else {
        row.classList.remove('active-row');
      }
    });
  }, 100);
}

function initInspector() {
  const panel = document.getElementById('order-inspector');
  const closeBtn = document.getElementById('btn-close-inspector');
  if (closeBtn) {
    closeBtn.addEventListener('click', () => {
      panel.classList.remove('open');
      activeInspectorOrderId = null;
      document.querySelectorAll('.gantt-block.selected-block').forEach(b => b.classList.remove('selected-block'));
    });
  }

  const reassignBtn = document.getElementById('btn-do-reassign');
  if (reassignBtn) {
    reassignBtn.addEventListener('click', () => {
      if (!activeInspectorOrderId) return;
      if (isOrderConfirmedTomorrow(activeInspectorOrderId)) {
        showToast(RU_TEXT.T_0247, RU_TEXT.T_0243, 'warning');
        return;
      }
      const targetStaff = document.getElementById('reassign-select').value;
      if (!targetStaff) return;

      const cand = (window.currentCandidatesMap && window.currentCandidatesMap[targetStaff]) || null;
      if (cand && !cand.eligible) {
        showToast('Назначение отклонено', cand.reasons.join('. '), 'error');
        return;
      }

      const eventPayload = {
        event_id: 'EV-REASSIGN-' + Date.now().toString().slice(-4),
        type: 'ORDER_TRANSFERRED',
        timestamp: getCurrentEventTimestamp(),
        order_id: activeInspectorOrderId,
        to_staff_id: targetStaff
      };

      fetch('/api/v1/events', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(eventPayload)
      })
        .then(res => res.json())
        .then(resData => {
          if (resData.accepted === false) {
            showToast('Назначение отклонено', resData.message || 'Ошибка назначения', 'error');
            return;
          }
          if (cand && cand.needs_restock) {
            showToast('Наряд назначен', `Заявка ${activeInspectorOrderId} передана специалисту ${targetStaff}. Спланирован заезд на склад СЦ за оборудованием!`, 'success', 10000, resData.undo_token || null);
          } else {
            showToast('Наряд назначен', `Заявка ${activeInspectorOrderId} передана специалисту ${targetStaff}`, 'success', 10000, resData.undo_token || null);
          }
          loadAllData();
          setTimeout(() => openInspector(activeInspectorOrderId), 300);
        })
        .catch(err => showToast('Ошибка назначения наряда', String(err), 'error'));
    });
  }

  const reassignSelectEl = document.getElementById('reassign-select');
  if (reassignSelectEl) {
    reassignSelectEl.addEventListener('change', () => {
      updateReassignValidationCard(reassignSelectEl.value);
    });
  }

  const delayBtn = document.getElementById('btn-add-delay');
  if (delayBtn) {
    delayBtn.addEventListener('click', () => {
      if (!activeInspectorOrderId || !currentPlanData) return;
      if (isOrderConfirmedTomorrow(activeInspectorOrderId)) {
        showToast(RU_TEXT.T_0247, RU_TEXT.T_0243, 'warning');
        return;
      }
      let targetStaff = null;
      let targetStop = null;
      for (const r of currentPlanData.routes) {
        for (const s of r.stops) {
          if (s.order_id === activeInspectorOrderId) {
            targetStaff = r.staff_id;
            targetStop = s;
            break;
          }
        }
        if (targetStaff) break;
      }

      if (targetStaff && targetStop) {
        const [h, m] = targetStop.end.split('T')[1].substring(0, 5).split(':').map(Number);
        const newM = m + 30;
        const newH = h + Math.floor(newM / 60);
        const activeDate = getActivePlanDate();
        const newEndStr = `${activeDate}T${String(newH).padStart(2, '0')}:${String(newM % 60).padStart(2, '0')}:00${getTimezoneOffsetSuffix()}`;

        const delayPayload = {
          event_id: 'EV-DELAY-' + Date.now().toString().slice(-4),
          type: 'VISIT_DELAYED',
          timestamp: getCurrentEventTimestamp(),
          staff_id: targetStaff,
          order_id: activeInspectorOrderId,
          expected_end: newEndStr
        };

        fetch('/api/v1/events', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(delayPayload)
        })
          .then(res => res.json())
          .then(resData => {
            showToast('Задержка зафиксирована', `Наряд ${activeInspectorOrderId} продлен на +30 мин. График смены пересчитан.`, 'warning', 10000, resData.undo_token || null);
            loadAllData();
            switchView('split');
          })
          .catch(err => showToast('Ошибка фиксации задержки', String(err), 'error'));
      } else {
        confirmOrderReschedule(activeInspectorOrderId);
      }
    });
  }

  const cancelBtn = document.getElementById('btn-cancel-visit');
  if (cancelBtn) {
    cancelBtn.addEventListener('click', () => {
      if (!activeInspectorOrderId) return;

      showConfirmDialog(
        'Отмена визита',
        `Вы действительно хотите отменить визит по наряду ${activeInspectorOrderId}?`,
        'Отменить визит',
        () => {
          const cancelPayload = {
            event_id: 'EV-CANCEL-' + Date.now().toString().slice(-4),
            type: 'ORDER_CANCELLED',
            timestamp: getCurrentEventTimestamp(),
            order_id: activeInspectorOrderId,
            reason: 'Отказ абонента'
          };

          fetch('/api/v1/events', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(cancelPayload)
          })
            .then(res => res.json())
            .then(resData => {
              showToast('Визит отменен', `Наряд ${activeInspectorOrderId} успешно отменен.`, 'info', 10000, resData.undo_token || null);
              loadAllData();
              panel.classList.remove('open');
            })
            .catch(err => showToast('Ошибка отмены наряда', String(err), 'error'));
        }
      );
    });
  }

  const toggleCompleteBtn = document.getElementById('btn-insp-toggle-complete');
  if (toggleCompleteBtn) {
    toggleCompleteBtn.addEventListener('click', () => {
      if (!activeInspectorOrderId) return;
      let targetStaff = null;
      if (currentPlanData && currentPlanData.routes) {
        for (const r of currentPlanData.routes) {
          for (const s of r.stops) {
            if (s.order_id === activeInspectorOrderId) {
              targetStaff = r.staff_id;
              break;
            }
          }
          if (targetStaff) break;
        }
      }
      toggleCompleteOrder(activeInspectorOrderId, targetStaff);
    });
  }
}

function populateAddOrderEngineers() {
  const select = document.getElementById('add-engineer-select');
  const warn = document.getElementById('add-engineer-warning');
  if (!select) return;

  const workType = document.getElementById('add-work-type') ? document.getElementById('add-work-type').value : '';
  const tech = document.getElementById('add-tech') ? document.getElementById('add-tech').value : '';
  const district = document.getElementById('add-district') ? document.getElementById('add-district').value : 'SC-01';
  const reqHeight = workType.includes('VLS');
  const wtMeta = (contextData && contextData.work_types) ? contextData.work_types[workType] : null;
  const reqProfiles = (wtMeta && Array.isArray(wtMeta.profiles) && wtMeta.profiles.length)
    ? wtMeta.profiles
    : (workType.startsWith('INST') ? ['INSTALL'] : (workType === 'SURVEY' ? ['INSTALL', 'FAULT_3LTP'] : ['FAULT_3LTP']));

  select.innerHTML = '<option value="auto">Автоматический умный подбор (рекомендуется)</option>';
  if (warn) warn.style.display = 'none';

  if (!contextData || !contextData.staff) return;

  const staffList = Object.values(contextData.staff);
  const districtStaff = staffList.filter(s => s.service_center_id === district);
  const otherStaff = staffList.filter(s => s.service_center_id !== district);

  const groupDistrict = document.createElement('optgroup');
  groupDistrict.label = `Инженеры района (${DISTRICT_NAMES[district] || district})`;

  const groupOther = document.createElement('optgroup');
  groupOther.label = 'Инженеры других районов';

  const formatAddEngineerOption = (s, withSc) => {
    const opt = document.createElement('option');
    opt.value = s.staff_id;
    const shortName = s.display_name.split(' (')[0];
    const scSuffix = withSc ? ` (${s.service_center_id})` : '';
    const profileOk = !reqProfiles.length || (s.profiles || []).some(p => reqProfiles.includes(p));
    const techOk = !tech || !(s.technologies && s.technologies.length) || s.technologies.includes(tech);
    const heightOk = !reqHeight || Boolean(s.height_permit);
    const denyReasons = [];
    if (!heightOk) denyReasons.push('Нет допуска к высоте');
    if (!techOk) denyReasons.push(`Нет ${tech}`);
    if (!profileOk) denyReasons.push('Другой профиль');
    if (denyReasons.length) {
      opt.innerText = `${s.staff_id}: ${shortName}${scSuffix} [${denyReasons.join(', ')}]`;
      opt.disabled = true;
    } else if (reqHeight && s.height_permit) {
      opt.innerText = `${s.staff_id}: ${shortName}${scSuffix} [Допуск к высоте есть]`;
    } else {
      opt.innerText = `${s.staff_id}: ${shortName}${scSuffix}`;
    }
    return opt;
  };

  districtStaff.forEach(s => groupDistrict.appendChild(formatAddEngineerOption(s, false)));
  otherStaff.forEach(s => groupOther.appendChild(formatAddEngineerOption(s, true)));

  if (groupDistrict.children.length) select.appendChild(groupDistrict);
  if (groupOther.children.length) select.appendChild(groupOther);
}

function initModals() {
  const modalAdd = document.getElementById('modal-add-order');
  const btnOpenAdd = document.getElementById('btn-open-add');
  if (btnOpenAdd && modalAdd) {
    btnOpenAdd.addEventListener('click', () => {
      document.getElementById('add-ord-id').value = 'ORD-NEW-' + Date.now().toString().slice(-4);
      modalAdd.classList.add('active');
      populateAddOrderEngineers();
    });
  }

  const workTypeSelect = document.getElementById('add-work-type');
  if (workTypeSelect) {
    workTypeSelect.addEventListener('change', populateAddOrderEngineers);
  }
  const techSelect = document.getElementById('add-tech');
  if (techSelect) {
    techSelect.addEventListener('change', populateAddOrderEngineers);
  }
  const districtSelect = document.getElementById('add-district');
  if (districtSelect) {
    districtSelect.addEventListener('change', populateAddOrderEngineers);
  }

  const btnCloseAdd = document.getElementById('btn-close-add');
  if (btnCloseAdd && modalAdd) {
    btnCloseAdd.addEventListener('click', () => modalAdd.classList.remove('active'));
  }
  const btnCancelAdd = document.getElementById('btn-cancel-add');
  if (btnCancelAdd && modalAdd) {
    btnCancelAdd.addEventListener('click', () => modalAdd.classList.remove('active'));
  }

  document.querySelectorAll('.btn-preset').forEach(btn => {
    btn.addEventListener('click', () => {
      const p = btn.dataset.preset;
      if (p === 'uralmash-pon') {
        document.getElementById('add-work-type').value = 'INST_PON_MKD';
        document.getElementById('add-tech').value = 'PON';
        document.getElementById('add-district').value = 'SC-01';
        document.getElementById('add-address').value = 'г. Екатеринбург, ул. Баумана, д. 15, кв. 24';
        document.getElementById('add-slot').value = '11:00-13:00';
      } else if (p === 'center-fttb') {
        document.getElementById('add-work-type').value = 'INST_FTTB_MKD';
        document.getElementById('add-tech').value = 'FTTB';
        document.getElementById('add-district').value = 'SC-02';
        document.getElementById('add-address').value = 'г. Екатеринбург, пр-кт Ленина, д. 48, кв. 10';
        document.getElementById('add-slot').value = '13:00-15:00';
      } else if (p === 'viz-repair') {
        document.getElementById('add-work-type').value = 'REPAIR';
        document.getElementById('add-tech').value = 'FTTB';
        document.getElementById('add-district').value = 'SC-03';
        document.getElementById('add-address').value = 'г. Екатеринбург, ул. Крауля, д. 63, кв. 77';
        document.getElementById('add-slot').value = '15:00-17:00';
      }
      populateAddOrderEngineers();
    });
  });

  const btnPickMapAddr = document.getElementById('btn-pick-map-address');
  const btnClearMapCoords = document.getElementById('btn-clear-map-coords');
  const clearPickedCoords = () => {
    const latIn = document.getElementById('add-lat');
    const lonIn = document.getElementById('add-lon');
    const badge = document.getElementById('add-coords-badge');
    if (latIn) latIn.value = '';
    if (lonIn) lonIn.value = '';
    if (badge) badge.innerText = 'Координаты: авто по району';
    if (btnClearMapCoords) btnClearMapCoords.style.display = 'none';
    if (pickedLocationMarker && map) {
      map.removeLayer(pickedLocationMarker);
      pickedLocationMarker = null;
    }
  };
  if (btnClearMapCoords) btnClearMapCoords.addEventListener('click', clearPickedCoords);

  if (btnPickMapAddr && modalAdd) {
    btnPickMapAddr.addEventListener('click', () => {
      if (!map) return;
      pickingOrderLocation = true;
      modalAdd.classList.remove('active');
      switchView('map');
      const mapEl = document.getElementById('map');
      if (mapEl) mapEl.classList.add('picking-location');
      showToast('Выбор точки на карте', 'Кликните на карте города, чтобы задать точный адрес и координаты наряда.', 'info', 6000);
      map.once('click', (ev) => {
        pickingOrderLocation = false;
        if (mapEl) mapEl.classList.remove('picking-location');
        const lat = Number(ev.latlng.lat.toFixed(5));
        const lon = Number(ev.latlng.lng.toFixed(5));
        const latIn = document.getElementById('add-lat');
        const lonIn = document.getElementById('add-lon');
        const badge = document.getElementById('add-coords-badge');
        if (latIn) latIn.value = String(lat);
        if (lonIn) lonIn.value = String(lon);
        if (badge) badge.innerText = `Точка: ${lat}, ${lon}`;
        if (btnClearMapCoords) btnClearMapCoords.style.display = 'inline-flex';
        if (pickedLocationMarker) map.removeLayer(pickedLocationMarker);
        pickedLocationMarker = L.marker([lat, lon]).addTo(map).bindPopup(`Выбранная точка: ${lat}, ${lon}`).openPopup();

        let bestSc = null;
        let bestScDist = Infinity;
        Object.keys(DISTRICT_COORDS).forEach(scId => {
          const [scLat, scLon] = DISTRICT_COORDS[scId];
          const d2 = (scLat - lat) ** 2 + (scLon - lon) ** 2;
          if (d2 < bestScDist) { bestScDist = d2; bestSc = scId; }
        });
        const distSel = document.getElementById('add-district');
        if (distSel && bestSc) distSel.value = bestSc;

        let nearestAddr = null;
        let minOrdDist = Infinity;
        if (contextData && contextData.orders) {
          Object.values(contextData.orders).forEach(o => {
            if (!o || o.lat == null || o.lon == null) return;
            const d2 = (o.lat - lat) ** 2 + (o.lon - lon) ** 2;
            if (d2 < minOrdDist) { minOrdDist = d2; nearestAddr = o.address; }
          });
        }
        const addrIn = document.getElementById('add-address');
        if (addrIn) {
          addrIn.value = nearestAddr
            ? `г. Екатеринбург, рядом с: ${nearestAddr.replace(/^г\.\s*Екатеринбург,\s*/i, '')} (${lat}, ${lon})`
            : `г. Екатеринбург (точка ${lat}, ${lon})`;
        }
        populateAddOrderEngineers();
        modalAdd.classList.add('active');
      });
    });
  }

  const formAdd = document.getElementById('form-add-order');
  if (formAdd) {
    formAdd.addEventListener('submit', (e) => {
      e.preventDefault();
      const ordId = document.getElementById('add-ord-id').value;
      const workType = document.getElementById('add-work-type').value;
      const tech = document.getElementById('add-tech').value;
      const district = document.getElementById('add-district').value;
      const address = document.getElementById('add-address').value;
      const clientName = (document.getElementById('add-client')?.value || '').trim();
      const clientPhone = (document.getElementById('add-phone')?.value || '').trim();
      const slotStr = document.getElementById('add-slot').value;
      const [sStart, sEnd] = slotStr.split('-');

      const pickedLat = parseFloat(document.getElementById('add-lat')?.value || '');
      const pickedLon = parseFloat(document.getElementById('add-lon')?.value || '');
      const hasPicked = Number.isFinite(pickedLat) && Number.isFinite(pickedLon);

      const baseCoords = DISTRICT_COORDS[district] || [56.8335, 60.6010];
      const randOffsetLat = (Math.random() - 0.5) * 0.015;
      const randOffsetLon = (Math.random() - 0.5) * 0.025;

      const isInst = workType.startsWith('INST');
      const [sh, sm] = sStart.split(':').map(Number);
      const latestM = sm + (isInst ? 15 : 90);
      const lh = sh + Math.floor(latestM / 60);
      const lm = latestM % 60;
      const latestStr = `${String(lh).padStart(2, '0')}:${String(lm).padStart(2, '0')}`;

      const chosenStaff = document.getElementById('add-engineer-select') ? document.getElementById('add-engineer-select').value : 'auto';
      const activeDate = getActivePlanDate();
      const tzSuffix = getTimezoneOffsetSuffix();
      const nextDate = getNextWorkdayIso();

      const newOrderPayload = {
        order_id: ordId,
        work_type_code: workType,
        technology: tech,
        building_id: 'B-NEW-' + Date.now().toString().slice(-4),
        address: address,
        client_name: clientName || null,
        client_phone: clientPhone || null,
        lat: hasPicked ? pickedLat : Number((baseCoords[0] + randOffsetLat).toFixed(5)),
        lon: hasPicked ? pickedLon : Number((baseCoords[1] + randOffsetLon).toFixed(5)),
        building_type: 'MKD',
        requires_height: workType.includes('VLS'),
        access_node_id: 'AN-0103',
        registered_at: `${activeDate}T08:00:00${tzSuffix}`,
        slot: {
          start: `${activeDate}T${sStart}:00${tzSuffix}`,
          end: `${activeDate}T${sEnd}:00${tzSuffix}`
        },
        latest_start: `${activeDate}T${latestStr}:00${tzSuffix}`,
        deadline: `${nextDate}T18:00:00${tzSuffix}`,
        required_equipment: isInst ? { ONT: 1, ROUTER: 1, STB: 0 } : { ONT: 0, ROUTER: 1, STB: 0 },
        is_repeat: false
      };

      if (chosenStaff && chosenStaff !== 'auto') {
        newOrderPayload.assigned_staff_id = chosenStaff;
      }

      fetch('/api/v1/custom-order', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(newOrderPayload)
      })
        .then(async res => {
          const resData = await res.json().catch(() => ({}));
          if (!res.ok) throw new Error(resData.detail || (resData.error && resData.error.message) || `HTTP ${res.status}`);
          return resData;
        })
        .then(resData => {
          modalAdd.classList.remove('active');
          clearPickedCoords();
          const msg = resData.message || `Заявка ${ordId} успешно добавлена в план смены`;
          showToast('Наряд зарегистрирован', msg, 'success', 10000, resData.undo_token || null);
          loadAllData();
          setTimeout(() => openInspector(ordId), 400);
        })
        .catch(err => showToast('Ошибка добавления наряда', String(err.message || err), 'error'));
    });
  }

  const modalUpload = document.getElementById('modal-upload');
  const btnOpenUpload = document.getElementById('btn-open-upload');
  if (btnOpenUpload && modalUpload) {
    btnOpenUpload.addEventListener('click', () => modalUpload.classList.add('active'));
  }
  const btnCloseUpload = document.getElementById('btn-close-upload');
  if (btnCloseUpload && modalUpload) {
    btnCloseUpload.addEventListener('click', () => modalUpload.classList.remove('active'));
  }
  const btnCancelUpload = document.getElementById('btn-cancel-upload');
  if (btnCancelUpload && modalUpload) {
    btnCancelUpload.addEventListener('click', () => modalUpload.classList.remove('active'));
  }

  const fileInput = document.getElementById('file-input');
  if (fileInput) {
    fileInput.addEventListener('change', (e) => {
      const file = e.target.files[0];
      if (!file) return;

      const reader = new FileReader();
      reader.onload = (evt) => {
        try {
          const parsed = JSON.parse(evt.target.result);
          fetch('/api/v1/upload-day', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(parsed)
          })
            .then(res => res.json())
            .then(data => {
              modalUpload.classList.remove('active');
              showToast('Файл успешно загружен', `Заявок в смене: ${data.orders_count}`, 'success');
              loadAllData();
            })
            .catch(err => showToast('Ошибка при загрузке файла', String(err), 'error'));
        } catch (err) {
          showToast('Ошибка парсинга JSON', String(err), 'error');
        }
      };
      reader.readAsText(file);
    });
  }

  initScenarioModal();
  initForceMajeureModal();
}

function openRouteSheet(staffId) {
  if (!staffId || staffId === '-' || staffId === 'all') staffId = activeSheetStaffId || 'ST-001';
  activeSheetStaffId = staffId;
  const engSelect = document.getElementById('engineer-lk-staff-select');
  if (engSelect) engSelect.value = staffId;
  switchView('sheet');
  loadRouteSheetData(staffId);
}

function loadRouteSheetData(staffId) {
  if (!staffId || staffId === 'all') staffId = 'ST-001';
  activeSheetStaffId = staffId;
  const reqSeq = ++routeSheetReqSeq;

  const engSelect = document.getElementById('engineer-lk-staff-select');
  if (engSelect && engSelect.value !== staffId) engSelect.value = staffId;

  fetch(`/api/v1/routes/${staffId}/sheet`)
    .then(r => r.json())
    .then(data => {
      if (reqSeq !== routeSheetReqSeq) return;
      updateRouteSheetDOM(data);
    })
    .catch(err => console.error('Ошибка загрузки маршрутного листа:', err));
}

function getCheckIconSvg(size = 12, className = 'icon-svg-check') {
  return `<svg class="${className}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>`;
}

function getCheckCircleIconSvg(size = 14, className = 'icon-svg-check') {
  return `<svg class="${className}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg>`;
}

function updateRouteSheetDOM(data) {
  const pageTitle = document.getElementById('page-sheet-title');
  if (pageTitle) pageTitle.innerText = `Маршрутный лист выездного инженера: ${data.staff_id} (${data.display_name.split(' (')[0]})`;
  const pageMeta = document.getElementById('page-sheet-meta');
  if (pageMeta) pageMeta.innerText = `Сервисный центр: ${data.service_center_name} | Смена: ${data.shift.start} - ${data.shift.end} | Автомобиль: ${data.vehicle} | Дата: ${data.date}`;
  const pageSig = document.getElementById('page-sig-engineer-name');
  if (pageSig) pageSig.innerText = data.display_name.split(' (')[0];

  const visitStops = data.stops.filter(s => s.type === 'VISIT');
  const completedVisits = visitStops.filter(s => isStopCompleted(s));
  const totalVisits = visitStops.length;
  const pct = totalVisits ? Math.round((completedVisits.length / totalVisits) * 100) : 0;

  const progWrap = document.getElementById('page-sheet-progress-wrap');
  if (progWrap) {
    progWrap.innerHTML = `
      <span>Прогресс смены: <b>${completedVisits.length} из ${totalVisits}</b> нарядов выполнено (${pct}%)</span>
      <div class="sheet-progress-bar-bg">
        <div class="sheet-progress-bar-fill" style="width: ${pct}%;"></div>
      </div>
    `;
  }

  const nextVisit = visitStops.find(s => s.status === 'IN_PROGRESS') || visitStops.find(s => !isStopCompleted(s));
  const pendingRestock = (data.stops || []).find(s => s.type === 'RESTOCK' && !isStopCompleted(s));
  const btnLkCompleteRestock = document.getElementById('btn-lk-complete-restock');
  if (btnLkCompleteRestock) {
    btnLkCompleteRestock.style.display = pendingRestock ? 'inline-flex' : 'none';
  }
  const nextStopContainer = document.getElementById('sheet-next-stop-container');
  if (nextStopContainer) {
    if (nextVisit) {
      let eqStr = RU_TEXT.T_0024;
      if (nextVisit.required_equipment) {
        const eqParts = [];
        for (const [k, v] of Object.entries(nextVisit.required_equipment)) {
          if (v > 0) eqParts.push(`${v}x ${k}`);
        }
        if (eqParts.length) eqStr = eqParts.join(', ');
      }
      const nextOrderMeta = (contextData && contextData.orders) ? contextData.orders[nextVisit.order_id] : null;
      const phoneHtml = nextOrderMeta && nextOrderMeta.client_phone
        ? `<div class="next-stop-eq">Телефон: <b>${nextOrderMeta.client_phone}</b></div>` : '';
      const callStateHtml = nextVisit.called
        ? '<span class="badge badge-success" style="margin-left:6px;">Звонок подтверждён</span>'
        : (nextVisit.demo_called ? '<span class="badge badge-primary" style="margin-left:6px;">Демозвонок</span>' : '');

      nextStopContainer.innerHTML = `
        <div class="next-stop-card">
          <div class="next-stop-header">
            <span class="next-stop-tag">Текущий наряд${callStateHtml}</span>
            <span class="next-stop-slot">Слот: <b>${nextVisit.slot}</b></span>
          </div>
          <div class="next-stop-title">
            <a href="javascript:void(0)" onclick="openInspector('${nextVisit.order_id}')" class="clickable-id" title="Открыть карточку">${nextVisit.order_id}</a>
            <span class="next-stop-work">${nextVisit.work_type}</span>
          </div>
          <div class="next-stop-address">${nextVisit.address}</div>
          ${phoneHtml}
          <div class="next-stop-eq">Комплектация: <b>${eqStr}</b></div>
          <div class="next-stop-actions">
            <button type="button" class="btn btn-primary btn-sm" onclick="toggleCompleteOrder('${nextVisit.order_id}', '${data.staff_id}')" style="flex:1; font-weight:600;">
              ${getCheckCircleIconSvg(12)} Завершить наряд
            </button>
            <button type="button" class="btn btn-outline btn-sm" onclick="mobileVisitAction('${nextVisit.order_id}', 'call', '${data.staff_id}')" title="${nextVisit.called ? RU_TEXT.T_0037 : 'Зафиксировать контрольный звонок клиенту'}">Звонок клиенту</button>
            <button type="button" class="btn btn-outline btn-sm" onclick="mobileVisitAction('${nextVisit.order_id}', 'no-answer', '${data.staff_id}')" title="Отложить наряд и пересчитать график без простоя">Не отвечает</button>
            <button type="button" class="btn btn-outline btn-sm" onclick="requestEngineerRestock('${data.staff_id}')" title="Добавить заезд на склад СЦ в маршрут">На склад</button>
            ${pendingRestock ? `<button type="button" class="btn btn-outline btn-sm" onclick="completeEngineerRestock('${data.staff_id}')" style="border-color:#10b981; color:#047857;">Завершить СЦ</button>` : ''}
            <button type="button" class="btn btn-outline btn-sm" onclick="showOnAppMap('${nextVisit.order_id}', '${data.staff_id}')" title="Показать точку на интерактивной карте">На карте</button>
          </div>
        </div>
      `;
    } else {
      nextStopContainer.innerHTML = `
        <div class="next-stop-card all-done">
          <div class="next-stop-done-title">${getCheckCircleIconSvg(16)} Все наряды смены выполнены!</div>
          <div class="next-stop-done-sub">Маршрут завершен. Возврат в Сервисный центр <b>${data.service_center_name}</b> для сдачи оборудования.</div>
        </div>
      `;
    }
  }

  const eqRows = [
    { key: 'ONT', name: RU_TEXT.T_0025 },
    { key: 'ROUTER', name: RU_TEXT.T_0026 },
    { key: 'STB', name: RU_TEXT.T_0027 }
  ];
  const wh = data.warehouse_manifest || {};
  let eqHtml = '';
  eqRows.forEach(it => {
    const initLoad = (wh.initial_load && wh.initial_load[it.key]) || 0;
    const cons = (wh.day_consumption && wh.day_consumption[it.key]) || 0;
    const rem = Math.max(0, initLoad - cons);

    eqHtml += `
      <tr>
        <td data-label="Оборудование"><b>${it.name}</b></td>
        <td data-label="Загрузка" style="text-align:center;">${initLoad} шт.</td>
        <td data-label="Расход" style="text-align:center; color:#7700ff; font-weight:700;">${cons} шт.</td>
        <td data-label="Остаток" style="text-align:center; font-weight:600; color:#16a34a;">${rem} шт.</td>
      </tr>
    `;
  });

  const pageEqTbody = document.getElementById('page-sheet-equipment-rows');
  if (pageEqTbody) pageEqTbody.innerHTML = eqHtml;

  let stopsHtml = '';
  data.stops.forEach(s => {
    let eqStr = '-';
    if (s.required_equipment) {
      const eqParts = [];
      for (const [k, v] of Object.entries(s.required_equipment)) {
        if (v > 0) eqParts.push(`${v}x ${k}`);
      }
      if (eqParts.length) eqStr = eqParts.join(', ');
    }

    const isBreak = s.type === 'BREAK';
    const isDepot = s.type === 'DEPOT_START' || s.type === 'DEPOT_END' || s.type === 'RESTOCK';

    let typeBadge = s.work_type;
    if (isBreak) typeBadge = RU_TEXT.T_0028;
    if (isDepot) {
      if (s.type === 'DEPOT_START') typeBadge = RU_TEXT.T_0029;
      else if (s.type === 'RESTOCK') typeBadge = RU_TEXT.T_0030;
      else typeBadge = RU_TEXT.T_0031;
    }

    const ordCol = s.order_id ? `<a href="javascript:void(0)" class="clickable-id" onclick="openInspector('${s.order_id}')" title="Открыть карточку">${s.order_id}</a>` : '-';
    const addrCol = (s.address && s.address !== '-')
      ? `<div style="display:flex; align-items:center; justify-content:space-between; gap:6px;">
           <span>${s.address}</span>
           ${s.order_id ? `<button type="button" class="btn-map-loc" onclick="showOnAppMap('${s.order_id}', '${data.staff_id}')" title="${RU_TEXT.T_0032}">На карте</button>` : ''}
         </div>`
      : (s.address || '-');

    let statusCell = RU_TEXT.T_0033;
    if (isBreak) {
      statusCell = RU_TEXT.T_0034;
    } else if (s.type === 'RESTOCK') {
      statusCell = isStopCompleted(s)
        ? `<span class="badge badge-success">Выполнен</span>`
        : `<button type="button" class="btn-stop-status pending" onclick="completeEngineerRestock('${data.staff_id}')" title="Отметить пополнение в СЦ завершенным">Завершить заезд СЦ</button>`;
    } else if (s.type === 'VISIT') {
      const isDone = isStopCompleted(s);
      statusCell = isDone
        ? `<button type="button" class="btn-stop-status completed" onclick="toggleCompleteOrder('${s.order_id}', '${data.staff_id}')" title="Кликните, чтобы вернуть наряд в работу">${getCheckCircleIconSvg(12)} Выполнен</button>`
        : `<button type="button" class="btn-stop-status pending" onclick="toggleCompleteOrder('${s.order_id}', '${data.staff_id}')" title="Отметить наряд как выполненный">Завершить наряд</button>`;
    }

    stopsHtml += `
      <tr class="${isStopCompleted(s) ? 'row-completed' : ''}">
        <td data-label="№" style="text-align:center; font-weight:700;">${s.seq_label}</td>
        <td data-label="Время"><b>${s.start} - ${s.end}</b></td>
        <td data-label="Слот">${s.slot}</td>
        <td data-label="Наряд"><b>${ordCol}</b></td>
        <td data-label="Вид работ">${typeBadge}</td>
        <td data-label="Адрес">${addrCol}</td>
        <td data-label="Оборудование">${eqStr}</td>
        <td data-label="Статус выполнения">${statusCell}</td>
      </tr>
    `;
  });

  const pageStopsTbody = document.getElementById('page-sheet-stops-rows');
  if (pageStopsTbody) pageStopsTbody.innerHTML = stopsHtml;

  renderEngineerRouteMap(data);
}

function renderEngineerRouteMap(data) {
  const mapContainer = document.getElementById('engineer-route-map');
  if (!mapContainer || typeof L === 'undefined') return;
  const mapSeq = ++engineerMapReqSeq;

  if (!window.engineerRouteMap) {
    window.engineerRouteMap = L.map('engineer-route-map', {
      zoomControl: true,
      attributionControl: false
    }).setView([56.8450, 60.6050], 12);

    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19
    }).addTo(window.engineerRouteMap);
    attachOfflineStreetsLayer(window.engineerRouteMap);

    window.engineerRouteLayers = L.layerGroup().addTo(window.engineerRouteMap);
  } else {
    window.engineerRouteLayers.clearLayers();
  }

  const scCoords = DISTRICT_COORDS[data.service_center_id] || [56.8450, 60.6050];
  const visitStops = data.stops.filter(s => s.type === 'VISIT');
  const completedStops = visitStops.filter(s => isStopCompleted(s));
  const remainingStops = visitStops.filter(s => !isStopCompleted(s));

  const remPill = document.getElementById('sheet-map-rem-pill');
  if (remPill) {
    remPill.innerText = remainingStops.length > 0 ? `В маршруте: ${remainingStops.length} нарядов` : RU_TEXT.T_0036;
    remPill.style.background = remainingStops.length > 0 ? '#f5f3ff' : '#f0fdf4';
    remPill.style.color = remainingStops.length > 0 ? '#7700ff' : '#16a34a';
  }
  const donePill = document.getElementById('sheet-map-done-pill');
  if (donePill) donePill.innerText = `Выполнено: ${completedStops.length} (точки убраны из маршрута)`;

  const scIcon = L.divIcon({
    className: 'custom-sc-marker',
    html: `<div class="sc-pin-icon" title="Сервисный центр: ${data.service_center_name}">СЦ</div>`,
    iconSize: [28, 28],
    iconAnchor: [14, 14]
  });
  L.marker(scCoords, { icon: scIcon })
    .bindPopup(`<div style="font-size:12px; font-weight:700; color:#7700ff; margin-bottom:4px;">Сервисный центр</div><div style="font-size:11.5px; color:#1e293b;">${data.service_center_name}</div><div style="font-size:10.5px; color:#64748b; margin-top:2px;">Базовый узел выезда бригады</div>`)
    .addTo(window.engineerRouteLayers);

  const activeRouteCoords = [scCoords];
  let activeOrderSeq = 0;

  remainingStops.forEach(s => {
    activeOrderSeq++;
    const coords = getOrderCoordinates(s.order_id, scCoords);
    activeRouteCoords.push(coords);

    const pinIcon = L.divIcon({
      className: 'custom-order-pin',
      html: `<div class="order-pin-bubble" title="Наряд ${s.order_id}">${activeOrderSeq}</div>`,
      iconSize: [26, 26],
      iconAnchor: [13, 13]
    });

    const popupHtml = `
      <div class="eng-map-popup">
        <div class="eng-map-popup-hdr">
          <b>Наряд ${s.order_id}</b>
          <span class="eng-map-popup-seq">Точка ${activeOrderSeq}</span>
        </div>
        <div class="eng-map-popup-row"><b>Вид работ:</b> ${s.work_type}</div>
        <div class="eng-map-popup-row"><b>Адрес:</b> ${s.address}</div>
        <div class="eng-map-popup-row"><b>Слот:</b> ${s.slot}</div>
        <div class="eng-map-popup-act">
          <button type="button" class="btn btn-sm btn-success" onclick="toggleCompleteOrder('${s.order_id}', '${data.staff_id}')" style="width:100%; font-size:11px; padding:5px 8px; font-weight:600;">
            ${getCheckCircleIconSvg(12)} Завершить наряд
          </button>
        </div>
      </div>
    `;

    L.marker(coords, { icon: pinIcon })
      .bindPopup(popupHtml)
      .addTo(window.engineerRouteLayers);
  });

  if (activeRouteCoords.length > 1) {
    activeRouteCoords.push(scCoords);
    let routeLine = L.polyline(activeRouteCoords, {
      color: '#7700ff',
      weight: 4,
      opacity: 0.5,
      dashArray: '5, 8',
      lineJoin: 'round'
    }).addTo(window.engineerRouteLayers);

    fetch('/api/v1/routing/track', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ points: activeRouteCoords })
    })
      .then(r => r.json())
      .then(track => {
        if (mapSeq !== engineerMapReqSeq) return;
        if (track && track.coordinates && track.coordinates.length > 1 && window.engineerRouteLayers) {
          window.engineerRouteLayers.removeLayer(routeLine);
          const osrmLine = L.polyline(track.coordinates, {
            color: '#7700ff',
            weight: 5,
            opacity: 0.95,
            lineCap: 'round',
            lineJoin: 'round'
          }).addTo(window.engineerRouteLayers);

          const distKm = (track.distance_m / 1000).toFixed(1);
          const durMin = Math.round(track.duration_sec / 60);
          osrmLine.bindPopup(`
            <b>Маршрут по улицам Екатеринбурга (${track.provider || 'OSRM'})</b><br>
            Оставшийся пробег по дорогам: <b>${distKm} км</b><br>
            Расчетное время в движении: <b>${durMin} мин</b><br>
            <i>Учтены скоростные лимиты и дорожные развязки</i>
          `);
        }
      })
      .catch(err => console.warn('Engineer OSRM track error:', err));
  }

  if (activeRouteCoords.length > 0) {
    try {
      window.engineerRouteMap.fitBounds(L.latLngBounds(activeRouteCoords), {
        padding: [35, 35],
        maxZoom: 15
      });
    } catch (e) {
      console.warn('fitBounds error:', e);
    }
  }

  const btnRecenter = document.getElementById('btn-recenter-engineer-map');
  if (btnRecenter) {
    btnRecenter.onclick = () => {
      if (window.engineerRouteMap && activeRouteCoords.length > 0) {
        window.engineerRouteMap.fitBounds(L.latLngBounds(activeRouteCoords), { padding: [35, 35] });
      }
    };
  }

  setTimeout(() => {
    if (window.engineerRouteMap) window.engineerRouteMap.invalidateSize();
  }, 150);
}

function requestEngineerRestock(staffId) {
  const selectEl = document.getElementById('engineer-lk-staff-select');
  const targetStaffId = staffId || (selectEl ? selectEl.value : '') || activeSheetStaffId || 'ST-001';
  activeSheetStaffId = targetStaffId;
  fetch(`/api/v1/ui/routes/${encodeURIComponent(targetStaffId)}/restock`, { method: 'POST' })
    .then(async res => {
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      return data;
    })
    .then(data => {
      showToast(RU_TEXT.T_0250, RU_TEXT.T_0251, 'success', 10000, data.undo_token || null);
      activeSheetStaffId = targetStaffId;
      loadAllData().then(() => {
        if (currentActiveView !== 'sheet') loadRouteSheetData(targetStaffId);
      });
    })
    .catch(err => showToast('Ошибка заезда на склад', String(err.message || err), 'error'));
}

function completeEngineerRestock(staffId) {
  const selectEl = document.getElementById('engineer-lk-staff-select');
  const targetStaffId = staffId || (selectEl ? selectEl.value : '') || activeSheetStaffId || 'ST-001';
  activeSheetStaffId = targetStaffId;
  fetch(`/api/v1/ui/routes/${encodeURIComponent(targetStaffId)}/restock/complete`, { method: 'POST' })
    .then(async res => {
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      return data;
    })
    .then(() => {
      showToast('Заезд в СЦ завершен', `Складское пополнение для ${targetStaffId} отмечено выполненным`, 'success', 5000);
      loadAllData().then(() => {
        if (currentActiveView !== 'sheet') loadRouteSheetData(targetStaffId);
      });
    })
    .catch(err => showToast('Ошибка завершения заезда', String(err.message || err), 'error'));
}

function mobileVisitAction(orderId, action, staffId) {
  if (!orderId || !action) return;
  const selectEl = document.getElementById('engineer-lk-staff-select');
  const targetStaffId = staffId || (selectEl ? selectEl.value : '') || activeSheetStaffId || 'ST-001';
  activeSheetStaffId = targetStaffId;
  const needsDemoQuery = (action === 'call' || action === 'no-answer');
  const url = `/api/v1/ui/visits/${encodeURIComponent(orderId)}/${action}${needsDemoQuery ? '?demo=true' : ''}`;
  fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) })
    .then(async r => {
      const res = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(res.detail || `HTTP ${r.status}`);
      return res;
    })
    .then(res => {
      const actionLabels = {
        call: RU_TEXT.T_0248,
        arrive: RU_TEXT.T_0038,
        'no-answer': RU_TEXT.T_0249,
        finish: RU_TEXT.T_0040
      };
      let toastMsg = res.message || `Наряд ${orderId} обновлен`;
      let toastType = 'success';
      if (action === 'call') {
        toastMsg = `Контрольный звонок по наряду ${orderId} зафиксирован в системе`;
      } else if (action === 'no-answer') {
        toastMsg = `Наряд ${orderId} отложен. График мастера пересчитан без простоя.`;
        toastType = 'warning';
      }
      showToast(actionLabels[action] || RU_TEXT.T_0041, toastMsg, toastType);
      activeSheetStaffId = targetStaffId;
      loadAllData().then(() => {
        if (currentActiveView !== 'sheet') loadRouteSheetData(targetStaffId);
      });
    })
    .catch(err => showToast(RU_TEXT.T_0042, String(err.message || err), 'error'));
}

function toggleCompleteOrder(orderId, staffId) {
  if (!orderId) return;
  const selectEl = document.getElementById('engineer-lk-staff-select');
  const targetStaffId = staffId || (selectEl ? selectEl.value : '') || activeSheetStaffId || '';
  if (targetStaffId) activeSheetStaffId = targetStaffId;

  fetch(`/api/v1/orders/${orderId}/toggle-complete`, { method: 'POST' })
    .then(r => r.json())
    .then(res => {
      const isDone = res.completed;
      showToast(
        isDone ? RU_TEXT.T_0043 : RU_TEXT.T_0041,
        isDone ? `Наряд ${orderId} выполнен. Точка исключена из маршрута инженера.` : `Наряд ${orderId} возвращен в активный маршрут.`,
        isDone ? 'success' : 'info',
        10000,
        res.undo_token || null
      );
      if (targetStaffId) activeSheetStaffId = targetStaffId;
      loadAllData().then(() => {
        if (targetStaffId && currentActiveView !== 'sheet') loadRouteSheetData(targetStaffId);
        if (activeInspectorOrderId === orderId) openInspector(orderId);
      });
    })
    .catch(err => showToast(RU_TEXT.T_0044, String(err), 'error'));
}

function showOnAppMap(orderId, staffId) {
  if (!orderId) return;

  const oMeta = (contextData && contextData.orders) ? contextData.orders[orderId] : null;
  let lat = oMeta ? oMeta.lat : null;
  let lon = oMeta ? oMeta.lon : null;
  let address = oMeta ? oMeta.address : RU_TEXT.T_0045;

  if (!lat || !lon) {
    if (currentPlanData) {
      for (const r of currentPlanData.routes) {
        for (const s of r.stops) {
          if (s.order_id === orderId) {
            const center = DISTRICT_COORDS[r.service_center_id] || [56.8450, 60.6050];
            const c = getOrderCoordinates(orderId, center);
            lat = c[0];
            lon = c[1];
            break;
          }
        }
        if (lat) break;
      }
    }
  }

  if (!lat || !lon) {
    lat = 56.8450;
    lon = 60.6050;
  }

  if (currentRole === 'engineer' || currentActiveView === 'sheet') {
    const mapCard = document.querySelector('.sheet-map-card');
    if (mapCard) {
      mapCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    if (window.engineerRouteMap) {
      window.engineerRouteMap.invalidateSize();
      window.engineerRouteMap.flyTo([lat, lon], 16, { animate: true, duration: 0.8 });

      L.popup({ offset: [0, -10] })
        .setLatLng([lat, lon])
        .setContent(`
          <div class="eng-map-popup">
            <div class="eng-map-popup-hdr">
              <b>Наряд ${orderId}</b>
              <span class="eng-map-popup-seq">Точка на карте</span>
            </div>
            <div class="eng-map-popup-row" style="margin-top:4px;"><b>Адрес:</b> ${address}</div>
            <div class="eng-map-popup-act">
              <button type="button" class="btn btn-sm btn-success" onclick="toggleCompleteOrder('${orderId}', '${staffId || activeSheetStaffId}')" style="width:100%; font-size:11px; padding:5px 8px; font-weight:600;">
                ${getCheckCircleIconSvg(12)} Завершить наряд
              </button>
            </div>
          </div>
        `)
        .openOn(window.engineerRouteMap);
    }
    showToast(RU_TEXT.T_0046, `${orderId}: ${address}`, 'info', 2500);
    return;
  }

  switchView('map');

  if (staffId && staffId !== 'all') {
    const crewSelect = document.getElementById('filter-crew');
    if (crewSelect) {
      crewSelect.value = staffId;
      applyFilters();
      updateSidebarEngineerCard(staffId);
    }
  }

  if (map) {
    setTimeout(() => {
      map.invalidateSize();
      map.flyTo([lat, lon], 16, { animate: true, duration: 1.0 });

      L.popup({ offset: [0, -10] })
        .setLatLng([lat, lon])
        .setContent(`
          <div style="font-family:inherit; padding:6px 8px; min-width:180px;">
            <div style="font-weight:700; color:#7700ff; margin-bottom:4px; font-size:12px;">${orderId}</div>
            <div style="font-size:11.5px; color:#1e293b; margin-bottom:6px; line-height:1.35;">${address}</div>
            <button type="button" class="btn btn-primary btn-sm" onclick="openInspector('${orderId}')" style="width:100%; font-size:10.5px; padding:4px 6px;">Открыть карточку наряда</button>
          </div>
        `)
        .openOn(map);
    }, 220);
  }

  showToast(RU_TEXT.T_0047, `${orderId}: ${address}`, 'info', 2500);
}

function downloadSheetPdf() {
  const staffId = activeSheetStaffId || 'ST-001';
  const element = document.querySelector('.route-sheet-view-card');
  if (!element) {
    showToast(RU_TEXT.T_0048, RU_TEXT.T_0049, 'error');
    return;
  }

  showToast(RU_TEXT.T_0050, `Формирование маршрутного листа ${staffId} в формате PDF...`, 'info');

  const dateStr = getActivePlanDate();
  const pdfFilename = `Маршрутный_лист_${staffId}_${dateStr}.pdf`;

  const opt = {
    margin: [8, 8, 10, 8],
    filename: pdfFilename,
    image: { type: 'jpeg', quality: 0.98 },
    html2canvas: { scale: 2, useCORS: true, logging: false },
    jsPDF: { unit: 'mm', format: 'a4', orientation: 'portrait' }
  };

  if (typeof html2pdf !== 'undefined') {
    const hiddenEls = element.querySelectorAll('.sheet-table th:nth-child(8), .sheet-table td:nth-child(8), #page-sheet-progress-wrap, .sheet-map-status-pills');
    const prevDisplay = Array.from(hiddenEls).map(el => ({ el, val: el.style.display }));
    hiddenEls.forEach(el => { el.style.display = 'none'; });
    html2pdf().set(opt).from(element).save().then(() => {
      showToast(RU_TEXT.T_0051, `Маршрутный лист сохранен: ${pdfFilename}`, 'success');
    }).catch(err => {
      console.warn('html2pdf fallback to print:', err);
      window.print();
    }).finally(() => {
      prevDisplay.forEach(({ el, val }) => { el.style.display = val; });
    });
  } else {
    window.print();
  }
}

function loadAllData() {
  return Promise.all([
    fetch('/api/v1/context').then(r => r.json()),
    fetch('/api/v1/plan').then(r => r.json())
  ])
    .then(([ctx, plan]) => {
      contextData = ctx;
      currentPlanData = plan;
      renderAll(plan);
      return plan;
    })
    .catch(err => console.error(RU_TEXT.T_0052, err));
}

function renderAll(plan) {
  currentPlanData = plan;
  const bulkNextLbl = document.getElementById('bulk-next-day-label');
  if (bulkNextLbl) bulkNextLbl.innerText = `на ${getNextWorkdayLabel()}`;
  const fmNextLbl = document.getElementById('fm-next-day-label');
  if (fmNextLbl) fmNextLbl.innerText = getNextWorkdayLabel();

  renderKPIs(plan);
  refreshValidationBadge();
  updateCrewFilterOptions(plan.routes);
  populateSheetStaffSelects(plan.routes);
  populateEngineerRoleSelect();
  populateSplitCrewSelect(plan.routes, plan.alerts || []);
  updateSidebarEngineerCard(document.getElementById('filter-crew').value);
  applyFilters();
  renderGantt(plan.routes);
  renderAlerts(plan.alerts || []);
  renderUnassigned(plan.unassigned || []);
  renderPGP(plan.pgp_groups || []);
  if (splitMap) renderRoutesOnSplitMap(plan, activeSplitStaffId);
  if (currentActiveView === 'sheet') {
    loadRouteSheetData(activeSheetStaffId || 'ST-001');
  }
  syncGanttShiftTimeControls();
  refreshOrderBoard();
}

function computePlanKpiStats(plan) {
  const s = (plan && plan.summary) || {};
  const report = (currentValidationData && plan && currentValidationData.plan_version === plan.plan_version)
    ? currentValidationData : null;
  const total = Number(s.orders_total ?? 0);
  const planned = Number(s.visits_planned ?? 0);
  const pgp = Number(s.pgp_orders ?? (plan && plan.pgp_groups ? plan.pgp_groups.length : 0));
  const unassigned = (plan && plan.unassigned) ? plan.unassigned.length : Math.max(0, total - planned - pgp);
  const eligibleTotal = Math.max(0, total - pgp);
  const fallbackSlaVal = eligibleTotal > 0 ? Math.min(100, (planned / eligibleTotal) * 100) : 100.0;
  const slaVal = (report && report.weighted_total > 0)
    ? (100 * report.weighted_on_time / report.weighted_total)
    : fallbackSlaVal;

  let inSlotCount = 0;
  let visitsCount = 0;
  (plan && plan.routes ? plan.routes : []).forEach(r => {
    (r.stops || []).forEach(st => {
      if (st.type !== 'VISIT' || st.status === 'CANCELLED') return;
      if (st.order_id && st.order_id.startsWith('VLS-PARTNER-')) return;
      visitsCount++;
      const oMeta = (contextData && contextData.orders) ? contextData.orders[st.order_id] : null;
      if (oMeta && oMeta.slot) {
        const arrM = parseTimeToMin(st.start || st.arrival);
        const sStart = parseTimeToMin(oMeta.slot.start);
        const sEnd = parseTimeToMin(oMeta.slot.end);
        if (arrM >= sStart && arrM <= sEnd) inSlotCount++;
      }
    });
  });
  const fallbackInSlotVal = visitsCount > 0 ? (inSlotCount / visitsCount) * 100 : 100.0;
  const inSlotVal = (report && typeof report.in_slot === 'number')
    ? (planned > 0 ? Math.min(100, (report.in_slot / planned) * 100) : 100.0)
    : fallbackInSlotVal;

  const scMetaDict = (contextData && contextData.service_centers) || {};
  const scMap = {};
  const defaultScNames = {
    'SC-01': RU_TEXT.T_0053,
    'SC-02': RU_TEXT.T_0054,
    'SC-03': RU_TEXT.T_0055,
    'SC-04': RU_TEXT.T_0056,
    'SC-05': RU_TEXT.T_0057
  };
  Object.keys(defaultScNames).forEach(scId => {
    const name = (scMetaDict[scId] && scMetaDict[scId].name) || defaultScNames[scId];
    scMap[scId] = { scId, name, crews: 0, orders: 0, totalOrders: 0, pgpOrders: 0, travelSec: 0 };
  });

  const pgpOrderIds = new Set();
  if (plan && plan.pgp_groups) {
    plan.pgp_groups.forEach(g => {
      if (Array.isArray(g.order_ids)) g.order_ids.forEach(id => pgpOrderIds.add(id));
      else if (g.order_id) pgpOrderIds.add(g.order_id);
    });
  }

  if (contextData && contextData.orders) {
    Object.values(contextData.orders).forEach(o => {
      if (o.order_id && o.order_id.startsWith('VLS-PARTNER-')) return;
      const scId = getOrderDistrict(o);
      if (!scMap[scId]) scMap[scId] = { scId, name: scId, crews: 0, orders: 0, totalOrders: 0, pgpOrders: 0, travelSec: 0 };
      scMap[scId].totalOrders++;
      if (pgpOrderIds.has(o.order_id)) scMap[scId].pgpOrders++;
    });
  }

  (plan && plan.routes ? plan.routes : []).forEach(r => {
    let scId = r.service_center_id;
    if (!scId && contextData && contextData.staff && contextData.staff[r.staff_id]) {
      scId = contextData.staff[r.staff_id].service_center_id;
    }
    if (!scId || !scMap[scId]) return;
    scMap[scId].crews++;
    const visits = (r.stops || []).filter(st =>
      st.type === 'VISIT' && st.status !== 'CANCELLED' && !(st.order_id && st.order_id.startsWith('VLS-PARTNER-'))
    ).length;
    scMap[scId].orders += visits;
    const routeTravel = (r.totals && r.totals.travel_sec != null) ? r.totals.travel_sec : (r.travel_sec || 0);
    scMap[scId].travelSec += Number(routeTravel);
  });

  Object.values(scMap).forEach(sc => {
    const valSc = report && report.service_center_sla && report.service_center_sla[sc.scId];
    if (valSc && valSc.weighted_total > 0) {
      sc.slaPct = (100 * valSc.weighted_on_time) / valSc.weighted_total;
      return;
    }
    const scEligible = Math.max(0, sc.totalOrders - sc.pgpOrders);
    if (scEligible > 0) {
      sc.slaPct = Math.min(100, (sc.orders / scEligible) * 100);
    } else {
      sc.slaPct = sc.crews > 0 ? 100.0 : 0.0;
    }
  });

  const travelSec = Number((report && report.travel_sec != null) ? report.travel_sec : (s.travel_sec_total ?? 0));
  const cvRaw = Number((report && report.utilization_cv != null) ? report.utilization_cv : (s.utilization_cv ?? 0));

  return {
    total,
    planned,
    pgp,
    unassigned,
    slaVal,
    slaPct: slaVal.toFixed(1),
    inSlotVal,
    inSlotPct: inSlotVal.toFixed(1),
    travelSec,
    cvRaw,
    cv: cvRaw.toFixed(3),
    lateRisks: ((plan && plan.alerts) || []).filter(a => a.type === 'LATE_RISK').length,
    scList: Object.values(scMap),
    report
  };
}

function renderKpiDelta(metric, current, initial, digits, unit, higherIsBetter = true) {
  const element = document.getElementById(`kpi-delta-${metric}`);
  if (!element) return;
  if (!Number.isFinite(current) || !Number.isFinite(initial)) {
    element.hidden = true;
    return;
  }
  const delta = Number((Number(current.toFixed(digits)) - Number(initial.toFixed(digits))).toFixed(digits));
  element.hidden = delta === 0;
  if (delta === 0) return;
  const formatter = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: digits, signDisplay: 'exceptZero' });
  const isGood = higherIsBetter ? delta > 0 : delta < 0;
  element.className = `kpi-delta ${isGood ? 'positive' : 'negative'}`;
  element.textContent = `${formatter.format(delta)}${unit} к исходному плану`;
}

function renderKPIs(plan) {
  const kpi = computePlanKpiStats(plan);

  const kpiSla = document.getElementById('kpi-sla');
  if (kpiSla) kpiSla.innerText = `${kpi.slaPct}%`;

  const kpiInSlot = document.getElementById('kpi-in-slot-pct');
  if (kpiInSlot) kpiInSlot.innerText = `${kpi.inSlotPct}%`;

  const kpiAssigned = document.getElementById('kpi-assigned');
  if (kpiAssigned) kpiAssigned.innerText = `${kpi.planned} / ${kpi.total}`;

  const kpiTravel = document.getElementById('kpi-travel');
  if (kpiTravel) kpiTravel.innerText = `${Math.round(kpi.travelSec / 60)} мин`;

  const kpiCv = document.getElementById('kpi-cv');
  if (kpiCv) kpiCv.innerText = kpi.cv;

  const kpiPgp = document.getElementById('kpi-pgp');
  if (kpiPgp) kpiPgp.innerText = `${kpi.pgp} нарядов`;

  const kpiRisks = document.getElementById('kpi-risks');
  if (kpiRisks) kpiRisks.innerText = kpi.lateRisks;

  const navPgp = document.getElementById('nav-pgp-count');
  if (navPgp) navPgp.innerText = (plan && plan.pgp_groups) ? plan.pgp_groups.length : 0;

  const navUnassigned = document.getElementById('nav-unassigned-count');
  if (navUnassigned) navUnassigned.innerText = kpi.unassigned;

  renderKpiDashboard(plan);
}

function renderKpiDashboard(plan) {
  if (!plan) return;
  const kpi = computePlanKpiStats(plan);
  const travelMin = Math.round(kpi.travelSec / 60);
  const travelHours = (kpi.travelSec / 3600).toFixed(1);

  const barSla = document.getElementById('kpi-dash-bar-sla');
  if (barSla) barSla.style.width = `${Math.min(100, Math.max(0, kpi.slaVal))}%`;

  const baseline = kpi.report && kpi.report.initial_kpis;
  const initialSla = baseline && baseline.weighted_total
    ? (100 * baseline.weighted_on_time) / baseline.weighted_total
    : null;
  renderKpiDelta('sla', (kpi.report && kpi.report.weighted_total) ? kpi.slaVal : null, initialSla, 1, ' п. п.', true);
  renderKpiDelta('travel', travelMin, baseline ? Math.round(baseline.travel_sec / 60) : null, 0, ' мин', false);
  renderKpiDelta('cv', kpi.report ? kpi.cvRaw : null, baseline ? baseline.utilization_cv : null, 3, '', false);

  const badgeSla = document.getElementById('kpi-badge-sla');
  if (badgeSla) {
    if (kpi.slaVal >= 85) {
      badgeSla.className = 'badge badge-success';
      badgeSla.innerText = RU_TEXT.T_0232;
    } else if (kpi.slaVal >= 60) {
      badgeSla.className = 'badge badge-primary';
      badgeSla.innerText = RU_TEXT.T_0233;
    } else {
      badgeSla.className = 'badge badge-warning';
      badgeSla.innerText = RU_TEXT.T_0234;
    }
  }

  const barAssigned = document.getElementById('kpi-dash-bar-assigned');
  if (barAssigned) barAssigned.style.width = `${Math.min(100, Math.round((kpi.planned / (kpi.total || 1)) * 100))}%`;

  const subAssigned = document.getElementById('kpi-assigned-sub');
  if (subAssigned) subAssigned.innerText = `${kpi.planned} нарядов`;

  const subUnassigned = document.getElementById('kpi-unassigned-sub');
  if (subUnassigned) subUnassigned.innerText = `${kpi.unassigned}`;

  const travelHoursEl = document.getElementById('kpi-travel-hours');
  if (travelHoursEl) travelHoursEl.innerText = `~${travelHours} ч`;

  const barTravel = document.getElementById('kpi-dash-bar-travel');
  if (barTravel) barTravel.style.width = `${Math.min(100, Math.round((travelMin / 2500) * 100))}%`;

  const barCv = document.getElementById('kpi-dash-bar-cv');
  if (barCv) barCv.style.width = `${Math.min(100, Math.max(10, Math.round((1 - Math.min(1, Number(kpi.cv))) * 100)))}%`;

  const barPgp = document.getElementById('kpi-dash-bar-pgp');
  if (barPgp) barPgp.style.width = `${Math.min(100, Math.round((kpi.pgp / (kpi.total || 1)) * 100))}%`;

  const pgpGroups = Array.isArray(plan.pgp_groups) ? plan.pgp_groups : [];
  let workSec = 0;
  let visitCount = 0;
  let breakSec = 0;
  let restockSec = 0;
  const routes = Array.isArray(plan.routes) ? plan.routes : [];
  routes.forEach(r => {
    (r.stops || []).forEach(st => {
      const diff = parseTimeToMin(st.end) - parseTimeToMin(st.start);
      if (diff <= 0) return;
      if (st.type === 'VISIT' && st.status !== 'CANCELLED') {
        workSec += diff * 60;
        visitCount += 1;
      } else if (st.type === 'BREAK') {
        breakSec += diff * 60;
      } else if (st.type === 'RESTOCK') {
        restockSec += diff * 60;
      }
    });
  });

  const avgVisitHours = visitCount > 0 ? (workSec / 3600) / visitCount : 1.0;
  const pgpSavedVisits = pgpGroups.reduce((acc, g) => acc + Math.max(0, ((g.order_ids || []).length) - 1), 0);
  const pgpSavedHours = pgpSavedVisits * avgVisitHours;
  const badgePgpEl = document.getElementById('kpi-badge-pgp');
  if (badgePgpEl) badgePgpEl.innerText = `Экономия ~${pgpSavedHours.toFixed(1)} ч`;
  const notePgpEl = document.getElementById('kpi-pgp-note');
  if (notePgpEl) notePgpEl.innerText = `${pgpGroups.length} узлов доступа объединены в станционные выезды`;

  const barRisks = document.getElementById('kpi-dash-bar-risks');
  const badgeRisks = document.getElementById('kpi-badge-risks');
  if (badgeRisks) {
    if (kpi.lateRisks > 0) {
      badgeRisks.className = 'badge badge-danger';
      badgeRisks.innerText = `${kpi.lateRisks} под риском`;
      if (barRisks) {
        barRisks.className = 'kpi-dash-progress-bar bg-danger';
        barRisks.style.width = '100%';
      }
    } else {
      badgeRisks.className = 'badge badge-success';
      badgeRisks.innerText = RU_TEXT.T_0058;
      if (barRisks) {
        barRisks.className = 'kpi-dash-progress-bar bg-success';
        barRisks.style.width = '100%';
      }
    }
  }

  const scTableBody = document.getElementById('kpi-sc-table-rows');
  if (scTableBody) {
    let rowsHtml = '';
    kpi.scList.forEach(sc => {
      const avgOrders = sc.crews > 0 ? (sc.orders / sc.crews).toFixed(1) : '0.0';
      const travelMins = Math.round(sc.travelSec / 60);
      const scSlaStr = sc.slaPct.toFixed(1);
      const slaClass = sc.slaPct >= 80 ? 'badge-success' : (sc.slaPct >= 50 ? 'badge-warning' : 'badge-danger');
      rowsHtml += `<tr>
        <td data-label="Сервисный центр"><b>${sc.name}</b></td>
        <td data-label="Бригады" style="text-align:center;"><b>${sc.crews}</b> инж.</td>
        <td data-label="Наряды в плане" style="text-align:center;"><span class="badge badge-primary">${sc.orders}</span></td>
        <td data-label="Ср. загрузка" style="text-align:center;">${avgOrders} наряда</td>
        <td data-label="Время в пути" style="text-align:center;">${travelMins} мин</td>
        <td data-label="SLA района" style="text-align:center;"><span class="badge ${slaClass}">${scSlaStr}%</span></td>
      </tr>`;
    });
    scTableBody.innerHTML = rowsHtml;
  }

  const activeShiftRoutes = routes.filter(r => r.status !== 'UNAVAILABLE');
  const crewCount = activeShiftRoutes.length || routes.length || 1;
  const avgCrewTravelMin = Math.round(travelMin / crewCount);

  const travelNoteEl = document.getElementById('kpi-travel-note');
  if (travelNoteEl) {
    travelNoteEl.innerHTML = `${RU_TEXT.T_0235}<b id="kpi-travel-hours">~${travelHours} ч</b>${RU_TEXT.T_0236}${avgCrewTravelMin}${RU_TEXT.T_0237}`;
  }
  const cvNoteEl = document.getElementById('kpi-cv-note');
  if (cvNoteEl) {
    cvNoteEl.innerText = `${RU_TEXT.T_0238}${crewCount}${RU_TEXT.T_0239}`;
  }
  const risksNoteEl = document.getElementById('kpi-risks-note');
  if (risksNoteEl) {
    risksNoteEl.innerText = kpi.lateRisks > 0 ? `${RU_TEXT.T_0240}${kpi.lateRisks}` : RU_TEXT.T_0241;
  }

  let totalShiftHours = 0;
  activeShiftRoutes.forEach(r => {
    const stObj = (typeof staffLookup !== 'undefined' && staffLookup) ? staffLookup[r.staff_id] : null;
    if (stObj && stObj.shift && stObj.shift.start && stObj.shift.end) {
      const shDiff = parseTimeToMin(stObj.shift.end) - parseTimeToMin(stObj.shift.start);
      totalShiftHours += shDiff > 0 ? shDiff / 60 : 9;
    } else {
      totalShiftHours += 9;
    }
  });
  totalShiftHours = Math.max(1, totalShiftHours);

  const timeTitleEl = document.getElementById('kpi-time-title');
  if (timeTitleEl) {
    timeTitleEl.innerText = `Структура фонда рабочего времени (${totalShiftHours.toFixed(0)} чел-час на ${crewCount} инж.)`;
  }

  const updateTimeRow = (textId, barId, sec) => {
    const hours = sec / 3600;
    const pct = Math.min(100, (hours / totalShiftHours) * 100);
    const textEl = document.getElementById(textId);
    if (textEl) textEl.innerText = `${hours.toFixed(1)} ч (${pct.toFixed(1)}%)`;
    const barEl = document.getElementById(barId);
    if (barEl) barEl.style.width = `${pct.toFixed(1)}%`;
  };

  updateTimeRow('kpi-time-work', 'kpi-bar-work', workSec);
  updateTimeRow('kpi-time-travel', 'kpi-bar-travel', kpi.travelSec);
  updateTimeRow('kpi-time-break', 'kpi-bar-break', breakSec);
  updateTimeRow('kpi-time-restock', 'kpi-bar-restock', restockSec);
}

function exportKpiSummaryCsv() {
  if (!currentPlanData) {
    showToast(RU_TEXT.T_0059, RU_TEXT.T_0060, 'error');
    return;
  }
  const kpi = computePlanKpiStats(currentPlanData);
  const travelMin = Math.round(kpi.travelSec / 60);
  const crewsCount = (currentPlanData.routes || []).length;

  let csv = '\uFEFF';
  csv += RU_TEXT.T_0061;
  csv += `SLA смены (план / доступные);${kpi.slaPct}%;Фактическое покрытие\n`;
  csv += `Точность клиентских слотов (In-Slot);${kpi.inSlotPct}%;Визиты в окне\n`;
  csv += `Распределено нарядов;${kpi.planned} из ${kpi.total};Бригад в смене: ${crewsCount}\n`;
  csv += `Суммарный пробег (время в пути);${travelMin} мин;Матрица переездов\n`;
  csv += `Балансировка нагрузки (CV);${kpi.cv};Коэффициент вариации\n`;
  csv += `Групповые аварии ПГП;${kpi.pgp} нарядов;Кластеризация на узлах доступа\n`;
  csv += `Оперативные риски;${kpi.lateRisks};LATE_RISK по плану\n\n`;
  csv += RU_TEXT.T_0062;
  kpi.scList.forEach(sc => {
    const avgOrders = sc.crews > 0 ? (sc.orders / sc.crews).toFixed(1) : '0.0';
    const travelMins = Math.round(sc.travelSec / 60);
    csv += `${sc.name};${sc.crews};${sc.orders};${avgOrders};${travelMins};${sc.slaPct.toFixed(1)}%\n`;
  });

  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  const dStr = getActivePlanDate();
  a.download = `RTC_KPI_Summary_${dStr}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  showToast(RU_TEXT.T_0063, RU_TEXT.T_0064, 'success', 3000);
}

function updateCrewFilterOptions(routes) {
  const select = document.getElementById('filter-crew');
  if (!select) return;
  const currVal = select.value;
  select.innerHTML = RU_TEXT.T_0065 + routes.length + ')</option>';

  routes.forEach(r => {
    const visits = r.stops.filter(s => s.type === 'VISIT').length;
    const staffMeta = (contextData && contextData.staff) ? contextData.staff[r.staff_id] : null;
    const staffName = staffMeta ? staffMeta.display_name.split(' (')[0] : r.staff_id;
    const opt = document.createElement('option');
    opt.value = r.staff_id;
    opt.innerText = `${r.staff_id}: ${staffName} (${visits} визитов)`;
    select.appendChild(opt);
  });
  select.value = currVal || 'all';
}

function populateSheetStaffSelects(routes) {
  populateEngineerRoleSelect();
}

function updateCrewFilterByDistrict() {
  const dist = document.getElementById('filter-district').value;
  const crewSelect = document.getElementById('filter-crew');
  if (!crewSelect || !currentPlanData) return;

  crewSelect.innerHTML = RU_TEXT.T_0066;
  currentPlanData.routes.forEach(r => {
    if (dist === 'all' || r.service_center_id === dist) {
      const visits = r.stops.filter(s => s.type === 'VISIT').length;
      const staffMeta = (contextData && contextData.staff) ? contextData.staff[r.staff_id] : null;
      const staffName = staffMeta ? staffMeta.display_name.split(' (')[0] : r.staff_id;
      const opt = document.createElement('option');
      opt.value = r.staff_id;
      opt.innerText = `${r.staff_id}: ${staffName} (${visits} виз.)`;
      crewSelect.appendChild(opt);
    }
  });
  crewSelect.value = 'all';
  updateSidebarEngineerCard('all');
}

function updateSidebarEngineerCard(staffId) {
  let targetId = staffId;
  if (!targetId || targetId === 'all') {
    targetId = 'ST-001';
  }

  const staffMeta = (contextData && contextData.staff) ? contextData.staff[targetId] : null;
  const nameEl = document.getElementById('sb-eng-name');
  const metaEl = document.getElementById('sb-eng-meta');
  const shiftEl = document.getElementById('sb-eng-shift');
  const visitsEl = document.getElementById('sb-eng-visits');
  const scEl = document.getElementById('sb-eng-sc');
  const carEl = document.getElementById('sb-eng-car');

  if (staffMeta) {
    if (nameEl) nameEl.innerText = staffMeta.display_name;
    const prof = (staffMeta.profiles && staffMeta.profiles.includes('INSTALL')) ? RU_TEXT.T_0067 : RU_TEXT.T_0068;
    if (metaEl) metaEl.innerText = `${prof} | ${targetId}`;
    if (shiftEl) shiftEl.innerText = `${staffMeta.shift.start} - ${staffMeta.shift.end}`;
    if (scEl) scEl.innerText = DISTRICT_NAMES[staffMeta.service_center_id] || staffMeta.service_center_id;
    if (carEl) carEl.innerText = RU_TEXT.T_0069;
  }

  if (currentPlanData && currentPlanData.routes) {
    const route = currentPlanData.routes.find(r => r.staff_id === targetId);
    const visitCount = route ? route.stops.filter(s => s.type === 'VISIT').length : 0;
    if (visitsEl) visitsEl.innerText = `${visitCount} наряда`;
  }
}

function applyFilters() {
  if (!currentPlanData) return;
  const district = document.getElementById('filter-district').value;
  const staffId = document.getElementById('filter-crew').value;

  renderRoutesOnMap(currentPlanData, district, staffId);
}

function parseTimeToMin(isoOrTime) {
  if (!isoOrTime) return 0;
  const timeStr = isoOrTime.includes('T') ? isoOrTime.split('T')[1].substring(0, 5) : isoOrTime.substring(0, 5);
  const [h, m] = timeStr.split(':').map(Number);
  return h * 60 + m;
}

function getOrderCoordinates(orderId, fallbackCenter) {
  let lat = fallbackCenter ? fallbackCenter[0] : 56.8450;
  let lon = fallbackCenter ? fallbackCenter[1] : 60.6050;
  let address = '';

  if (contextData && contextData.orders && contextData.orders[orderId]) {
    const o = contextData.orders[orderId];
    if (typeof o.lat === 'number' && typeof o.lon === 'number' && o.lat > 50 && o.lon > 50) {
      lat = o.lat;
      lon = o.lon;
      address = o.address || '';
    }
  }

  if (lat >= 56.820 && lat <= 56.890 && lon < 60.548) {
    if (address.includes(RU_TEXT.T_0070)) {
      lat = 56.8520 + (Math.abs(hashString(address + 'lat')) % 90) * 0.0001;
      lon = 60.5550 + (Math.abs(hashString(address + 'lon')) % 70) * 0.0001;
    } else if (address.includes(RU_TEXT.T_0071)) {
      lat = 56.8550 + (Math.abs(hashString(address + 'lat')) % 100) * 0.0001;
      lon = 60.5580 + (Math.abs(hashString(address + 'lon')) % 80) * 0.0001;
    } else if (address.includes(RU_TEXT.T_0072)) {
      lat = 56.8510 + (Math.abs(hashString(address + 'lat')) % 30) * 0.0001;
      lon = 60.5650 + (Math.abs(hashString(address + 'lon')) % 100) * 0.0001;
    } else if (address.includes(RU_TEXT.T_0073)) {
      lat = 56.8340 + (Math.abs(hashString(address + 'lat')) % 40) * 0.0001;
      lon = 60.5520 + (Math.abs(hashString(address + 'lon')) % 150) * 0.0001;
    } else if (address.includes(RU_TEXT.T_0074)) {
      lat = 56.8280 + (Math.abs(hashString(address + 'lat')) % 40) * 0.0001;
      lon = 60.5500 + (Math.abs(hashString(address + 'lon')) % 150) * 0.0001;
    } else if (address.includes(RU_TEXT.T_0075)) {
      lat = 56.8250 + (Math.abs(hashString(address + 'lat')) % 80) * 0.0001;
      lon = 60.5480 + (Math.abs(hashString(address + 'lon')) % 50) * 0.0001;
    } else if (address.includes(RU_TEXT.T_0076)) {
      lat = 56.8380 + (Math.abs(hashString(address + 'lat')) % 45) * 0.0001;
      lon = 60.5510 + (Math.abs(hashString(address + 'lon')) % 80) * 0.0001;
    } else {
      lat = 56.8420 + (Math.abs(hashString(address + 'lat')) % 60) * 0.0001;
      lon = 60.5550 + (Math.abs(hashString(address + 'lon')) % 80) * 0.0001;
    }
  }

  else if (lat >= 56.837 && lat <= 56.848 && lon >= 60.596 && lon <= 60.6065) {
    if (address.includes(RU_TEXT.T_0077)) {
      lat = 56.8340 + (Math.abs(hashString(address + 'lat')) % 70) * 0.0001;
      lon = 60.6075 + (Math.abs(hashString(address)) % 35) * 0.0001;
    } else if (address.includes(RU_TEXT.T_0078)) {
      lat = 56.8250 + (Math.abs(hashString(address)) % 90) * 0.0001;
      lon = 60.5950 + (Math.abs(hashString(address + 'lon')) % 30) * 0.0001;
    } else {
      lon = 60.6075;
    }
  }

  return [lat, lon];
}

function renderRoutesOnMap(plan, selectedDistrict, selectedStaffId) {
  if (!map) return;

  routeLayers.forEach(l => map.removeLayer(l));
  markerLayers.forEach(l => map.removeLayer(l));
  scMarkers.forEach(l => map.removeLayer(l));
  routeLayers = [];
  markerLayers = [];
  scMarkers = [];

  Object.keys(DISTRICT_COORDS).forEach(scId => {
    if (selectedDistrict !== 'all' && selectedDistrict !== scId) return;

    const coords = DISTRICT_COORDS[scId];
    const scName = DISTRICT_NAMES[scId] || scId;
    const crewsInSc = plan.routes.filter(r => r.service_center_id === scId).length;

    const scIcon = L.divIcon({
      html: `<div class="map-pin pin-sc" title="${scName}">${scId.replace('SC-0', 'СЦ-')}</div>`,
      className: 'custom-pin-container',
      iconSize: [26, 26]
    });

    const m = L.marker(coords, { icon: scIcon }).addTo(map);
    m.bindPopup(`
      <b>${scName}</b><br>
      Инженеров в СЦ: <b>${crewsInSc}</b><br>
      <i>Кликните для фильтрации района</i>
    `);
    m.on('click', () => {
      document.getElementById('filter-district').value = scId;
      updateCrewFilterByDistrict();
      applyFilters();
    });
    scMarkers.push(m);
  });

  if (selectedDistrict === 'all' && selectedStaffId === 'all') {
    plan.routes.forEach(route => {
      const center = DISTRICT_COORDS[route.service_center_id] || [56.8335, 60.6010];
      route.stops.forEach(s => {
        if (s.type !== 'VISIT') return;

        const coords = getOrderCoordinates(s.order_id, center);
        const lat = coords[0];
        const lon = coords[1];
        const endMin = parseTimeToMin(s.end || s.start);

        const dotIcon = L.divIcon({
          html: `<div class="map-pin pin-dot" data-visit-minute="${endMin}"></div>`,
          className: 'custom-pin-container',
          iconSize: [10, 10]
        });

        const marker = L.marker([lat, lon], { icon: dotIcon }).addTo(map);
        marker.on('click', () => openInspector(s.order_id));
        markerLayers.push(marker);
      });
    });

    map.setView([56.8450, 60.6050], 12);
    applyMapTimeDimming();
    return;
  }

  if (selectedDistrict !== 'all' && selectedStaffId === 'all') {
    const center = DISTRICT_COORDS[selectedDistrict] || [56.8450, 60.6050];
    const districtRoutes = plan.routes.filter(r => r.service_center_id === selectedDistrict);
    const bounds = L.latLngBounds([center]);

    districtRoutes.forEach(route => {
      route.stops.forEach(s => {
        if (s.type !== 'VISIT') return;

        const coords = getOrderCoordinates(s.order_id, center);
        const lat = coords[0];
        const lon = coords[1];
        const endMin = parseTimeToMin(s.end || s.start);
        bounds.extend([lat, lon]);

        const dotIcon = L.divIcon({
          html: `<div class="map-pin pin-dot" data-visit-minute="${endMin}" title="${s.order_id}"></div>`,
          className: 'custom-pin-container',
          iconSize: [12, 12]
        });

        const marker = L.marker([lat, lon], { icon: dotIcon }).addTo(map);
        marker.bindPopup(`
          <b>Наряд:</b> ${s.order_id}<br>
          <b>Инженер:</b> ${route.staff_id}<br>
          <b>Время:</b> ${s.start.split('T')[1].substring(0, 5)} - ${s.end.split('T')[1].substring(0, 5)}<br>
          <a href="javascript:void(0)" onclick="openInspector('${s.order_id}')">Открыть карточку наряда</a>
        `);
        marker.on('click', () => openInspector(s.order_id));
        markerLayers.push(marker);
      });
    });

    map.fitBounds(bounds, { padding: [30, 30] });
    applyMapTimeDimming();
    return;
  }

  const targetRoute = plan.routes.find(r => r.staff_id === selectedStaffId);
  if (!targetRoute) return;

  const center = DISTRICT_COORDS[targetRoute.service_center_id] || [56.8450, 60.6050];
  const routePoints = [];
  const bounds = L.latLngBounds([]);
  let visitNum = 0;

  targetRoute.stops.forEach((s) => {
    let lat, lon;
    let pinClass = 'pin-num';
    let pinLabel = '';

    if (s.type === 'DEPOT_START' || s.type === 'DEPOT_END') {
      lat = center[0];
      lon = center[1];
      pinClass = 'pin-sc';
      pinLabel = s.type === 'DEPOT_START' ? RU_TEXT.T_0079 : RU_TEXT.T_0080;
    } else if (s.type === 'BREAK') {
      const lastPt = routePoints.length ? routePoints[routePoints.length - 1] : center;
      lat = lastPt[0] + 0.0005;
      lon = lastPt[1] + 0.0005;
      pinClass = 'pin-lunch';
      pinLabel = RU_TEXT.T_0081;
    } else if (s.type === 'RESTOCK') {
      lat = center[0];
      lon = center[1];
      pinClass = 'pin-sc';
      pinLabel = RU_TEXT.T_0079;
    } else {
      visitNum++;
      pinLabel = String(visitNum);
      const coords = getOrderCoordinates(s.order_id, center);
      lat = coords[0];
      lon = coords[1];
    }

    routePoints.push([lat, lon]);
    bounds.extend([lat, lon]);

    const visitMinAttr = s.type === 'VISIT' ? ` data-visit-minute="${parseTimeToMin(s.end || s.start)}"` : '';
    const icon = L.divIcon({
      html: `<div class="map-pin ${pinClass}"${visitMinAttr}>${pinLabel}</div>`,
      className: 'custom-pin-container',
      iconSize: pinClass === 'pin-lunch' ? [34, 18] : [22, 22]
    });

    const marker = L.marker([lat, lon], { icon }).addTo(map);

    const timeStart = s.start ? s.start.split('T')[1].substring(0, 5) : '';
    const timeEnd = s.end ? s.end.split('T')[1].substring(0, 5) : '';

    marker.bindPopup(`
      <b>${pinLabel.startsWith('Обед') ? 'Обеденный перерыв' : (s.type.startsWith('DEPOT') ? 'Сервисный центр' : 'Остановка #' + pinLabel)}</b><br>
      <b>Время:</b> ${timeStart} - ${timeEnd}<br>
      ${s.order_id ? `<b>Наряд:</b> ${s.order_id}<br><a href="javascript:void(0)" onclick="openInspector('${s.order_id}')">Открыть карточку наряда</a>` : ''}
    `);

    if (s.order_id) {
      marker.on('click', () => openInspector(s.order_id));
    }
    markerLayers.push(marker);
  });

  if (routePoints.length > 1) {
    let polyline = L.polyline(routePoints, {
      color: '#7700ff',
      weight: 4,
      opacity: 0.8,
      lineCap: 'round',
      lineJoin: 'round'
    }).addTo(map);
    routeLayers.push(polyline);

    fetch(`/api/v1/routing/track/${selectedStaffId}`)
      .then(r => r.json())
      .then(track => {
        if (track && track.coordinates && track.coordinates.length > 1) {
          map.removeLayer(polyline);
          const osrmPolyline = L.polyline(track.coordinates, {
            color: '#7700ff',
            weight: 5,
            opacity: 0.95,
            lineCap: 'round',
            lineJoin: 'round'
          }).addTo(map);
          routeLayers.push(osrmPolyline);

          const distKm = (track.distance_m / 1000).toFixed(1);
          const durMin = Math.round(track.duration_sec / 60);
          osrmPolyline.bindPopup(`
            <b>Дорожный граф: ${track.provider}</b><br>
            Пробег по улицам: <b>${distKm} км</b><br>
            Время в движении: <b>${durMin} мин</b><br>
            <i>Учтены скоростные лимиты и дорожные развязки</i>
          `);

          const statEl = document.getElementById('sb-eng-osrm-stat');
          if (statEl) {
            statEl.innerHTML = `<span class="badge" style="background:#f3e8ff; color:#7e22ce; font-weight:600;">Граф OSRM: ${distKm} км (${durMin} мин)</span>`;
          }
        }
      })
      .catch(err => console.warn('OSRM track error:', err));
  }

  map.fitBounds(bounds, { padding: [40, 40] });
  applyMapTimeDimming();
}

function hashString(str) {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    hash = (hash << 5) - hash + str.charCodeAt(i);
    hash |= 0;
  }
  return hash;
}

function getOrderShortCode(orderId) {
  if (!orderId) return '';
  const match = orderId.match(/\d+$/);
  return match ? match[0].padStart(4, '0').slice(-4) : orderId.slice(-4);
}

function syncGanttShiftTimeControls() {
  const slider = document.getElementById('gantt-shift-time');
  const valEl = document.getElementById('gantt-shift-time-value');
  const clamped = Math.max(540, Math.min(1260, Number(currentShiftMinutes) || 630));
  if (slider && Number(slider.value) !== clamped) slider.value = String(clamped);
  if (valEl) valEl.innerText = formatMinutesToHHMM(clamped);
  const pct = ((clamped - 540) / (1260 - 540)) * 100;
  document.querySelectorAll('.gantt-time-cursor-line').forEach(el => {
    el.style.left = `${pct.toFixed(2)}%`;
  });
}

function applyMapTimeDimming() {
  document.querySelectorAll('[data-visit-minute]').forEach(el => {
    const visitMin = Number(el.dataset.visitMinute || 0);
    el.style.opacity = visitMin && visitMin < currentShiftMinutes ? '0.45' : '1';
  });
}

function attachFmHoverTooltip(el, text) {
  let tip = document.getElementById('gantt-fm-tooltip');
  if (!tip) {
    tip = document.createElement('div');
    tip.id = 'gantt-fm-tooltip';
    tip.className = 'fm-hover-tooltip';
    document.body.appendChild(tip);
  }
  const moveTip = (e) => {
    const x = Math.min(window.innerWidth - 350, e.clientX + 12);
    const y = Math.min(window.innerHeight - 110, e.clientY + 12);
    tip.style.left = `${Math.max(8, x)}px`;
    tip.style.top = `${Math.max(8, y)}px`;
  };
  el.addEventListener('mouseenter', (e) => {
    tip.textContent = text;
    tip.style.display = 'block';
    moveTip(e);
  });
  el.addEventListener('mousemove', moveTip);
  el.addEventListener('mouseleave', () => {
    tip.style.display = 'none';
  });
}

function renderGantt(routes) {
  const container = document.getElementById('gantt-chart');
  if (!container) return;
  container.innerHTML = '';

  const dayStart = 9 * 60;
  const dayEnd = 21 * 60;
  const dayTotal = dayEnd - dayStart;

  routes
    .filter(r => r.stops.length > 0 || r.status === 'UNAVAILABLE')
    .slice()
    .sort((a, b) => (a.status === 'UNAVAILABLE' ? 1 : 0) - (b.status === 'UNAVAILABLE' ? 1 : 0))
    .forEach(r => {
      const row = document.createElement('div');
      row.className = 'gantt-row';
      row.dataset.staffId = r.staff_id;
      row.dataset.district = r.service_center_id;

      const staffMeta = (contextData && contextData.staff) ? contextData.staff[r.staff_id] : null;
      const staffName = staffMeta ? staffMeta.display_name.split(' (')[0] : r.staff_id;
      const profile = (staffMeta && staffMeta.profiles && staffMeta.profiles.includes('INSTALL')) ? RU_TEXT.T_0082 : RU_TEXT.T_0083;
      const scShort = r.service_center_id.replace('SC-0', RU_TEXT.T_0084);
      const scName = DISTRICT_NAMES[r.service_center_id] || scShort;
      const visits = r.stops.filter(s => s.type === 'VISIT');
      const visitCount = visits.length;

      const mobHeader = document.createElement('div');
      mobHeader.className = 'gantt-mob-header';
      const mobAbsentHtml = r.status === 'UNAVAILABLE'
        ? `<span class="gantt-absent-pill" aria-label="${RU_TEXT.T_0228}">${RU_TEXT.T_0228}</span>`
        : '';
      mobHeader.innerHTML = `
      <div class="gantt-mob-row-top">
        <div class="gantt-mob-title" title="Инженер: ${staffName} (${r.staff_id}). Нажмите, чтобы снять со смены (Форс-мажор)">
          <span class="gantt-mob-id">${r.staff_id}</span>
          <span class="gantt-mob-name">${staffName}</span>
          <span class="gantt-mob-spec">${profile}</span>
          ${mobAbsentHtml}
        </div>
        <button type="button" class="btn-sheet-link" title="Открыть маршрутный лист">Маршрут</button>
      </div>
      <div class="gantt-mob-row-sub">
        <span class="gantt-mob-district">${scName}</span>
        <span class="gantt-mob-stats"><b>${visitCount}</b> наряда &bull; 09:00 - 21:00</span>
      </div>
    `;

      const mobTitleEl = mobHeader.querySelector('.gantt-mob-title');
      if (mobTitleEl) {
        mobTitleEl.addEventListener('click', (e) => {
          e.stopPropagation();
          if (r.status === 'UNAVAILABLE') {
            showToast('Сотрудник уже снят', `${staffName} (${r.staff_id}) уже переведён в статус «Недоступен».`, 'info', 3500);
            return;
          }
          openForceMajeureModal(r.staff_id);
        });
      }

      mobHeader.querySelector('.btn-sheet-link').addEventListener('click', (e) => {
        e.stopPropagation();
        openRouteSheet(r.staff_id);
      });

      row.appendChild(mobHeader);

      const label = document.createElement('div');
      label.className = 'gantt-label';
      if (r.status === 'UNAVAILABLE') {
        row.classList.add('gantt-row-unavailable');
        const fmTooltip = getUnavailableTooltipText(r);
        label.innerHTML = `<span class="gantt-label-name">${staffName}</span><span class="gantt-absent-pill" aria-label="${RU_TEXT.T_0228}">${RU_TEXT.T_0228}</span>`;
        const absentPill = label.querySelector('.gantt-absent-pill');
        attachFmHoverTooltip(row, fmTooltip);
        attachFmHoverTooltip(label, fmTooltip);
        if (absentPill) attachFmHoverTooltip(absentPill, fmTooltip);
      } else {
        label.innerHTML = `<span class="gantt-label-name">${staffName}</span>`;
        label.title = `Инженер: ${staffName} (${r.staff_id}). Нажмите, чтобы снять со смены (Форс-мажор)`;
      }

      label.addEventListener('click', (e) => {
        e.stopPropagation();
        if (r.status === 'UNAVAILABLE') {
          showToast('Сотрудник уже снят', `${staffName} (${r.staff_id}) уже переведён в статус «Недоступен».`, 'info', 3500);
          return;
        }
        openForceMajeureModal(r.staff_id);
      });

      row.appendChild(label);

      const timeline = document.createElement('div');
      timeline.className = 'gantt-timeline-wrap';
      const cursorPct = (((Math.max(540, Math.min(1260, Number(currentShiftMinutes) || 630)) - 540) / (1260 - 540)) * 100).toFixed(2);
      const cursorEl = document.createElement('div');
      cursorEl.className = 'gantt-time-cursor-line';
      cursorEl.style.left = `${cursorPct}%`;
      timeline.appendChild(cursorEl);

      let visCounter = 0;

      r.stops.forEach(s => {
        const startMin = parseTimeToMin(s.start || s.arrival);
        const endMin = parseTimeToMin(s.end);
        if (endMin <= startMin) return;

        const leftPercent = Math.max(0, ((startMin - dayStart) / dayTotal) * 100);
        const widthPercent = Math.max(1.5, ((endMin - startMin) / dayTotal) * 100);

        const block = document.createElement('div');
        block.className = 'gantt-block';
        block.style.left = `${leftPercent}%`;
        block.style.width = `${widthPercent}%`;

        const tStartStr = s.start.split('T')[1].substring(0, 5);
        const tEndStr = s.end.split('T')[1].substring(0, 5);
        const durationMin = endMin - startMin;

        if (s.type === 'BREAK') {
          block.classList.add('badge-lunch');
          block.innerHTML = RU_TEXT.T_0085;
          block.title = `Обеденный перерыв (1 час): ${tStartStr} - ${tEndStr}`;
          block.addEventListener('click', (e) => {
            e.stopPropagation();
            showToast(RU_TEXT.T_0081, `Обеденный перерыв (${tStartStr} - ${tEndStr}) инженера ${staffName}`, 'info');
          });
        } else if (s.type === 'RESTOCK') {
          block.classList.add('badge-reload');
          block.innerHTML = RU_TEXT.T_0086;
          block.title = `Пополнение оборудования в СЦ: ${tStartStr} - ${tEndStr}`;
          block.addEventListener('click', (e) => {
            e.stopPropagation();
            showToast(RU_TEXT.T_0087, `Загрузка оборудования со склада СЦ: ${tStartStr} - ${tEndStr}`, 'info');
          });
        } else if (s.type === 'VISIT') {
          visCounter++;

          const orderMeta = (contextData && contextData.orders) ? contextData.orders[s.order_id] : null;
          const wtCode = orderMeta ? orderMeta.work_type_code : '';
          const wtMeta = (wtCode && contextData && contextData.work_types) ? contextData.work_types[wtCode] : null;
          const wtName = wtMeta ? wtMeta.name : (orderMeta ? orderMeta.work_type_code : RU_TEXT.T_0088);

          const isInstall = wtCode.startsWith('INST') || wtCode === 'ADD_SERVICE' || (wtMeta && wtMeta.name && wtMeta.name.toLowerCase().includes(RU_TEXT.T_0089));
          let blockClass = isInstall ? 'badge-order-install' : 'badge-order-std';
          if (s.risk || s.is_late) {
            blockClass = 'badge-risk';
          }

          block.classList.add(blockClass);

          const isDone = isStopCompleted(s);
          const isCancelled = s.status === 'CANCELLED';
          if (isDone) {
            block.classList.add('block-completed');
          }

          const checkIcon = isDone ? getCheckIconSvg(10, 'gantt-check-svg') + ' ' : '';
          const shortNum = getOrderShortCode(s.order_id);

          if (isCancelled) {
            block.style.opacity = '0.45';
            block.style.background = '#f1f5f9';
            block.style.borderColor = '#ef4444';
            block.style.borderStyle = 'dashed';
            block.innerHTML = `<span class="block-txt" style="text-decoration:line-through; color:#dc2626;">${shortNum}</span>`;
            if (r.status === 'UNAVAILABLE') {
              attachFmHoverTooltip(block, getUnavailableTooltipText(r));
            } else {
              block.title = `[ОТМЕНЕН/ПЕРЕНЕСЕН]: Наряд ${s.order_id}\nМастер ${staffName} снят со смены (${s.explanation || 'Форс-мажор'})`;
            }
          } else {
            block.innerHTML = `<span class="block-txt">${checkIcon}${shortNum}</span>`;
            block.title = `Наряд ${s.order_id} (Визит ${visCounter})${isDone ? ' [Выполнен]' : ''}\nВремя: ${tStartStr} - ${tEndStr} (${durationMin} мин)\nВид работ: ${wtName}\nИнженер: ${staffName}\n${s.explanation || ''}\nКликните для просмотра полной карточки наряда`;
          }

          block.dataset.orderId = s.order_id;
          block.addEventListener('click', (e) => {
            e.stopPropagation();
            document.querySelectorAll('.gantt-block.selected-block').forEach(b => b.classList.remove('selected-block'));
            block.classList.add('selected-block');

            openInspector(s.order_id);
          });
        } else {
          return;
        }

        timeline.appendChild(block);
      });

      row.appendChild(timeline);

      const mobTicks = document.createElement('div');
      mobTicks.className = 'gantt-mob-ticks';
      mobTicks.innerHTML = `
      <span>09:00</span>
      <span>13:00</span>
      <span>17:00</span>
      <span>21:00</span>
    `;
      row.appendChild(mobTicks);

      if (visitCount > 0) {
        const toggleBtn = document.createElement('button');
        toggleBtn.type = 'button';
        toggleBtn.className = 'gantt-mob-expand-btn';
        toggleBtn.innerHTML = `<span>График нарядов (${visitCount})</span> <span class="arr-icon">&#9662;</span>`;

        const stopsList = document.createElement('div');
        stopsList.className = 'gantt-mob-stops-list';
        stopsList.style.display = 'none';

        let listHtml = '';
        r.stops.forEach(s => {
          if (s.type === 'DEPOT_START' || s.type === 'DEPOT_END') return;
          const tStart = s.start.split('T')[1].substring(0, 5);
          const tEnd = s.end.split('T')[1].substring(0, 5);

          if (s.type === 'BREAK') {
            listHtml += `
            <div class="mob-stop-item break-item">
              <span class="mob-stop-time">${tStart} - ${tEnd}</span>
              <span class="mob-stop-badge badge-lunch">Обед</span>
              <span class="mob-stop-desc">Обеденный перерыв (1 час)</span>
            </div>
          `;
          } else if (s.type === 'RESTOCK') {
            listHtml += `
            <div class="mob-stop-item reload-item">
              <span class="mob-stop-time">${tStart} - ${tEnd}</span>
              <span class="mob-stop-badge badge-reload">СЦ</span>
              <span class="mob-stop-desc">Пополнение оборудования на складе</span>
            </div>
          `;
          } else if (s.type === 'VISIT') {
            const oMeta = (contextData && contextData.orders) ? contextData.orders[s.order_id] : null;
            const wtCode = oMeta ? oMeta.work_type_code : '';
            const addr = oMeta ? oMeta.address : RU_TEXT.T_0045;
            let bClass = 'badge-install';
            let bName = RU_TEXT.T_0090;
            if (wtCode.startsWith('INST') || wtCode === 'ADD_SERVICE') {
              bClass = 'badge-install';
              bName = wtCode.includes('PON') ? 'PON' : RU_TEXT.T_0090;
            } else if (wtCode.startsWith('REPAIR') || wtCode === 'DEGRADATION') {
              bClass = 'badge-repair';
              bName = RU_TEXT.T_0083;
            } else if (wtCode.startsWith('SURVEY') || wtCode === 'DISMANTLE') {
              bClass = 'badge-survey';
              bName = RU_TEXT.T_0091;
            }

            listHtml += `
            <div class="mob-stop-item" onclick="openInspector('${s.order_id}')" title="Открыть карточку наряда">
              <span class="mob-stop-time">${tStart} - ${tEnd}</span>
              <span class="mob-stop-badge ${bClass}">${bName}</span>
              <span class="mob-stop-ord">${s.order_id}</span>
              <span class="mob-stop-addr">${addr}</span>
            </div>
          `;
          }
        });

        stopsList.innerHTML = listHtml;

        toggleBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          const isOpen = stopsList.style.display !== 'none';
          stopsList.style.display = isOpen ? 'none' : 'flex';
          toggleBtn.classList.toggle('active', !isOpen);
          toggleBtn.querySelector('.arr-icon').innerHTML = isOpen ? '&#9662;' : '&#9652;';
          toggleBtn.querySelector('span:first-child').innerText = isOpen ? `График нарядов (${visitCount})` : RU_TEXT.T_0092;
        });

        row.appendChild(toggleBtn);
        row.appendChild(stopsList);
      }

      container.appendChild(row);
    });

  filterGanttRows();
}

function openInspector(orderId) {
  if (!orderId) return;
  activeInspectorOrderId = orderId;

  const panel = document.getElementById('order-inspector');
  panel.classList.add('open');

  let assignedStaff = null;
  let assignedStop = null;
  let assignedRoute = null;

  if (currentPlanData && currentPlanData.routes) {
    for (const r of currentPlanData.routes) {
      for (const s of r.stops) {
        if (s.order_id === orderId) {
          assignedStaff = r.staff_id;
          assignedStop = s;
          assignedRoute = r;
          break;
        }
      }
      if (assignedStaff) break;
    }
  }

  const orderMeta = (contextData && contextData.orders) ? contextData.orders[orderId] : null;
  const staffMeta = (contextData && contextData.staff && assignedStaff) ? contextData.staff[assignedStaff] : null;
  const wtMeta = (orderMeta && contextData && contextData.work_types) ? contextData.work_types[orderMeta.work_type_code] : null;

  document.getElementById('insp-order-id').innerText = orderId;
  const badge = document.getElementById('insp-status-badge');
  const reassignLabel = document.getElementById('reassign-label');
  const btnReassign = document.getElementById('btn-do-reassign');
  const btnDelay = document.getElementById('btn-add-delay');
  const btnSheet = document.getElementById('btn-insp-route-sheet');

  const btnToggleComplete = document.getElementById('btn-insp-toggle-complete');

  const reassignSelectEl = document.getElementById('reassign-select');

  if (assignedStop) {
    if (btnReassign) {
      btnReassign.disabled = false;
      btnReassign.classList.remove('btn-disabled');
      btnReassign.style.opacity = '1';
    }
    if (btnDelay) {
      btnDelay.disabled = false;
      btnDelay.classList.remove('btn-disabled');
    }
    if (reassignSelectEl) reassignSelectEl.disabled = false;
    const isDone = isStopCompleted(assignedStop);
    badge.className = isDone ? 'status-badge completed' : 'status-badge';
    badge.innerText = isDone ? RU_TEXT.T_0093 : RU_TEXT.T_0094;
    if (reassignLabel) reassignLabel.innerText = RU_TEXT.T_0095;
    if (btnReassign) btnReassign.innerText = RU_TEXT.T_0096;
    if (btnDelay) {
      btnDelay.innerText = RU_TEXT.T_0097;
      btnDelay.title = RU_TEXT.T_0098;
    }
    if (btnSheet) {
      btnSheet.innerText = `Маршрутный лист инженера (${assignedStaff})`;
      btnSheet.title = RU_TEXT.T_0099;
    }
    if (btnToggleComplete) {
      btnToggleComplete.style.display = 'block';
      if (isDone) {
        btnToggleComplete.className = 'btn btn-outline btn-sm';
        btnToggleComplete.innerHTML = `${getCheckCircleIconSvg(14)} Выполнен (кликните для отмены)`;
        btnToggleComplete.title = RU_TEXT.T_0100;
      } else {
        btnToggleComplete.className = 'btn btn-success btn-sm';
        btnToggleComplete.innerHTML = RU_TEXT.T_0101;
        btnToggleComplete.title = RU_TEXT.T_0102;
      }
    }

    const btnRemoveStaff = document.getElementById('btn-insp-remove-staff');
    if (btnRemoveStaff) {
      if (assignedStaff) {
        btnRemoveStaff.style.display = 'block';
        btnRemoveStaff.innerText = `Снять мастера ${assignedStaff} со смены (Форс-мажор)`;
        btnRemoveStaff.onclick = () => openForceMajeureModal(assignedStaff);
      } else {
        btnRemoveStaff.style.display = 'none';
      }
    }
  } else {
    let isConfirmed = false;
    if (currentPlanData && currentPlanData.unassigned) {
      const uMatch = currentPlanData.unassigned.find(u => u.order_id === orderId);
      if (uMatch && (uMatch.suggested_action === 'CONFIRMED_FOR_TOMORROW' || (uMatch.reason_text && uMatch.reason_text.includes(RU_TEXT.T_0103)))) {
        isConfirmed = true;
      }
    }

    const nextShort = getNextWorkdayShortLabel();
    const nextFull = getNextWorkdayLabel();
    badge.className = 'status-badge unassigned';
    badge.innerText = isConfirmed ? `Согласовано на ${nextShort}` : RU_TEXT.T_0104;
    if (reassignLabel) reassignLabel.innerText = RU_TEXT.T_0105;
    if (btnReassign) {
      btnReassign.innerText = isConfirmed ? RU_TEXT.T_0247 : RU_TEXT.T_0106;
      btnReassign.disabled = isConfirmed;
      btnReassign.classList.toggle('btn-disabled', isConfirmed);
      btnReassign.style.opacity = isConfirmed ? '0.5' : '1';
      btnReassign.title = isConfirmed ? RU_TEXT.T_0243 : RU_TEXT.T_0144;
    }
    if (reassignSelectEl) {
      reassignSelectEl.disabled = isConfirmed;
    }
    if (btnDelay) {
      btnDelay.innerText = `Согласовать перенос на завтра (${nextShort})`;
      btnDelay.title = isConfirmed ? RU_TEXT.T_0243 : `Зафиксировать согласие клиента на перенос заявки на ${nextFull}`;
      btnDelay.disabled = isConfirmed;
      btnDelay.classList.toggle('btn-disabled', isConfirmed);
    }
    if (btnSheet) {
      btnSheet.innerText = RU_TEXT.T_0107;
      btnSheet.title = RU_TEXT.T_0108;
    }
    if (btnToggleComplete) {
      btnToggleComplete.style.display = 'none';
    }
    const btnRemoveStaff = document.getElementById('btn-insp-remove-staff');
    if (btnRemoveStaff) {
      btnRemoveStaff.style.display = 'none';
    }
  }

  const clientIdx = Math.abs(hashString(orderId)) % CLIENT_NAMES.length;
  const clientName = (orderMeta && orderMeta.client_name) || CLIENT_NAMES[clientIdx];
  const clientPhone = (orderMeta && orderMeta.client_phone) || `+7 (922) ${100 + (clientIdx * 37) % 900}-${10 + (clientIdx * 7) % 90}-${10 + (clientIdx * 11) % 90}`;

  const clientAddress = orderMeta ? orderMeta.address : RU_TEXT.T_0109;
  const addrEl = document.getElementById('insp-address');
  if (addrEl) {
    addrEl.innerHTML = `
      <div style="display:flex; align-items:center; justify-content:space-between; gap:6px;">
        <span>${clientAddress}</span>
        <button type="button" class="btn-map-loc" onclick="showOnAppMap('${orderId}', '${assignedStaff || ''}')" title="Показать объект на интерактивной карте">На карте</button>
      </div>
    `;
  }
  const scId = assignedRoute ? assignedRoute.service_center_id : getOrderDistrict(orderMeta);
  document.getElementById('insp-district').innerText = DISTRICT_NAMES[scId] || scId;
  document.getElementById('insp-client').innerText = clientName;
  const cleanPhone = clientPhone.replace(/[^0-9+]/g, '');
  document.getElementById('insp-phone').innerHTML = `<a href="tel:${cleanPhone}" style="color:#7700ff; text-decoration:underline; font-weight:600;" title="Позвонить абоненту">${clientPhone}</a>`;
  document.getElementById('insp-building-type').innerText = (orderMeta && orderMeta.building_type === 'PRIVATE') ? RU_TEXT.T_0110 : RU_TEXT.T_0111;
  document.getElementById('insp-access-node').innerText = orderMeta ? (orderMeta.access_node_id || 'AN-0103') : 'AN-0103';

  const wtTitle = wtMeta ? wtMeta.name : (orderMeta ? orderMeta.work_type_code : RU_TEXT.T_0112);
  const wtDuration = wtMeta ? `${wtMeta.duration_min} мин` : RU_TEXT.T_0113;
  document.getElementById('insp-work-type').innerText = `${wtTitle} (${wtDuration})`;
  document.getElementById('insp-tech').innerText = orderMeta ? orderMeta.technology : 'PON (GPON)';

  const slotStr = orderMeta ? `${orderMeta.slot.start.split('T')[1].substring(0, 5)} - ${orderMeta.slot.end.split('T')[1].substring(0, 5)}` : '11:00 - 13:00';
  document.getElementById('insp-slot').innerText = slotStr;

  if (assignedStop && orderMeta && orderMeta.slot) {
    const arrMin = parseTimeToMin(assignedStop.start || assignedStop.arrival);
    const slotStartMin = parseTimeToMin(orderMeta.slot.start);
    const slotEndMin = parseTimeToMin(orderMeta.slot.end);
    const arrTime = assignedStop.start.split('T')[1].substring(0, 5);
    const arrEl = document.getElementById('insp-arrival');

    if (arrMin >= slotStartMin && arrMin <= slotEndMin) {
      arrEl.className = 'insp-value bold text-success';
      arrEl.innerText = `${arrTime} (в согласованном слоте)`;
    } else if (arrMin < slotStartMin) {
      arrEl.className = 'insp-value bold text-primary';
      arrEl.innerText = `${arrTime} (раньше слота)`;
    } else {
      const lateness = arrMin - slotEndMin;
      arrEl.className = 'insp-value bold text-danger';
      arrEl.innerText = `${arrTime} (опоздание на ${lateness} мин)`;
    }
  } else if (assignedStop) {
    const arrTime = assignedStop.start.split('T')[1].substring(0, 5);
    document.getElementById('insp-arrival').innerText = `${arrTime}`;
  } else {
    document.getElementById('insp-arrival').className = 'insp-value';
    document.getElementById('insp-arrival').innerText = RU_TEXT.T_0114;
  }

  document.getElementById('insp-height').innerText = (orderMeta && orderMeta.requires_height) ? RU_TEXT.T_0115 : RU_TEXT.T_0116;
  document.getElementById('insp-repeat').innerText = (orderMeta && orderMeta.is_repeat) ? RU_TEXT.T_0117 : RU_TEXT.T_0118;

  const elPred = document.getElementById('insp-duration-pred');
  if (elPred) {
    let baseDur = wtMeta ? wtMeta.duration_min : (orderMeta && orderMeta.duration_min ? orderMeta.duration_min : 60);
    let predDur = baseDur;
    let factorItems = [];
    const expYears = (staffMeta && staffMeta.experience_years !== undefined && staffMeta.experience_years !== null) ? staffMeta.experience_years : 4.5;
    if (expYears >= 5.0) {
      predDur = Math.round(predDur * 0.88);
      factorItems.push(`стаж ${expYears}г: -12%`);
    } else if (expYears < 1.5) {
      predDur = Math.round(predDur * 1.15);
      factorItems.push(`стаж ${expYears}г: +15%`);
    }
    if (orderMeta && orderMeta.building_type === 'PRIVATE') {
      predDur = Math.round(predDur * 1.12);
      factorItems.push(RU_TEXT.T_0119);
    }
    if (orderMeta && orderMeta.is_repeat) {
      predDur = Math.round(predDur * 1.15);
      factorItems.push(RU_TEXT.T_0120);
    }
    if (orderMeta && orderMeta.requires_height) {
      factorItems.push(RU_TEXT.T_0121);
    }
    const factorsDesc = factorItems.length > 0 ? ` (${factorItems.join(', ')})` : RU_TEXT.T_0122;
    elPred.innerText = `${predDur} мин${factorsDesc}`;
  }

  const elHistForecast = document.getElementById('insp-duration-forecast');
  if (elHistForecast) {
    elHistForecast.innerText = RU_TEXT.T_0123;
    fetch(`/api/v1/ui/orders/${encodeURIComponent(orderId)}/duration-forecast`)
      .then(r => (r.ok ? r.json() : null))
      .then(fc => {
        if (!fc || activeInspectorOrderId !== orderId) return;
        if (fc.source === 'history') {
          elHistForecast.innerText = `По истории: ${fc.minutes} мин (n=${fc.sample_count})`;
        } else {
          elHistForecast.innerText = `По нормативу: ${fc.minutes} мин (история не накоплена)`;
        }
      })
      .catch(() => {
        if (elHistForecast && activeInspectorOrderId === orderId) {
          elHistForecast.innerText = RU_TEXT.T_0124;
        }
      });
  }

  const smsWrap = document.getElementById('insp-sms-draft-wrap');
  const smsBtn = document.getElementById('btn-insp-sms-draft');
  const smsBox = document.getElementById('insp-sms-draft-text');
  const copySmsBtn = document.getElementById('btn-copy-sms-draft');
  const callActionsRow = document.getElementById('insp-call-actions');
  const btnInspCall = document.getElementById('btn-insp-call');
  const btnInspDemoCall = document.getElementById('btn-insp-demo-call');
  const btnInspNoAnswer = document.getElementById('btn-insp-no-answer');
  const btnInspCompleteRestock = document.getElementById('btn-insp-complete-restock');

  if (callActionsRow) {
    const showCalls = Boolean(assignedStop && assignedStop.status !== 'DONE' && assignedStop.status !== 'CANCELLED');
    callActionsRow.style.display = showCalls ? 'flex' : 'none';
    const runInspectorVisitAction = (path, title, okMsg, toastType = 'success') => {
      fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) })
        .then(async r => {
          const data = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
          return data;
        })
        .then(() => {
          showToast(title, okMsg, toastType, 5000);
          loadAllData().then(() => {
            if (activeInspectorOrderId === orderId) openInspector(orderId);
          });
        })
        .catch(err => showToast('Действие недоступно', String(err.message || err), 'error'));
    };
    if (btnInspCall) {
      btnInspCall.onclick = () => runInspectorVisitAction(
        `/api/v1/ui/visits/${encodeURIComponent(orderId)}/call`,
        'Контрольный звонок',
        `Контрольный звонок по наряду ${orderId} подтверждён`
      );
    }
    if (btnInspDemoCall) {
      btnInspDemoCall.onclick = () => runInspectorVisitAction(
        `/api/v1/ui/visits/${encodeURIComponent(orderId)}/call?demo=true`,
        'Демозвонок зафиксирован',
        `Демозвонок по наряду ${orderId} отмечен`
      );
    }
    if (btnInspNoAnswer) {
      btnInspNoAnswer.onclick = () => runInspectorVisitAction(
        `/api/v1/ui/visits/${encodeURIComponent(orderId)}/no-answer?demo=true`,
        'Клиент не отвечает',
        `Визит ${orderId} отложен в очередь без простоя инженера`,
        'warning'
      );
    }
  }

  if (btnInspCompleteRestock) {
    const hasPendingRestock = Boolean(
      assignedRoute && (assignedRoute.stops || []).some(st => st.type === 'RESTOCK' && st.status !== 'DONE' && st.status !== 'CANCELLED')
    );
    btnInspCompleteRestock.style.display = hasPendingRestock ? 'block' : 'none';
    btnInspCompleteRestock.onclick = () => completeEngineerRestock(assignedStaff);
  }

  const isAtRiskOrUnassigned = !assignedStop || Boolean(assignedStop.risk || assignedStop.is_late);
  if (smsWrap) {
    smsWrap.style.display = isAtRiskOrUnassigned ? 'block' : 'none';
    if (smsBox) {
      smsBox.style.display = 'none';
      smsBox.value = '';
    }
    if (copySmsBtn) {
      copySmsBtn.style.display = 'none';
      copySmsBtn.onclick = async () => {
        if (!smsBox || !smsBox.value) return;
        try {
          if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(smsBox.value);
          } else {
            smsBox.select();
            document.execCommand('copy');
          }
          showToast('SMS скопировано', 'Черновик сообщения скопирован в буфер обмена', 'success', 3500);
        } catch (_err) {
          smsBox.select();
          document.execCommand('copy');
          showToast('SMS скопировано', 'Черновик сообщения выделен/скопирован', 'success', 3500);
        }
      };
    }
    if (smsBtn) {
      smsBtn.onclick = () => {
        smsBtn.disabled = true;
        fetch(`/api/v1/ui/orders/${encodeURIComponent(orderId)}/reschedule-draft`)
          .then(r => r.json())
          .then(d => {
            if (smsBox) {
              smsBox.value = `Причина: ${d.reason || 'Перенос'}\n${d.draft || ''}`;
              smsBox.style.display = 'block';
            }
            if (copySmsBtn && smsBox && smsBox.value.trim()) {
              copySmsBtn.style.display = 'block';
            }
          })
          .catch(err => showToast(RU_TEXT.T_0125, String(err), 'error'))
          .finally(() => { smsBtn.disabled = false; });
      };
    }
  }

  const jvBox = document.getElementById('insp-joint-visit-box');
  const jvPartner = document.getElementById('jv-partner-select');
  const jvStart = document.getElementById('jv-start-time');
  const jvAutoBtn = document.getElementById('btn-jv-auto');
  const jvPreviewBtn = document.getElementById('btn-jv-preview');
  const jvConfirmBtn = document.getElementById('btn-jv-confirm');
  const jvResultEl = document.getElementById('jv-preview-result');
  let suggestedLeadStaffId = null;
  if (jvBox) {
    if (orderMeta && orderMeta.requires_height) {
      jvBox.style.display = 'block';
      if (jvResultEl) jvResultEl.innerHTML = '';
      const unavailableIds = new Set(
        (currentPlanData && currentPlanData.routes ? currentPlanData.routes : [])
          .filter(r => r.status === 'UNAVAILABLE')
          .map(r => r.staff_id)
      );
      const partnerOrderId = `VLS-PARTNER-${orderId}`;
      let pairedStaffId = null;
      let pairedStop = null;
      if (currentPlanData && currentPlanData.routes) {
        for (const rt of currentPlanData.routes) {
          const hit = (rt.stops || []).find(st => st.order_id === partnerOrderId && st.status !== 'CANCELLED');
          if (hit) {
            pairedStaffId = rt.staff_id;
            pairedStop = hit;
            break;
          }
        }
      }
      const staffValues = (contextData && contextData.staff) ? Object.values(contextData.staff) : [];
      const reqProfiles = (wtMeta && Array.isArray(wtMeta.profiles)) ? wtMeta.profiles : [];
      const qualifiedHeightStaff = staffValues.filter(s =>
        s.height_permit
        && !unavailableIds.has(s.staff_id)
        && (!orderMeta.technology || !(s.technologies && s.technologies.length) || s.technologies.includes(orderMeta.technology))
        && (!reqProfiles.length || !(s.profiles && s.profiles.length) || s.profiles.some(p => reqProfiles.includes(p)))
      );
      const partners = qualifiedHeightStaff.filter(s => s.staff_id !== assignedStaff);
      if (jvPartner) {
        jvPartner.innerHTML = partners.length
          ? partners.map(p => `<option value="${p.staff_id}">${p.staff_id}: ${p.display_name.split(' (')[0]}</option>`).join('')
          : '<option value="">Нет доступных напарников с допуском ВЛС</option>';
        if (pairedStaffId && partners.some(p => p.staff_id === pairedStaffId)) {
          jvPartner.value = pairedStaffId;
        }
      }
      if (pairedStaffId && pairedStop && jvResultEl) {
        const sT = pairedStop.start ? pairedStop.start.split('T')[1].substring(0, 5) : '';
        const eT = pairedStop.end ? pairedStop.end.split('T')[1].substring(0, 5) : '';
        jvResultEl.innerHTML = `<span style="color:#16a34a; font-weight:600;">Парный экипаж ВЛС подтверждён: ${assignedStaff || '—'} + ${pairedStaffId} (${sT} – ${eT})</span>`;
      }
      if (jvAutoBtn) jvAutoBtn.disabled = Boolean(pairedStaffId);
      if (jvPreviewBtn) jvPreviewBtn.disabled = Boolean(pairedStaffId);
      if (jvConfirmBtn) jvConfirmBtn.disabled = Boolean(pairedStaffId);
      if (jvStart) {
        const initT = assignedStop && assignedStop.start
          ? assignedStop.start.split('T')[1].substring(0, 5)
          : (orderMeta.slot && orderMeta.slot.start ? orderMeta.slot.start.split('T')[1].substring(0, 5) : '11:00');
        jvStart.value = initT;
      }
      let lastJointMeta = null;
      const isTimeInOrderSlot = (hhmm) => {
        if (!orderMeta || !orderMeta.slot || !orderMeta.slot.start) return false;
        const ts = getCurrentEventTimestamp(hhmm);
        const s0 = orderMeta.slot.start;
        let maxIso = orderMeta.latest_start || orderMeta.slot.end || orderMeta.slot.start;
        if (String(orderMeta.work_type_code || '').startsWith('INST')) {
          const d0 = new Date(s0);
          const cap = new Date(d0.getTime() + 10 * 60000);
          const m0 = new Date(maxIso);
          maxIso = (cap < m0 ? cap : m0).toISOString();
        }
        return new Date(ts) >= new Date(s0) && new Date(ts) <= new Date(maxIso);
      };
      const getJointPayload = (forConfirm = false) => {
        const partnerId = jvPartner ? jvPartner.value : '';
        const chosenLead = document.getElementById('reassign-select')?.value;
        const chosenLeadValid = qualifiedHeightStaff.find(s => s.staff_id === chosenLead && s.staff_id !== partnerId)?.staff_id;
        const fallbackLead = qualifiedHeightStaff.find(s => s.staff_id !== partnerId)?.staff_id || '';
        const leadId = (assignedStaff && assignedStaff !== partnerId ? assignedStaff : null)
          || (suggestedLeadStaffId && suggestedLeadStaffId !== partnerId ? suggestedLeadStaffId : null)
          || chosenLeadValid
          || fallbackLead;
        const hhmm = (jvStart && jvStart.value) ? jvStart.value : '11:00';
        const startIso = getCurrentEventTimestamp(hhmm);
        if (lastJointMeta && lastJointMeta.partner_staff_id === partnerId && String(lastJointMeta.start).slice(11, 16) === hhmm) {
          return {
            lead_staff_id: lastJointMeta.lead_staff_id || leadId,
            partner_staff_id: partnerId,
            start: lastJointMeta.start,
            queue_order_ids: forConfirm ? (lastJointMeta.queue_order_ids || []) : [],
            new_slot: Boolean(lastJointMeta.new_slot)
          };
        }
        return {
          lead_staff_id: leadId,
          partner_staff_id: partnerId,
          start: startIso,
          queue_order_ids: [],
          new_slot: !isTimeInOrderSlot(hhmm)
        };
      };
      if (jvAutoBtn) {
        jvAutoBtn.onclick = () => {
          jvAutoBtn.disabled = true;
          const prevLabel = jvAutoBtn.innerText;
          jvAutoBtn.innerText = 'Поиск...';
          fetch(`/api/v1/ui/orders/${encodeURIComponent(orderId)}/joint-visit/auto`, { method: 'POST' })
            .then(async r => {
              const d = await r.json().catch(() => ({}));
              if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
              return d;
            })
            .then(d => {
              lastJointMeta = d;
              suggestedLeadStaffId = d.lead_staff_id || null;
              if (jvPartner && d.partner_staff_id) {
                if (!Array.from(jvPartner.options).some(opt => opt.value === d.partner_staff_id)) {
                  const opt = document.createElement('option');
                  opt.value = d.partner_staff_id;
                  opt.textContent = d.partner_staff_id;
                  jvPartner.appendChild(opt);
                }
                jvPartner.value = d.partner_staff_id;
              }
              if (jvStart && d.start) {
                jvStart.value = String(d.start).slice(11, 16);
              }
              const sT = d.start ? String(d.start).slice(11, 16) : '';
              const eT = d.end ? String(d.end).slice(11, 16) : '';
              const slotTag = d.new_slot ? ' (новый слот)' : '';
              const crewTxt = (d.crew || [])
                .map(c => `${c.name} (+${Math.round((c.travel_sec || 0) / 60)} мин)`)
                .join(' + ');
              const qCount = Array.isArray(d.queue_order_ids) ? d.queue_order_ids.length : 0;
              const qNote = qCount > 0 ? ` В очередь переноса: ${qCount}.` : '';
              if (jvResultEl) {
                jvResultEl.innerHTML = `<span style="color:#16a34a; font-weight:600;">Подобрано окно ВЛС: ${sT} – ${eT}${slotTag}</span> &bull; Экипаж: ${crewTxt}.${qNote} Нажмите «Подтвердить ВЛС».`;
              }
            })
            .catch(err => {
              if (jvResultEl) jvResultEl.innerHTML = `<span style="color:#dc2626;">${String(err.message || err)}</span>`;
            })
            .finally(() => {
              jvAutoBtn.disabled = false;
              jvAutoBtn.innerText = prevLabel;
            });
        };
      }
      if (jvPreviewBtn) {
        jvPreviewBtn.onclick = () => {
          const payload = getJointPayload(false);
          if (!payload.lead_staff_id || !payload.partner_staff_id) {
            showToast(RU_TEXT.T_0126, RU_TEXT.T_0127, 'warning');
            return;
          }
          jvPreviewBtn.disabled = true;
          fetch(`/api/v1/ui/orders/${encodeURIComponent(orderId)}/joint-visit`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
          })
            .then(async r => {
              const d = await r.json().catch(() => ({}));
              if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
              return d;
            })
            .then(d => {
              lastJointMeta = d;
              const sT = d.start ? d.start.split('T')[1].substring(0, 5) : '';
              const eT = d.end ? d.end.split('T')[1].substring(0, 5) : '';
              const slotTag = d.new_slot ? ' (новый слот)' : '';
              const crewTxt = (d.crew || [])
                .map(c => `${c.name} (+${Math.round((c.travel_sec || 0) / 60)} мин)`)
                .join(' + ');
              const qCount = Array.isArray(d.queue_order_ids) ? d.queue_order_ids.length : 0;
              const qNote = qCount > 0 ? ` &bull; В очередь переноса: ${qCount}` : '';
              if (jvResultEl) {
                jvResultEl.innerHTML = `<span style="color:#16a34a; font-weight:600;">Окно ВЛС: ${sT} – ${eT}${slotTag}</span> &bull; Экипаж: ${crewTxt}${qNote}`;
              }
            })
            .catch(err => {
              if (jvResultEl) jvResultEl.innerHTML = `<span style="color:#dc2626;">${String(err.message || err)}</span>`;
            })
            .finally(() => { jvPreviewBtn.disabled = false; });
        };
      }
      if (jvConfirmBtn) {
        jvConfirmBtn.onclick = async () => {
          let payload = getJointPayload(true);
          if (!payload.lead_staff_id || !payload.partner_staff_id) {
            showToast(RU_TEXT.T_0126, RU_TEXT.T_0127, 'warning');
            return;
          }
          jvConfirmBtn.disabled = true;
          try {
            if (!lastJointMeta || lastJointMeta.partner_staff_id !== payload.partner_staff_id || String(lastJointMeta.start).slice(11, 16) !== String(payload.start).slice(11, 16)) {
              const pr = await fetch(`/api/v1/ui/orders/${encodeURIComponent(orderId)}/joint-visit`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
              });
              const pd = await pr.json().catch(() => ({}));
              if (!pr.ok) throw new Error(pd.detail || `HTTP ${pr.status}`);
              lastJointMeta = pd;
              payload = {
                lead_staff_id: pd.lead_staff_id,
                partner_staff_id: pd.partner_staff_id,
                start: pd.start,
                queue_order_ids: pd.queue_order_ids || [],
                new_slot: Boolean(pd.new_slot)
              };
            }
            const r = await fetch(`/api/v1/ui/orders/${encodeURIComponent(orderId)}/joint-visit/confirm`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(payload)
            });
            const d = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
            showToast(RU_TEXT.T_0128, `Наряд ${orderId} назначен паре ${payload.lead_staff_id} + ${payload.partner_staff_id}`, 'success', 10000, d.undo_token || null);
            await loadAllData();
            openInspector(orderId);
          } catch (err) {
            showToast(RU_TEXT.T_0129, String(err.message || err), 'error');
          } finally {
            jvConfirmBtn.disabled = false;
          }
        };
      }
    } else {
      jvBox.style.display = 'none';
    }
  }

  const elCrew = document.getElementById('insp-crew-format');
  if (elCrew) {
    if (orderMeta && orderMeta.requires_height && orderMeta.building_type === 'PRIVATE') {
      elCrew.innerHTML = RU_TEXT.T_0130;
    } else if (orderMeta && orderMeta.requires_height) {
      elCrew.innerHTML = RU_TEXT.T_0131;
    } else {
      elCrew.innerText = RU_TEXT.T_0132;
    }
  }

  if (staffMeta) {
    activeSheetStaffId = staffMeta.staff_id;
    const pageSelect = document.getElementById('page-sheet-staff-select');
    if (pageSelect) pageSelect.value = staffMeta.staff_id;

    document.getElementById('insp-staff-name').innerText = staffMeta.display_name;
    document.getElementById('insp-staff-id').innerText = staffMeta.staff_id;
    const profText = staffMeta.profiles.includes('INSTALL') ? RU_TEXT.T_0067 : RU_TEXT.T_0068;
    document.getElementById('insp-staff-profile').innerText = profText;
    document.getElementById('insp-staff-shift').innerText = `${staffMeta.shift.start} - ${staffMeta.shift.end}`;
    document.getElementById('insp-staff-vehicle').innerText = RU_TEXT.T_0069;
    document.getElementById('insp-staff-exp').innerText = RU_TEXT.T_0133;
  } else {
    document.getElementById('insp-staff-name').innerText = RU_TEXT.T_0134;
    document.getElementById('insp-staff-id').innerText = '-';
    document.getElementById('insp-staff-profile').innerText = '-';
    document.getElementById('insp-staff-shift').innerText = '-';
    document.getElementById('insp-staff-vehicle').innerText = '-';
    document.getElementById('insp-staff-exp').innerText = '-';
  }

  if (orderMeta && orderMeta.required_equipment) {
    const reqList = [];
    if (orderMeta.required_equipment.ONT) reqList.push(`${orderMeta.required_equipment.ONT}x ONT`);
    if (orderMeta.required_equipment.ROUTER) reqList.push(`${orderMeta.required_equipment.ROUTER}x Wi-Fi роутер`);
    if (orderMeta.required_equipment.STB) reqList.push(`${orderMeta.required_equipment.STB}x ТВ-приставка`);
    document.getElementById('insp-equipment-req').innerText = reqList.length ? reqList.join(', ') : RU_TEXT.T_0024;
  } else {
    document.getElementById('insp-equipment-req').innerText = RU_TEXT.T_0135;
  }
  document.getElementById('insp-equipment-status').innerText = RU_TEXT.T_0136;

  if (assignedStop && assignedStop.explanation) {
    document.getElementById('insp-xai-explanation').innerText = assignedStop.explanation;
  } else {
    document.getElementById('insp-xai-explanation').innerText = RU_TEXT.T_0137;
  }

  function updateReassignValidationCard(staffId) {
    const card = document.getElementById('reassign-validation-card');
    const btn = document.getElementById('btn-do-reassign');
    if (!card) return;
    const c = (window.currentCandidatesMap && window.currentCandidatesMap[staffId]) || null;
    if (!c) {
      card.style.display = 'none';
      return;
    }
    card.style.display = 'block';

    const distClass = c.district_match ? 'ok' : 'warn';
    const scName = DISTRICT_NAMES[c.service_center_id] || c.service_center_id;
    const distText = c.district_match ? `${scName} (Свой район)` : `${scName} (Другой район)`;

    const heightClass = c.height_ok ? 'ok' : 'error';
    const heightText = c.height_ok ? RU_TEXT.T_0138 : RU_TEXT.T_0139;

    let eqClass = 'ok';
    let eqText = RU_TEXT.T_0140;
    let alertBanner = '';
    if (c.needs_restock) {
      eqClass = 'purple';
      const missingArr = Object.entries(c.missing_equipment).map(([k, v]) => `${v} шт ${k}`);
      eqText = `Не хватает: ${missingArr.join(', ')}`;
      alertBanner = `
      <div class="reassign-alert-banner restock">
        <b>Внимание:</b> Оборудования в автомобиле недостаточно. При назначении алгоритм автоматически включит в маршрут <b>дозаезд на склад СЦ (${c.service_center_id})</b> на 15 мин.
      </div>
    `;
    }

    const confirmedTomorrow = isOrderConfirmedTomorrow(activeInspectorOrderId);
    if (confirmedTomorrow) {
      if (btn) {
        btn.disabled = true;
        btn.classList.add('btn-disabled');
        btn.style.opacity = '0.5';
        btn.innerText = RU_TEXT.T_0247;
        btn.title = RU_TEXT.T_0243;
      }
    } else if (!c.eligible) {
      if (btn) {
        btn.disabled = true;
        btn.classList.add('btn-disabled');
        btn.style.opacity = '0.5';
        btn.innerText = RU_TEXT.T_0141;
        btn.title = c.reasons.join(', ');
      }
      alertBanner = `
      <div class="reassign-alert-banner danger">
        <b>Ограничение:</b> ${c.reasons.join('. ')}. Назначение данному сотруднику запрещено правилами безопасности.
      </div>
    `;
    } else {
      if (btn) {
        btn.disabled = false;
        btn.classList.remove('btn-disabled');
        btn.style.opacity = '1';
        btn.innerText = c.needs_restock ? RU_TEXT.T_0142 : RU_TEXT.T_0143;
        btn.title = RU_TEXT.T_0144;
      }
    }

    const loadClass = c.workload_status === 'OVERLOAD' ? 'warn' : 'ok';
    const loadText = `${c.visits_count} нарядов (${c.workload_status === 'LOW' ? 'Низкая' : c.workload_status === 'NORMAL' ? 'Штатная' : 'Повышенная'})`;
    const extraTravelText = (typeof c.extra_travel_sec === 'number')
      ? `+${Math.round(c.extra_travel_sec / 60)} мин`
      : '—';
    const extraTravelClass = (typeof c.extra_travel_sec === 'number' && c.extra_travel_sec > 1800) ? 'warn' : 'ok';
    const cvDeltaText = (typeof c.cv_delta === 'number')
      ? `${c.cv_delta >= 0 ? '+' : ''}${c.cv_delta.toFixed(3)}`
      : '—';
    const cvDeltaClass = (typeof c.cv_delta === 'number' && c.cv_delta > 0.03) ? 'warn' : 'ok';

    if (c.solver_feasible === false && !alertBanner) {
      alertBanner = `
      <div class="reassign-alert-banner danger">
        <b>Внимание (Солвер H1–H10):</b> Вставка в маршрут выбранного инженера нарушает сменное окно или перегрузку.
      </div>
    `;
    }

    const skillOk = (c.profile_ok !== false) && (c.tech_ok !== false);
    const skillClass = skillOk ? 'ok' : 'error';
    const skillText = skillOk ? 'Соответствуют' : (c.profile_ok === false ? 'Неподходящий профиль' : 'Нет технологии');

    card.innerHTML = `
    <div class="reassign-check-title">
      <span>Проверка готовности инженера:</span>
      <span style="font-size:10px; color:#64748b;">${c.staff_id}</span>
    </div>
    <div class="reassign-check-grid">
      <div class="reassign-check-item">
        <span class="reassign-check-lbl">Район:</span>
        <span class="reassign-check-val ${distClass}">${distText}</span>
      </div>
      <div class="reassign-check-item">
        <span class="reassign-check-lbl">Профиль и техн.:</span>
        <span class="reassign-check-val ${skillClass}">${skillText}</span>
      </div>
      <div class="reassign-check-item">
        <span class="reassign-check-lbl">Допуск к высоте:</span>
        <span class="reassign-check-val ${heightClass}">${heightText}</span>
      </div>
      <div class="reassign-check-item">
        <span class="reassign-check-lbl">Оборудование:</span>
        <span class="reassign-check-val ${eqClass}">${eqText}</span>
      </div>
      <div class="reassign-check-item">
        <span class="reassign-check-lbl">Загрузка:</span>
        <span class="reassign-check-val ${loadClass}">${loadText}</span>
      </div>
      <div class="reassign-check-item">
        <span class="reassign-check-lbl">Доп. пробег (солвер):</span>
        <span class="reassign-check-val ${extraTravelClass}">${extraTravelText}</span>
      </div>
      <div class="reassign-check-item">
        <span class="reassign-check-lbl">ΔCV баланса:</span>
        <span class="reassign-check-val ${cvDeltaClass}">${cvDeltaText}</span>
      </div>
    </div>
    ${alertBanner}
  `;
  }

  const reassignSelect = document.getElementById('reassign-select');
  reassignSelect.innerHTML = RU_TEXT.T_0145;

  fetch(`/api/v1/orders/${orderId}/eligible-engineers`)
    .then(r => r.json())
    .then(data => {
      window.currentCandidatesMap = {};
      reassignSelect.innerHTML = '';
      const candidates = data.candidates || [];
      candidates.forEach(c => {
        window.currentCandidatesMap[c.staff_id] = c;
      });

      const optGroup1 = document.createElement('optgroup');
      optGroup1.label = RU_TEXT.T_0146;

      const optGroup2 = document.createElement('optgroup');
      optGroup2.label = RU_TEXT.T_0147;

      const optGroup3 = document.createElement('optgroup');
      optGroup3.label = RU_TEXT.T_0148;

      const optGroup4 = document.createElement('optgroup');
      optGroup4.label = RU_TEXT.T_0149;

      let preselectedId = null;

      candidates.forEach(c => {
        const opt = document.createElement('option');
        opt.value = c.staff_id;
        const shortName = c.display_name.split(' (')[0];
        const extraTxt = (typeof c.extra_travel_sec === 'number') ? `, +${Math.round(c.extra_travel_sec / 60)} мин` : '';
        const infeasTxt = (c.solver_feasible === false) ? RU_TEXT.T_0150 : '';

        if (c.tier === 1) {
          opt.innerText = `${c.staff_id}: ${shortName} (${c.service_center_id}) - Готов, ${c.visits_count} нар.${extraTxt}${infeasTxt}`;
          optGroup1.appendChild(opt);
          if (!preselectedId) preselectedId = c.staff_id;
        } else if (c.tier === 2) {
          opt.innerText = `${c.staff_id}: ${shortName} (${c.service_center_id}) - Заезд на склад, ${c.visits_count} нар.${extraTxt}${infeasTxt}`;
          optGroup2.appendChild(opt);
          if (!preselectedId) preselectedId = c.staff_id;
        } else if (c.tier === 3 || c.tier === 4) {
          opt.innerText = `${c.staff_id}: ${shortName} (${c.service_center_id}) - ${c.visits_count} нар.${extraTxt}${infeasTxt}`;
          optGroup3.appendChild(opt);
          if (!preselectedId) preselectedId = c.staff_id;
        } else {
          opt.innerText = `${c.staff_id}: ${shortName} (${c.service_center_id}) - ${c.reasons[0] || 'Недоступен'}`;
          opt.disabled = true;
          optGroup4.appendChild(opt);
        }

        if (assignedStaff && c.staff_id === assignedStaff) {
          preselectedId = c.staff_id;
        }
      });

      if (optGroup1.children.length) reassignSelect.appendChild(optGroup1);
      if (optGroup2.children.length) reassignSelect.appendChild(optGroup2);
      if (optGroup3.children.length) reassignSelect.appendChild(optGroup3);
      if (optGroup4.children.length) reassignSelect.appendChild(optGroup4);

      if (preselectedId) {
        reassignSelect.value = preselectedId;
      }
      updateReassignValidationCard(reassignSelect.value);
    })
    .catch(err => {
      console.warn('Fallback populate reassign-select:', err);
      reassignSelect.innerHTML = '';
      if (currentPlanData) {
        currentPlanData.routes.forEach(r => {
          const sMeta = (contextData && contextData.staff) ? contextData.staff[r.staff_id] : null;
          const opt = document.createElement('option');
          opt.value = r.staff_id;
          opt.innerText = `${r.staff_id}: ${sMeta ? sMeta.display_name.split(' (')[0] : r.staff_id} (${r.service_center_id})`;
          if (assignedStaff && r.staff_id === assignedStaff) opt.selected = true;
          reassignSelect.appendChild(opt);
        });
      }
    });
}

function renderAlerts(alerts) {
  const splitContainer = document.getElementById('split-alerts-list');
  const splitCount = document.getElementById('split-alerts-cnt');
  if (splitCount) splitCount.innerText = alerts ? alerts.length : 0;

  if (!alerts || !alerts.length) {
    const emptyHtml = RU_TEXT.T_0151;
    if (splitContainer) splitContainer.innerHTML = emptyHtml;
    return;
  }

  const html = alerts.slice().reverse().map(a => {
    const timeStr = a.created_at ? a.created_at.split('T')[1].substring(0, 5) : RU_TEXT.T_0152;
    const staffMeta = (contextData && contextData.staff) ? contextData.staff[a.staff_id] : null;
    const staffName = staffMeta ? staffMeta.display_name.split(' (')[0] : a.staff_id;
    const scId = staffMeta ? staffMeta.service_center_id : 'SC-01';
    const scName = DISTRICT_NAMES[scId] || scId;

    const orderMeta = (contextData && contextData.orders) ? contextData.orders[a.order_id] : null;
    const address = orderMeta ? orderMeta.address : RU_TEXT.T_0045;
    const slotStr = orderMeta ? `${orderMeta.slot.start.split('T')[1].substring(0, 5)} - ${orderMeta.slot.end.split('T')[1].substring(0, 5)}` : '17:00 - 19:00';

    const lateness = a.lateness_min || 30;
    const isCritical = lateness >= 60;
    const rawAction = a.suggested_action;
    const workInfo = (orderMeta && (orderMeta.technology || orderMeta.work_type_code)) || RU_TEXT.T_0242;
    const actionLabel = rawAction === 'REASSIGN'
      ? RU_TEXT.T_0153
      : ((!rawAction || rawAction === 'NONE') ? workInfo : rawAction);

    return `
      <div class="alert-item-card ${isCritical ? 'critical' : ''}" data-staff-id="${a.staff_id || ''}" data-order-id="${a.order_id || ''}" onclick="focusSplitMapOnAlert('${a.staff_id}', '${a.order_id}')" title="Кликните, чтобы показать наряд на карте">
        <div class="alert-item-header">
          <div class="alert-badge-group">
            <span class="alert-time-pill">${timeStr}</span>
            <span class="alert-type-pill">Риск срыва слота</span>
          </div>
          <span class="alert-action-pill">${actionLabel}</span>
        </div>

        <div class="alert-grid">
          <div class="alert-row">
            <span class="alert-lbl">Инженер:</span>
            <span class="alert-val"><b>${staffName}</b> (${a.staff_id}) &bull; ${scName}</span>
          </div>
          <div class="alert-row">
            <span class="alert-lbl">Наряд:</span>
            <span class="alert-val"><b style="color:var(--primary);">${a.order_id}</b> &bull; ${address}</span>
          </div>
          <div class="alert-row">
            <span class="alert-lbl">Слот клиента:</span>
            <span class="alert-val"><b>${slotStr}</b> &bull; Задержка: <span class="text-danger bold">+${lateness} мин</span></span>
          </div>
        </div>

        <div class="alert-desc">
          ${a.message || `Инженер ${staffName} задержался на адресе. Прогнозируется срыв согласованного окна.`}
        </div>

        <div class="alert-card-footer">
          <button type="button" class="btn btn-sm btn-outline" onclick="event.stopPropagation(); focusSplitMapOnAlert('${a.staff_id}', '${a.order_id}')">
            Показать на карте
          </button>
          <button type="button" class="btn btn-sm btn-primary" onclick="event.stopPropagation(); openInspector('${a.order_id}')">
            Карточка наряда
          </button>
        </div>
      </div>
    `;
  }).join('');

  if (splitContainer) {
    splitContainer.innerHTML = html;
    const routes = (currentPlanData && currentPlanData.routes) ? currentPlanData.routes : [];
    splitContainer.querySelectorAll('.alert-item-card').forEach(cardEl => {
      const stId = cardEl.dataset.staffId;
      const fmRoute = stId ? routes.find(r => r.staff_id === stId && r.status === 'UNAVAILABLE') : null;
      if (fmRoute) {
        cardEl.removeAttribute('title');
        attachFmHoverTooltip(cardEl, getUnavailableTooltipText(fmRoute));
      }
    });
  }
}

function formatPGPAction(action) {
  if (action === 'FORWARD_TO_STATION') {
    return RU_TEXT.T_0155;
  }
  return action || RU_TEXT.T_0156;
}

function togglePgpOrders(groupId) {
  const drawer = document.getElementById(`pgp-orders-${groupId}`);
  const btn = document.getElementById(`btn-toggle-${groupId}`);
  if (!drawer) return;

  if (drawer.style.display === 'none' || !drawer.style.display) {
    drawer.style.display = 'block';
    if (btn) btn.innerHTML = btn.innerHTML.replace(RU_TEXT.T_0157, RU_TEXT.T_0158).replace('▼', '▲');
  } else {
    drawer.style.display = 'none';
    if (btn) btn.innerHTML = btn.innerHTML.replace(RU_TEXT.T_0158, RU_TEXT.T_0157).replace('▲', '▼');
  }
}

function renderPGP(groups) {
  const container = document.getElementById('pgp-list');
  if (!container) return;

  if (!groups || !groups.length) {
    container.innerHTML = RU_TEXT.T_0159;
    return;
  }

  container.innerHTML = '';
  groups.forEach(g => {
    const card = document.createElement('div');
    card.className = 'pgp-card';

    const actionLabel = formatPGPAction(g.action);
    const hoursSaved = (g.order_ids.length * 1.5).toFixed(1);
    const ordersTooltip = g.order_ids.join(', ');

    const ordersTableRows = g.order_ids.map(ordId => {
      const oMeta = (contextData && contextData.orders) ? contextData.orders[ordId] : null;
      const addr = oMeta ? oMeta.address : RU_TEXT.T_0045;
      const slot = oMeta ? `${oMeta.slot.start.split('T')[1].substring(0, 5)} - ${oMeta.slot.end.split('T')[1].substring(0, 5)}` : '-';
      const tech = oMeta ? oMeta.technology : 'PON';
      return `
        <tr>
          <td data-label="ID наряда"><b>${ordId}</b></td>
          <td data-label="Адрес">
            <div style="display:flex; align-items:center; justify-content:space-between; gap:6px;">
              <span>${addr}</span>
              <button type="button" class="btn-map-loc" onclick="showOnAppMap('${ordId}')" title="Показать объект на интерактивной карте">На карте</button>
            </div>
          </td>
          <td data-label="Слот">${slot}</td>
          <td data-label="Технология"><span class="tech-badge">${tech}</span></td>
          <td data-label="Действие"><button type="button" class="btn btn-outline btn-sm" onclick="openInspector('${ordId}')">Карточка</button></td>
        </tr>
      `;
    }).join('');

    card.innerHTML = `
      <div class="pgp-header">
        <div class="pgp-title-wrap">
          <span class="pgp-badge">Групповая авария: ${g.group_id}</span>
          <span class="pgp-node-name">Коммутатор узла доступа: <b>${g.access_node_id}</b></span>
        </div>
        <span class="pgp-action-badge" title="Заявки переданы дежурному станционному инженеру. Выезды абонентских бригад отменены.">${actionLabel}</span>
      </div>

      <div class="pgp-meta-bar">
        <div class="pgp-metric">
          <span class="pgp-metric-label">Объединено жалоб:</span>
          <span class="pgp-metric-val bold text-primary" title="Наряды: ${ordersTooltip}">${g.order_ids.length} нарядов</span>
        </div>
        <div class="pgp-metric">
          <span class="pgp-metric-label">Экономия смены:</span>
          <span class="pgp-metric-val bold text-success">~${hoursSaved} нормо-часов</span>
        </div>
        <div class="pgp-metric-action">
          <button type="button" class="btn btn-outline btn-sm pgp-toggle-btn" onclick="togglePgpOrders('${g.group_id}')" id="btn-toggle-${g.group_id}">
            Показать состав нарядов (${g.order_ids.length}) ▼
          </button>
        </div>
      </div>

      <div class="pgp-comment">
        ${g.comment || 'Обнаружены множественные неисправности на узле доступа. Устранение назначено дежурной станционной службе без выезда выездных бригад.'}
      </div>

      <div class="pgp-orders-drawer pgp-table-container" id="pgp-orders-${g.group_id}" style="display: none;">
        <div class="pgp-drawer-title">Наряды абонентов, привязанные к аварийному узлу ${g.access_node_id}:</div>
        <table class="data-table pgp-table">
          <thead>
            <tr>
              <th>ID наряда</th>
              <th>Адрес абонента</th>
              <th>Согласованный слот</th>
              <th>Технология</th>
              <th>Действие</th>
            </tr>
          </thead>
          <tbody>
            ${ordersTableRows}
          </tbody>
        </table>
      </div>
    `;
    container.appendChild(card);
  });
}

function renderUnassigned(unassigned) {
  const tbody = document.getElementById('unassigned-rows');
  const splitTbody = document.getElementById('split-unassigned-rows');
  const navBadge = document.getElementById('nav-unassigned-count');
  if (navBadge) navBadge.innerText = unassigned.length;

  if (!unassigned.length) {
    selectedUnassignedIds.clear();
    updateSelectedUnassignedControls();
    const emptyRow = RU_TEXT.T_0160;
    if (tbody) tbody.innerHTML = emptyRow;
    if (splitTbody) splitTbody.innerHTML = RU_TEXT.T_0161;
    return;
  }

  const validIds = new Set(unassigned.map(u => u.order_id));
  Array.from(selectedUnassignedIds).forEach(id => {
    if (!validIds.has(id)) selectedUnassignedIds.delete(id);
  });

  let rowsHtml = '';
  let splitRowsHtml = '';

  unassigned.forEach(u => {
    const orderMeta = (contextData && contextData.orders) ? contextData.orders[u.order_id] : null;
    const address = orderMeta ? orderMeta.address : RU_TEXT.T_0045;
    const scId = getOrderDistrict(orderMeta);
    const scName = DISTRICT_NAMES[scId] || RU_TEXT.T_0162;
    const slotStr = orderMeta ? `${orderMeta.slot.start.split('T')[1].substring(0, 5)} - ${orderMeta.slot.end.split('T')[1].substring(0, 5)}` : '09:00 - 11:00';
    const isConfirmed = (u.suggested_action === 'CONFIRMED_FOR_TOMORROW' || (u.reason_text && u.reason_text.includes(RU_TEXT.T_0103)));
    if (isConfirmed) selectedUnassignedIds.delete(u.order_id);
    const isChecked = !isConfirmed && selectedUnassignedIds.has(u.order_id);
    const nextShort = getNextWorkdayShortLabel();

    const reasonText = u.reason_text || `Перенос на ${nextShort}`;
    const statusBadge = isConfirmed
      ? `<div style="display:flex; flex-direction:column; gap:4px; align-items:flex-start;">
           ${u.reason_text ? `<span class="status-badge-resched" title="${u.reason_text}">${u.reason_text}</span>` : ''}
           <span class="status-badge-confirmed" title="Перенос согласован с клиентом">Согласовано на ${nextShort}</span>
         </div>`
      : `<span class="status-badge-resched" title="${reasonText}">${reasonText}</span>`;

    const searchBlob = `${u.order_id} ${address} ${scName} ${slotStr} ${u.reason_text || ''}`.toLowerCase();

    rowsHtml += `
      <tr data-order-id="${u.order_id}" data-district="${scId}" data-search="${searchBlob}">
        <td style="text-align:center;"><input type="checkbox" class="unassigned-row-chk" data-order-id="${u.order_id}" ${isChecked ? 'checked' : ''} ${isConfirmed ? 'disabled' : ''} style="accent-color:#7700ff; cursor:pointer;"></td>
        <td data-label="Наряд"><b><a href="javascript:void(0)" class="clickable-id" onclick="openInspector('${u.order_id}')">${u.order_id}</a></b></td>
        <td data-label="Адрес">
          <div style="display:flex; align-items:center; justify-content:space-between; gap:6px;">
            <div>
              <div style="font-weight:600; color:#1e293b;">${address}</div>
              <div style="font-size:11px; color:#64748b; margin-top:2px;">${scName}</div>
            </div>
            <button type="button" class="btn-map-loc" onclick="showOnAppMap('${u.order_id}')" title="Показать объект на интерактивной карте">На карте</button>
          </div>
        </td>
        <td data-label="Слот" style="font-size:12px; font-weight:600; color:#475569;">${slotStr}</td>
        <td data-label="Статус">${statusBadge}</td>
        <td data-label="Действия" style="text-align:center;">
          <div class="unassigned-btn-group">
            <button type="button" class="btn btn-primary btn-action-unassigned${isConfirmed ? ' btn-disabled' : ''}" ${isConfirmed ? `disabled title="${RU_TEXT.T_0243}"` : `onclick="quickAssignUnassignedOrder('${u.order_id}')" title="Встроить наряд в график инженера сегодня"`}>В смену</button>
            <button type="button" class="btn btn-warning btn-action-unassigned${isConfirmed ? ' btn-disabled' : ''}" ${isConfirmed ? `disabled title="${RU_TEXT.T_0243}"` : `onclick="confirmOrderReschedule('${u.order_id}')" title="Согласовать перенос на завтра (${nextShort})"`}>На завтра</button>
            <button type="button" class="btn btn-outline btn-action-unassigned" onclick="openInspector('${u.order_id}')" title="Открыть подробную карточку наряда">Карточка</button>
          </div>
        </td>
      </tr>
    `;

    splitRowsHtml += `
      <tr>
        <td data-label="Наряд" style="font-weight:700;"><a href="javascript:void(0)" class="clickable-id" onclick="openInspector('${u.order_id}')">${u.order_id}</a></td>
        <td data-label="Адрес объекта">
          <div style="font-weight:600; color:#1e293b; font-size:11.5px;">${address}</div>
          <div style="font-size:10.5px; color:#64748b; margin-top:2px;">${scName}</div>
        </td>
        <td data-label="Слот" style="text-align:center; font-size:11.5px; font-weight:600; color:#475569;">${slotStr}</td>
        <td data-label="Статус переноса" style="text-align:center;">${statusBadge}</td>
        <td data-label="Карта" style="text-align:center;">
          <button type="button" class="btn-map-loc" onclick="showOnAppMap('${u.order_id}')" title="Показать объект на соседней карте">На карте</button>
        </td>
      </tr>
    `;
  });

  if (tbody) {
    tbody.innerHTML = rowsHtml;
    tbody.querySelectorAll('.unassigned-row-chk').forEach(chk => {
      chk.addEventListener('change', () => {
        const oid = chk.dataset.orderId;
        if (chk.checked) selectedUnassignedIds.add(oid);
        else selectedUnassignedIds.delete(oid);
        updateSelectedUnassignedControls();
      });
    });
  }
  if (splitTbody) splitTbody.innerHTML = splitRowsHtml;
  updateSelectedUnassignedControls();
}

function updateSelectedUnassignedControls() {
  const btn = document.getElementById('btn-bulk-reschedule-selected');
  if (btn) {
    const cnt = selectedUnassignedIds.size;
    btn.disabled = cnt === 0;
    btn.innerText = `Перенести выбранные (${cnt})`;
  }
  const selectAll = document.getElementById('unassigned-select-all');
  if (selectAll) {
    const visibleChks = Array.from(document.querySelectorAll('#unassigned-rows tr'))
      .filter(r => r.style.display !== 'none')
      .map(r => r.querySelector('.unassigned-row-chk'))
      .filter(c => c && !c.disabled);
    selectAll.checked = visibleChks.length > 0 && visibleChks.every(c => c.checked);
  }
}

function bulkRescheduleSelected() {
  const ids = Array.from(selectedUnassignedIds);
  if (!ids.length || !currentPlanData) {
    showToast('Выбор не задан', 'Отметьте галочками хотя бы один наряд в таблице', 'info');
    return;
  }
  const nextDateLabel = getNextWorkdayLabel();
  showConfirmDialog(
    'Выборочный перенос очереди',
    `Перенести выбранные наряды (${ids.length} шт.) на следующий рабочий день (${nextDateLabel}) с проверкой слотов и маршрутов?`,
    'Перенести выбранные',
    () => {
      fetch('/api/v1/ui/unassigned/reschedule', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          order_ids: ids,
          plan_version: currentPlanData.plan_version
        })
      })
        .then(async res => {
          const data = await res.json().catch(() => ({}));
          if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
          return data;
        })
        .then(data => {
          selectedUnassignedIds.clear();
          showToast(
            'Выборочный перенос выполнен',
            `Перенесено: ${data.moved || 0}, пропущено: ${data.skipped || 0}`,
            'success',
            10000,
            data.undo_token || null
          );
          loadAllData();
        })
        .catch(err => showToast('Ошибка выборочного переноса', String(err.message || err), 'error'));
    }
  );
}

function filterUnassignedTable() {
  const searchInput = document.getElementById('unassigned-search');
  const distSelect = document.getElementById('unassigned-filter-district');
  const q = searchInput ? searchInput.value.toLowerCase().trim() : '';
  const dist = distSelect ? distSelect.value : 'all';

  const rows = document.querySelectorAll('#unassigned-rows tr');
  let visibleCount = 0;
  rows.forEach(row => {
    const rowDist = row.dataset.district;
    const rowSearch = row.dataset.search || row.innerText.toLowerCase();
    const matchDist = (dist === 'all' || rowDist === dist);
    const matchText = (!q || rowSearch.includes(q));
    row.style.display = (matchDist && matchText) ? '' : 'none';
    if (row.style.display !== 'none') visibleCount++;
  });
  updateSelectedUnassignedControls();
  return visibleCount;
}

function refreshOrderBoard() {
  const listEl = document.getElementById('dispatcher-order-board-list');
  const tabCntEl = document.getElementById('board-tab-count');
  if (!listEl && !tabCntEl) return;
  fetch('/api/v1/ui/order-cards')
    .then(r => (r.ok ? r.json() : null))
    .then(data => {
      if (!data || !Array.isArray(data.orders)) return;
      orderBoardItems = data.orders;
      if (tabCntEl) tabCntEl.innerText = String(orderBoardItems.length);
      renderOrderBoardList();
    })
    .catch(err => console.warn('Order board load error:', err));
}

const ORDER_BOARD_STATUS_LABELS = {
  PLANNED: 'В плане',
  IN_PROGRESS: 'В работе',
  DONE: 'Выполнен',
  UNASSIGNED: 'К переносу',
  PGP: 'Групповая',
  CANCELLED: 'Отменён',
};

function formatOrderBoardStatus(status) {
  return ORDER_BOARD_STATUS_LABELS[status] || status || '—';
}

const ORDER_BOARD_WORK_TYPE_FALLBACK = {
  INST_PON_MKD: 'Инсталляция ШПД/IPTV, МКД, PON',
  INST_FTTB_MKD: 'Инсталляция ШПД/IPTV, МКД, FTTB',
  INST_PON_PRIVATE_VLS: 'Инсталляция, частный сектор, с ВЛС',
  ADD_SERVICE: 'Доподключение услуги (IPTV, Wi-Fi)',
  SURVEY_PRIVATE: 'Обследование (частный сектор)',
  REPAIR: 'Устранение неисправности (обращение абонента)',
  DEGRADATION: 'Устранение деградации',
  DISMANTLE: 'Демонтаж АО',
};

function formatOrderBoardWorkType(code) {
  if (!code) return '';
  const meta = contextData && contextData.work_types ? contextData.work_types[code] : null;
  if (meta && meta.name) return meta.name;
  return ORDER_BOARD_WORK_TYPE_FALLBACK[code] || code;
}

function renderOrderBoardList() {
  const listEl = document.getElementById('dispatcher-order-board-list');
  const cntEl = document.getElementById('board-count-label');
  if (!listEl) return;

  const q = (document.getElementById('board-search')?.value || '').toLowerCase().trim();
  const statusVal = document.getElementById('board-status-filter')?.value || 'ALL';
  const heightOnly = Boolean(document.getElementById('board-height-only')?.checked);

  const filtered = orderBoardItems.filter(item => {
    if (statusVal !== 'ALL' && item.status !== statusVal) return false;
    if (heightOnly && !item.requires_height) return false;
    if (orderBoardCategory === 'INST' && !(item.work_type || '').startsWith('INST')) return false;
    if (orderBoardCategory === '3LTP' && !['REPAIR', 'DEGRADATION', 'LINE_REPAIR', 'SURVEY', 'DISMANTLE'].includes(item.work_type || '')) return false;
    if (orderBoardCategory === 'RISK' && !item.risky) return false;
    if (orderBoardCategory === 'UNASSIGNED' && item.status !== 'UNASSIGNED') return false;
    if (q) {
      const wtRu = formatOrderBoardWorkType(item.work_type);
      const hay = `${item.id || ''} ${item.address || ''} ${item.staff_id || ''} ${item.staff_name || ''} ${item.work_type || ''} ${wtRu}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  if (cntEl) cntEl.innerText = `Показано: ${filtered.length} из ${orderBoardItems.length}`;
  if (!filtered.length) {
    listEl.innerHTML = '<div style="font-size:12px; color:#64748b; padding:12px; text-align:center;">Нет нарядов по выбранным фильтрам</div>';
    return;
  }

  const slice = filtered.slice(0, 120);
  listEl.innerHTML = slice.map(item => {
    const slotTxt = item.slot && item.slot.start ? `${String(item.slot.start).slice(11, 16)}–${String(item.slot.end).slice(11, 16)}` : '';
    const staffTxt = item.staff_id ? `${item.staff_id}` : 'Не назначен';
    const heightTag = item.requires_height ? ' · ВЛС' : '';
    const workTypeRu = formatOrderBoardWorkType(item.work_type);
    return `
      <div class="order-board-card${item.risky ? ' is-risk' : ''}" data-order-id="${item.id}">
        <div class="order-board-card-top">
          <span class="order-board-id">${item.id}${heightTag}</span>
          <span class="order-board-status status-${item.status}">${formatOrderBoardStatus(item.status)}</span>
        </div>
        <div class="order-board-addr">${item.address || ''}</div>
        <div class="order-board-meta">
          <span>${workTypeRu} · ${slotTxt}</span>
          <span><b>${staffTxt}</b></span>
        </div>
      </div>
    `;
  }).join('');

  listEl.querySelectorAll('.order-board-card').forEach(card => {
    card.addEventListener('click', () => {
      const oid = card.dataset.orderId;
      if (!oid) return;
      showOnAppMap(oid);
      openInspector(oid);
    });
  });
}

function quickAssignUnassignedOrder(orderId) {
  if (!orderId || !currentPlanData) return;
  if (isOrderConfirmedTomorrow(orderId)) {
    showToast(RU_TEXT.T_0247, RU_TEXT.T_0243, 'warning');
    return;
  }

  fetch(`/api/v1/orders/${orderId}/eligible-engineers`)
    .then(r => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    })
    .then(data => {
      const candidates = data.candidates || [];
      const eligible = candidates.filter(c => c.eligible);
      if (!eligible.length) {
        showToast(RU_TEXT.T_0163, RU_TEXT.T_0164, 'warning');
        return;
      }

      const best = eligible.find(c => c.solver_feasible) || eligible[0];
      const eventPayload = {
        event_id: `EV-QUICK-${Date.now()}`,
        type: 'ORDER_TRANSFERRED',
        timestamp: getCurrentEventTimestamp(),
        order_id: orderId,
        to_staff_id: best.staff_id,
        auto_fallback: true
      };

      fetch('/api/v1/events', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(eventPayload)
      })
        .then(async res => {
          const resData = await res.json().catch(() => ({}));
          if (!res.ok || resData.accepted === false) {
            throw new Error(resData.detail || resData.message || RU_TEXT.T_0166);
          }
          const assignedStaff = (resData.changed_staff_ids && resData.changed_staff_ids[0]) || (resData.affected_staff_ids && resData.affected_staff_ids[0]) || best.staff_id;
          if (best.needs_restock) {
            showToast(RU_TEXT.T_0167, `Заявка ${orderId} назначена ${assignedStaff}. Автоматически спланирован заезд на склад СЦ за оборудованием!`, 'success', 10000, resData.undo_token || null);
          } else {
            showToast(RU_TEXT.T_0167, `Заявка ${orderId} назначена ${assignedStaff} (оборудование есть в рюкзаке)`, 'success', 10000, resData.undo_token || null);
          }
          loadAllData();
          if (activeInspectorOrderId === orderId) {
            setTimeout(() => openInspector(orderId), 300);
          }
        })
        .catch(err => showToast(RU_TEXT.T_0165, err.message || String(err), 'error'));
    })
    .catch(err => {
      console.warn('Fallback quick assign:', err);
      const orderMeta = (contextData && contextData.orders) ? contextData.orders[orderId] : null;
      const targetSc = getOrderDistrict(orderMeta);
      let candidates = currentPlanData.routes.filter(r => r.status !== 'UNAVAILABLE');
      if (targetSc) {
        const scRoutes = candidates.filter(r => r.service_center_id === targetSc);
        if (scRoutes.length) candidates = scRoutes;
      }
      candidates.sort((a, b) => a.stops.length - b.stops.length);
      if (!candidates.length) {
        showToast(RU_TEXT.T_0165, RU_TEXT.T_0164, 'error');
        return;
      }
      const bestStaff = candidates[0].staff_id;

      const eventPayload = {
        event_id: `EV-QUICK-${Date.now()}`,
        type: 'ORDER_TRANSFERRED',
        timestamp: getCurrentEventTimestamp(),
        order_id: orderId,
        to_staff_id: bestStaff,
        auto_fallback: true
      };

      fetch('/api/v1/events', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(eventPayload)
      })
        .then(async res => {
          const resData = await res.json().catch(() => ({}));
          if (!res.ok || resData.accepted === false) {
            throw new Error(resData.detail || resData.message || RU_TEXT.T_0166);
          }
          const assignedStaff = (resData.changed_staff_ids && resData.changed_staff_ids[0]) || (resData.affected_staff_ids && resData.affected_staff_ids[0]) || bestStaff;
          showToast(RU_TEXT.T_0167, `Заявка ${orderId} назначена инженеру ${assignedStaff}`, 'success', 10000, resData.undo_token || null);
          loadAllData();
          if (activeInspectorOrderId === orderId) {
            setTimeout(() => openInspector(orderId), 300);
          }
        })
        .catch(e => showToast(RU_TEXT.T_0165, e.message || String(e), 'error'));
    });
}

function confirmOrderReschedule(orderId) {
  if (!orderId) return;
  if (isOrderConfirmedTomorrow(orderId)) {
    showToast(RU_TEXT.T_0247, RU_TEXT.T_0243, 'warning');
    return;
  }
  const nextDateLabel = getNextWorkdayLabel();

  const eventPayload = {
    event_id: 'EV-RESCHED-' + Date.now().toString().slice(-4),
    type: 'RESCHEDULE_TO_NEXT_DAY',
    timestamp: getCurrentEventTimestamp(),
    order_id: orderId
  };

  fetch('/api/v1/events', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(eventPayload)
  })
    .then(res => res.json())
    .then(resData => {
      showToast(RU_TEXT.T_0169, `Наряд ${orderId}: согласован выезд на ${nextDateLabel}`, 'success', 10000, resData.undo_token || null);
      loadAllData();
      if (activeInspectorOrderId === orderId) {
        setTimeout(() => openInspector(orderId), 300);
      }
    })
    .catch(err => showToast(RU_TEXT.T_0170, String(err), 'error'));
}

function bulkRescheduleAll() {
  if (!currentPlanData || !currentPlanData.unassigned || !currentPlanData.unassigned.length) {
    showToast(RU_TEXT.T_0171, RU_TEXT.T_0172, 'info');
    return;
  }

  const count = currentPlanData.unassigned.length;
  const nextDateLabel = getNextWorkdayLabel();
  showConfirmDialog(
    RU_TEXT.T_0173,
    `Согласовать перенос всей очереди (${count} нарядов) на следующий рабочий день (${nextDateLabel})?`,
    RU_TEXT.T_0174,
    () => {
      fetch('/api/v1/events', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          event_id: 'EV-BULK-' + Date.now().toString().slice(-4),
          type: 'RESCHEDULE_ALL_BACKLOG',
          timestamp: getCurrentEventTimestamp()
        })
      })
        .then(res => res.json())
        .then(resData => {
          showToast(RU_TEXT.T_0175, `Все наряды (${count} шт.) согласованы к переносу на ${nextDateLabel}`, 'success', 10000, resData.undo_token || null);
          loadAllData();
        })
        .catch(err => showToast(RU_TEXT.T_0176, String(err), 'error'));
    }
  );
}

function addDutyCrews() {
  showConfirmDialog(
    RU_TEXT.T_0177,
    RU_TEXT.T_0178,
    RU_TEXT.T_0179,
    () => {
      fetch('/api/v1/events', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          event_id: 'EV-DUTY-' + Date.now().toString().slice(-4),
          type: 'ADD_DUTY_CREW',
          timestamp: getCurrentEventTimestamp()
        })
      })
        .then(res => res.json())
        .then(resData => {
          const msg = resData.message || `Дежурные бригады выведены. В работу принято нарядов: ${resData.assigned_count ?? 0}.`;
          showToast(RU_TEXT.T_0180, msg, 'success', 10000, resData.undo_token || null);
          loadAllData();
          switchView('gantt');
          if (Array.isArray(resData.created_staff_ids) && resData.created_staff_ids.length) {
            setTimeout(() => highlightGanttStaff(resData.created_staff_ids[0]), 300);
          }
        })
        .catch(err => showToast(RU_TEXT.T_0181, String(err), 'error'));
    }
  );
}

function initSplitMap() {
  if (splitMap) return;
  const splitContainer = document.getElementById('split-map');
  if (!splitContainer) return;

  splitMap = L.map('split-map', {
    attributionControl: false
  }).setView([56.8450, 60.6050], 12);

  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 18,
    attribution: 'OpenStreetMap'
  }).addTo(splitMap);
  attachOfflineStreetsLayer(splitMap);

  if (currentPlanData) {
    populateSplitCrewSelect(currentPlanData.routes, currentPlanData.alerts || []);
    renderRoutesOnSplitMap(currentPlanData, activeSplitStaffId);
  }
}

function populateSplitCrewSelect(routes, alerts = []) {
  const select = document.getElementById('split-crew-select');
  if (!select) return;
  const currentVal = activeSplitStaffId;
  select.innerHTML = RU_TEXT.T_0182;

  const staffAlertCount = {};
  alerts.forEach(a => {
    staffAlertCount[a.staff_id] = (staffAlertCount[a.staff_id] || 0) + 1;
  });

  const filteredRoutes = activeSplitCenterId === 'all'
    ? routes
    : routes.filter(r => r.service_center_id === activeSplitCenterId);

  const sortedRoutes = filteredRoutes.slice().sort((a, b) => {
    const ac = staffAlertCount[a.staff_id] || 0;
    const bc = staffAlertCount[b.staff_id] || 0;
    if (ac !== bc) return bc - ac;
    return a.staff_id.localeCompare(b.staff_id);
  });

  sortedRoutes.forEach(r => {
    const sMeta = (contextData && contextData.staff) ? contextData.staff[r.staff_id] : null;
    const name = sMeta ? sMeta.display_name.split(' (')[0] : r.staff_id;
    const scName = DISTRICT_NAMES[r.service_center_id] ? DISTRICT_NAMES[r.service_center_id].split(' (')[0] : r.service_center_id;
    const opt = document.createElement('option');
    opt.value = r.staff_id;
    const aCnt = staffAlertCount[r.staff_id];
    if (aCnt) {
      opt.innerText = `(!) ${r.staff_id}: ${name} (${aCnt} риска срыва)`;
      opt.style.color = '#dc2626';
      opt.style.fontWeight = 'bold';
    } else {
      opt.innerText = `${r.staff_id}: ${name} (${scName})`;
    }
    if (r.staff_id === currentVal) opt.selected = true;
    select.appendChild(opt);
  });
}

function renderRoutesOnSplitMap(plan, selectedStaffId = 'all', highlightOrderId = null) {
  if (!splitMap || !plan) return;
  activeSplitStaffId = selectedStaffId;

  const crewSelect = document.getElementById('split-crew-select');
  if (crewSelect && crewSelect.value !== selectedStaffId) {
    crewSelect.value = selectedStaffId;
  }

  const visibleRoutes = activeSplitCenterId === 'all'
    ? plan.routes
    : plan.routes.filter(r => r.service_center_id === activeSplitCenterId);
  const visibleStaffIds = new Set(visibleRoutes.map(r => r.staff_id));
  const visibleAlerts = (plan.alerts || []).filter(a => visibleStaffIds.has(a.staff_id));

  const badge = document.getElementById('split-map-status-badge');
  if (badge) {
    if (selectedStaffId === 'all') {
      const aCnt = visibleAlerts.length;
      const scLabel = activeSplitCenterId === 'all' ? 'Обзор города' : (DISTRICT_NAMES[activeSplitCenterId] || activeSplitCenterId).split(' (')[0];
      badge.innerText = `${scLabel} (${visibleRoutes.length} бригад, рисков: ${aCnt})`;
    } else {
      const sMeta = (contextData && contextData.staff) ? contextData.staff[selectedStaffId] : null;
      badge.innerText = sMeta ? sMeta.display_name.split(' (')[0] : selectedStaffId;
    }
  }

  splitRouteLayers.forEach(l => splitMap.removeLayer(l));
  splitMarkerLayers.forEach(l => splitMap.removeLayer(l));
  splitRouteLayers = [];
  splitMarkerLayers = [];

  const alertOrderMap = {};
  visibleAlerts.forEach(a => {
    alertOrderMap[a.order_id] = a;
  });

  Object.keys(DISTRICT_COORDS).forEach(scId => {
    if (activeSplitCenterId !== 'all' && scId !== activeSplitCenterId) return;
    const coords = DISTRICT_COORDS[scId];
    const scIcon = L.divIcon({
      html: `<div class="map-pin pin-sc">${scId.replace('SC-0', 'СЦ-')}</div>`,
      className: 'custom-pin-container',
      iconSize: [22, 22]
    });
    const m = L.marker(coords, { icon: scIcon }).addTo(splitMap);
    splitMarkerLayers.push(m);
  });

  if (selectedStaffId === 'all') {
    const alertStaffIds = new Set(visibleAlerts.map(a => a.staff_id));

    visibleRoutes.forEach(route => {
      const center = DISTRICT_COORDS[route.service_center_id] || [56.8335, 60.6010];
      const isProblemStaff = alertStaffIds.has(route.staff_id);
      const staffPoints = [];

      if (isProblemStaff) {
        staffPoints.push(center);
      }

      route.stops.forEach(s => {
        if (s.type !== 'VISIT') return;
        const coords = getOrderCoordinates(s.order_id, center);
        const lat = coords[0];
        const lon = coords[1];

        if (isProblemStaff) {
          staffPoints.push([lat, lon]);
        }

        const isAlertOrder = Boolean(alertOrderMap[s.order_id]);
        const isHighlight = (s.order_id === highlightOrderId);
        const endMin = parseTimeToMin(s.end || s.start);

        let pinHtml = `<div class="map-pin pin-dot" data-visit-minute="${endMin}"></div>`;
        let pinSize = [10, 10];

        if (isAlertOrder || isHighlight) {
          pinHtml = `<div class="map-pin pin-risk" data-visit-minute="${endMin}" title="Риск срыва слота!">!</div>`;
          pinSize = [22, 22];
        }

        const dotIcon = L.divIcon({
          html: pinHtml,
          className: 'custom-pin-container',
          iconSize: pinSize
        });

        const marker = L.marker([lat, lon], { icon: dotIcon }).addTo(splitMap);
        marker.bindPopup(`
          <b>Наряд: ${s.order_id}</b><br>
          Инженер: <b>${route.staff_id}</b><br>
          ${isAlertOrder ? '<span style="color:#dc2626; font-weight:700;">Критический риск срыва слота!</span><br>' : ''}
          <a href="javascript:void(0)" onclick="openInspector('${s.order_id}')">Открыть карточку наряда</a>
        `);
        marker.on('click', () => openInspector(s.order_id));
        splitMarkerLayers.push(marker);
      });

      if (isProblemStaff && staffPoints.length > 1) {
        staffPoints.push(center);
        let probPolyline = L.polyline(staffPoints, {
          color: '#dc2626',
          weight: 3.5,
          opacity: 0.85,
          dashArray: '6, 6'
        }).addTo(splitMap);
        splitRouteLayers.push(probPolyline);

        fetch(`/api/v1/routing/track/${route.staff_id}`)
          .then(r => r.json())
          .then(track => {
            if (track && track.coordinates && track.coordinates.length > 1) {
              splitMap.removeLayer(probPolyline);
              const osrmProb = L.polyline(track.coordinates, {
                color: '#dc2626',
                weight: 4,
                opacity: 0.95,
                lineCap: 'round',
                lineJoin: 'round'
              }).addTo(splitMap);
              splitRouteLayers.push(osrmProb);
            }
          })
          .catch(() => { });
      }
    });

    return;
  }

  const targetRoute = plan.routes.find(r => r.staff_id === selectedStaffId);
  if (!targetRoute) return;

  const center = DISTRICT_COORDS[targetRoute.service_center_id] || [56.8450, 60.6050];
  const routePoints = [];
  const bounds = L.latLngBounds([]);
  let visitNum = 0;
  let targetMarkerToOpen = null;

  targetRoute.stops.forEach((s) => {
    let lat, lon;
    let pinClass = 'pin-num';
    let pinLabel = '';

    if (s.type === 'DEPOT_START' || s.type === 'DEPOT_END') {
      lat = center[0];
      lon = center[1];
      pinClass = 'pin-sc';
      pinLabel = s.type === 'DEPOT_START' ? RU_TEXT.T_0079 : RU_TEXT.T_0080;
    } else if (s.type === 'BREAK') {
      const lastPt = routePoints.length ? routePoints[routePoints.length - 1] : center;
      lat = lastPt[0] + 0.0005;
      lon = lastPt[1] + 0.0005;
      pinClass = 'pin-lunch';
      pinLabel = RU_TEXT.T_0081;
    } else if (s.type === 'RESTOCK') {
      lat = center[0];
      lon = center[1];
      pinClass = 'pin-sc';
      pinLabel = RU_TEXT.T_0079;
    } else {
      visitNum++;
      const isAlertOrder = Boolean(alertOrderMap[s.order_id]);
      const isHighlight = (s.order_id === highlightOrderId);

      if (isAlertOrder || isHighlight) {
        pinClass = 'pin-risk';
        pinLabel = '!';
      } else {
        pinClass = 'pin-num';
        pinLabel = String(visitNum);
      }

      const coords = getOrderCoordinates(s.order_id, center);
      lat = coords[0];
      lon = coords[1];
    }

    routePoints.push([lat, lon]);
    bounds.extend([lat, lon]);

    const visitMinAttr = s.type === 'VISIT' ? ` data-visit-minute="${parseTimeToMin(s.end || s.start)}"` : '';
    const icon = L.divIcon({
      html: `<div class="map-pin ${pinClass}"${visitMinAttr}>${pinLabel}</div>`,
      className: 'custom-pin-container',
      iconSize: pinClass === 'pin-lunch' ? [34, 18] : [22, 22]
    });

    const marker = L.marker([lat, lon], { icon }).addTo(splitMap);
    if (s.order_id) {
      marker.bindPopup(`
        <b>Остановка #${visitNum}: Наряд ${s.order_id}</b><br>
        Время: ${s.start ? s.start.split('T')[1].substring(0, 5) : ''} - ${s.end ? s.end.split('T')[1].substring(0, 5) : ''}<br>
        ${alertOrderMap[s.order_id] ? '<span style="color:#dc2626; font-weight:700;">Критический риск срыва слота!</span><br>' : ''}
        <a href="javascript:void(0)" onclick="openInspector('${s.order_id}')">Открыть карточку наряда</a>
      `);
      marker.on('click', () => openInspector(s.order_id));
      if (s.order_id === highlightOrderId) {
        targetMarkerToOpen = marker;
      }
    }
    splitMarkerLayers.push(marker);
  });

  let polyline = L.polyline(routePoints, {
    color: '#ff4f12',
    weight: 4,
    opacity: 0.85,
    dashArray: '6, 6'
  }).addTo(splitMap);
  splitRouteLayers.push(polyline);

  fetch(`/api/v1/routing/track/${selectedStaffId}`)
    .then(r => r.json())
    .then(track => {
      if (track && track.coordinates && track.coordinates.length > 1) {
        splitMap.removeLayer(polyline);
        const osrmPolyline = L.polyline(track.coordinates, {
          color: '#ff4f12',
          weight: 4.5,
          opacity: 0.95,
          lineCap: 'round',
          lineJoin: 'round'
        }).addTo(splitMap);
        splitRouteLayers.push(osrmPolyline);
        const distKm = (track.distance_m / 1000).toFixed(1);
        const durMin = Math.round(track.duration_sec / 60);
        osrmPolyline.bindPopup(`
          <b>Дорожный граф: ${track.provider}</b><br>
          Пробег по улицам: <b>${distKm} км</b> (${durMin} мин в пути)<br>
          <i>Построено по дорожной сети г. Екатеринбург</i>
        `);

        if (badge) {
          const sMeta = (contextData && contextData.staff) ? contextData.staff[selectedStaffId] : null;
          const sName = sMeta ? sMeta.display_name.split(' (')[0] : selectedStaffId;
          badge.innerText = `${sName} | OSRM: ${distKm} км (${durMin} мин)`;
        }
      }
    })
    .catch(err => console.warn('Split OSRM track error:', err));

  if (bounds.isValid()) {
    splitMap.fitBounds(bounds, { padding: [35, 35] });
  }

  if (targetMarkerToOpen) {
    setTimeout(() => targetMarkerToOpen.openPopup(), 200);
  }
}

function focusSplitMapOnAlert(staffId, orderId) {
  if (currentActiveView !== 'split') {
    switchView('split');
  }
  setTimeout(() => {
    renderRoutesOnSplitMap(currentPlanData, staffId, orderId);
  }, 120);
}

function runActiveDaySimulation() {
  if (!currentPlanData || !currentPlanData.routes.length) return;

  const activeRoutes = currentPlanData.routes.filter(r => r.status !== 'UNAVAILABLE');
  const targetRoute = activeRoutes.find(r => r.stops && r.stops.some(s => s.type === 'VISIT' && s.status !== 'DONE' && s.status !== 'COMPLETED' && s.status !== 'CANCELLED'))
    || activeRoutes.find(r => r.stops && r.stops.some(s => s.type === 'VISIT' && s.status !== 'CANCELLED'));
  const visitStop = targetRoute && targetRoute.stops.find(s => s.type === 'VISIT' && s.status !== 'DONE' && s.status !== 'COMPLETED' && s.status !== 'CANCELLED')
    || (targetRoute && targetRoute.stops.find(s => s.type === 'VISIT' && s.status !== 'CANCELLED'));
  if (!targetRoute || !visitStop) {
    showToast(RU_TEXT.T_0183, RU_TEXT.T_0151, 'warning');
    return;
  }

  const secondRoute = activeRoutes.find(r => r.staff_id !== targetRoute.staff_id) || targetRoute;
  const activeDate = getActivePlanDate();
  const tzSuffix = getTimezoneOffsetSuffix();
  const tsNow = getCurrentEventTimestamp();
  const asOfMin = parseTimeToMin(tsNow);
  const stopEndMin = parseTimeToMin(visitStop.end || visitStop.start);
  const endMin = Math.max(stopEndMin + 35, asOfMin + 35);
  const newEndHHMM = formatMinutesToHHMM(endMin);
  const stamp = Date.now();

  showToast(RU_TEXT.T_0183, `Задержка ${targetRoute.staff_id} (+35 мин), снятие ${secondRoute.staff_id} и оперативная переброска нарядов`, 'warning', 4500);

  const delayEvent = {
    event_id: `EV-SIM-01-${stamp}`,
    type: 'VISIT_DELAYED',
    timestamp: tsNow,
    staff_id: targetRoute.staff_id,
    order_id: visitStop.order_id,
    expected_end: `${activeDate}T${newEndHHMM}:00${tzSuffix}`
  };

  fetch('/api/v1/events', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(delayEvent)
  })
    .then(async res => {
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
      return body;
    })
    .then(() => {
      const breakdownEvent = {
        event_id: `EV-SIM-02-${stamp}`,
        type: 'STAFF_UNAVAILABLE',
        timestamp: tsNow,
        staff_id: secondRoute.staff_id,
        reason: RU_TEXT.T_0184,
        comment: RU_TEXT.T_0184
      };
      return fetch('/api/v1/events', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(breakdownEvent)
      });
    })
    .then(async res => {
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
      return body;
    })
    .then(() => {
      loadAllData();
      switchView('split');
    })
    .catch(err => {
      console.error(err);
      showToast(RU_TEXT.T_0183, err.message || String(err), 'error');
    });
}

let currentTrafficEnabled = false;
let currentDisabledHours = new Set();
let currentHourlyFactors = {
  8: 1.0, 9: 1.0, 10: 1.0, 11: 1.0, 12: 1.0, 13: 1.0,
  14: 1.0, 15: 1.0, 16: 1.0, 17: 1.0, 18: 1.0, 19: 1.0, 20: 1.0
};
let currentBridgeEnabled = false;

function loadTrafficProfile() {
  fetch('/api/v1/traffic/profile')
    .then(r => r.json())
    .then(data => {
      if (typeof data.traffic_enabled === 'boolean') {
        currentTrafficEnabled = data.traffic_enabled;
      }
      if (Array.isArray(data.disabled_hours)) {
        currentDisabledHours = new Set(data.disabled_hours.map(Number));
      }
      if (data.hourly_factors && typeof data.hourly_factors === 'object') {
        Object.keys(data.hourly_factors).forEach(h => {
          currentHourlyFactors[Number(h)] = parseFloat(data.hourly_factors[h]);
        });
      }
      if (typeof data.bridge_penalty_enabled === 'boolean') {
        currentBridgeEnabled = data.bridge_penalty_enabled;
      }

      const chkMaster = document.getElementById('chk-traffic-master');
      if (chkMaster) chkMaster.checked = currentTrafficEnabled;
      updateTrafficMasterStatusLabel();

      const chkBridge = document.getElementById('chk-bridge-penalty');
      if (chkBridge) chkBridge.checked = currentBridgeEnabled;

      renderTrafficHoursChips();
    })
    .catch(err => console.warn('Traffic profile load error:', err));
}

function updateTrafficMasterStatusLabel() {
  const lbl = document.getElementById('traffic-master-status');
  if (lbl) {
    lbl.innerText = currentTrafficEnabled ? RU_TEXT.T_0185 : RU_TEXT.T_0186;
    lbl.style.color = currentTrafficEnabled ? '#7700ff' : '#94a3b8';
  }
}

function renderTrafficHoursChips() {
  const container = document.getElementById('traffic-hours-chips');
  if (!container) return;
  container.innerHTML = '';

  const peakHours = [8, 9, 17, 18, 19];
  const sortedHours = Object.keys(currentHourlyFactors).map(Number).sort((a, b) => a - b);
  let activeCount = 0;

  sortedHours.forEach(h => {
    const factor = currentHourlyFactors[h] || 1.0;
    const isPeak = peakHours.includes(h);
    const isHourDisabled = !currentTrafficEnabled || currentDisabledHours.has(h);
    if (!isHourDisabled) activeCount++;

    const chip = document.createElement('div');
    chip.dataset.hour = String(h);

    if (!isHourDisabled) {
      chip.className = `traffic-hour-chip ${isPeak ? 'active-peak' : 'active-normal'}`;
      chip.innerHTML = `
        <span style="display:inline-flex; align-items:center; justify-content:center; width:13px; height:13px; border-radius:3px; background:${isPeak ? '#ef4444' : '#3b82f6'}; flex-shrink:0;">
          <svg width="9" height="9" viewBox="0 0 10 10" fill="none"><path d="M2 5L4 8L8 2" stroke="#ffffff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </span>
        <span>${h}:00 - <b>x${factor.toFixed(2)}</b>${isPeak ? ' (пик)' : ''}</span>
      `;
    } else {
      chip.className = 'traffic-hour-chip disabled-hour';
      chip.innerHTML = `
        <span style="display:inline-block; width:13px; height:13px; border-radius:3px; border:1px solid #cbd5e1; background:#ffffff; flex-shrink:0;"></span>
        <span>${h}:00 - <span style="text-decoration:line-through;">x${factor.toFixed(2)}</span> (снят)</span>
      `;
    }

    chip.title = isHourDisabled ? `Кликните, чтобы включить коэффициент для ${h}:00` : `Кликните, чтобы снять коэффициент для ${h}:00 (будет x1.0)`;

    chip.addEventListener('click', () => {
      toggleTrafficHour(h);
    });

    container.appendChild(chip);
  });

  const stat = document.getElementById('traffic-summary-stat');
  if (stat) {
    stat.innerText = `Активно коэффициентов: ${activeCount} из ${sortedHours.length} | Мосты: ${currentBridgeEnabled ? 'включены (+25%)' : 'отключены'}`;
  }
}

function toggleTrafficHour(h) {
  if (!currentTrafficEnabled) {
    currentTrafficEnabled = true;
    const chkMaster = document.getElementById('chk-traffic-master');
    if (chkMaster) chkMaster.checked = true;
    updateTrafficMasterStatusLabel();
  }

  if (currentDisabledHours.has(h)) {
    currentDisabledHours.delete(h);
  } else {
    currentDisabledHours.add(h);
  }
  renderTrafficHoursChips();
}

function applyTrafficProfileAndReplan() {
  const btnApply = document.getElementById('btn-apply-traffic-replan');
  if (btnApply) {
    btnApply.innerText = RU_TEXT.T_0187;
    btnApply.disabled = true;
  }

  const payload = {
    traffic_enabled: currentTrafficEnabled,
    disabled_hours: Array.from(currentDisabledHours),
    hourly_factors: currentHourlyFactors,
    bridge_penalty_enabled: currentBridgeEnabled
  };

  fetch('/api/v1/traffic/profile', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
    .then(r => r.json())
    .then(() => {
      return fetch('/api/v1/plan', { method: 'POST' });
    })
    .then(r => r.json())
    .then(plan => {
      renderAll(plan);
      const modal = document.getElementById('modal-traffic');
      if (modal) modal.classList.remove('active');
      if (btnApply) {
        btnApply.innerText = RU_TEXT.T_0188;
        btnApply.disabled = false;
      }
      showToast(RU_TEXT.T_0189, `План смены пересчитан: ${plan.routes.length} маршрутов, пробег ${Math.round(plan.summary.travel_sec_total / 60)} мин`, 'success', 4000);
    })
    .catch(err => {
      console.error(err);
      if (btnApply) {
        btnApply.innerText = RU_TEXT.T_0188;
        btnApply.disabled = false;
      }
      showToast(RU_TEXT.T_0059, RU_TEXT.T_0190, 'error');
    });
}

function simulateEngineerIncident() {
  const staffSelect = document.getElementById('filter-crew');
  let staffId = staffSelect ? staffSelect.value : 'all';
  if (!staffId || staffId === 'all') staffId = 'ST-001';

  if (!currentPlanData || !currentPlanData.routes) return;
  const route = currentPlanData.routes.find(r => r.staff_id === staffId);
  if (!route) return;

  const firstVisit = route.stops.find(s => s.type === 'VISIT');
  if (!firstVisit) {
    showToast(RU_TEXT.T_0191, RU_TEXT.T_0192, 'info');
    return;
  }

  const oldEnd = firstVisit.end;
  const [dPart, tPart] = oldEnd.split('T');
  const [hStr, mStr] = tPart.substring(0, 5).split(':');
  let totalMin = Number(hStr) * 60 + Number(mStr) + 25;
  const newH = Math.floor(totalMin / 60) % 24;
  const newM = totalMin % 60;
  const newEndTime = `${dPart}T${String(newH).padStart(2, '0')}:${String(newM).padStart(2, '0')}:00+05:00`;

  showToast(RU_TEXT.T_0193, `Затор / ДТП на мосту через Исеть (+25 мин для ${staffId}). Запуск Warm Start...`, 'warning', 4000);

  const eventPayload = {
    event_id: `EV-TRAFFIC-${Date.now().toString().slice(-4)}`,
    type: 'VISIT_DELAYED',
    timestamp: `${dPart}T${tPart}`,
    staff_id: staffId,
    order_id: firstVisit.order_id,
    expected_end: newEndTime
  };

  fetch('/api/v1/events', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(eventPayload)
  })
    .then(r => r.json())
    .then(res => {
      loadAllData();
      switchView('split');
      setTimeout(() => {
        showToast(RU_TEXT.T_0194, `Версия плана: ${res.plan_version}. Сдвиг затронул инженера ${staffId}`, 'success', 4000);
      }, 500);
    })
    .catch(err => {
      console.error(err);
      showToast(RU_TEXT.T_0059, RU_TEXT.T_0195, 'error');
    });
}

let currentSelectedScenario = 'staff_minus_3';
let lastScenarioSimResult = null;
let selectedScenarioStaffIds = new Set();

function renderScenarioCustomStaffPicker() {
  const picker = document.getElementById('scenario-custom-staff-picker');
  if (!picker) return;
  const routes = (currentPlanData && currentPlanData.routes)
    ? currentPlanData.routes.filter(r => r.status !== 'UNAVAILABLE')
    : [];
  const validIds = new Set(routes.map(r => r.staff_id));
  selectedScenarioStaffIds.forEach(id => {
    if (!validIds.has(id)) selectedScenarioStaffIds.delete(id);
  });
  picker.innerHTML = '';
  routes.forEach(r => {
    const sMeta = (contextData && contextData.staff) ? contextData.staff[r.staff_id] : null;
    const shortName = sMeta ? sMeta.display_name.split(' (')[0] : r.staff_id;
    const chip = document.createElement('label');
    chip.className = `scenario-staff-chip${selectedScenarioStaffIds.has(r.staff_id) ? ' selected' : ''}`;
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = r.staff_id;
    cb.checked = selectedScenarioStaffIds.has(r.staff_id);
    cb.addEventListener('change', () => {
      if (cb.checked) {
        while (selectedScenarioStaffIds.size >= 3) {
          const oldestId = selectedScenarioStaffIds.values().next().value;
          selectedScenarioStaffIds.delete(oldestId);
          picker.querySelectorAll('input[type="checkbox"]').forEach(otherCb => {
            if (otherCb.value === oldestId) {
              otherCb.checked = false;
              const otherChip = otherCb.closest('.scenario-staff-chip');
              if (otherChip) otherChip.classList.remove('selected');
            }
          });
        }
        selectedScenarioStaffIds.add(r.staff_id);
        chip.classList.add('selected');
      } else {
        selectedScenarioStaffIds.delete(r.staff_id);
        chip.classList.remove('selected');
      }
      selectScenarioCard('staff_minus_3');
      runScenarioSimulation('staff_minus_3', false);
    });
    const txt = document.createElement('span');
    txt.innerText = `${r.staff_id} (${shortName})`;
    chip.appendChild(cb);
    chip.appendChild(txt);
    picker.appendChild(chip);
  });
}

function initScenarioModal() {
  const btnOpen = document.getElementById('btn-open-scenario');
  const modal = document.getElementById('modal-scenario');
  const btnClose = document.getElementById('btn-close-scenario');
  const btnCloseFooter = document.getElementById('btn-close-scenario-footer');
  const btnApply = document.getElementById('btn-apply-scenario-shift');
  const btnRunCustom = document.getElementById('btn-run-custom-minus3');

  if (btnOpen && modal) {
    btnOpen.addEventListener('click', () => {
      renderScenarioCustomStaffPicker();
      modal.classList.add('active');
      selectScenarioCard(currentSelectedScenario);
      runScenarioSimulation(currentSelectedScenario, false);
    });
  }

  if (btnClose && modal) {
    btnClose.addEventListener('click', () => modal.classList.remove('active'));
  }

  if (btnCloseFooter && modal) {
    btnCloseFooter.addEventListener('click', () => modal.classList.remove('active'));
  }

  if (modal) {
    modal.addEventListener('click', (e) => {
      if (e.target === modal) modal.classList.remove('active');
    });
  }

  if (btnRunCustom) {
    btnRunCustom.addEventListener('click', () => {
      if (selectedScenarioStaffIds.size === 0) {
        showToast(RU_TEXT.T_0198, RU_TEXT.T_0199, 'warning');
        return;
      }
      selectScenarioCard('staff_minus_3');
      runScenarioSimulation('staff_minus_3', false);
    });
  }

  const cards = document.querySelectorAll('.scenario-card');
  cards.forEach(card => {
    card.addEventListener('click', () => {
      const scen = card.getAttribute('data-scenario');
      if (scen) {
        selectScenarioCard(scen);
        runScenarioSimulation(scen, false);
      }
    });
  });

  if (btnApply) {
    btnApply.addEventListener('click', () => {
      btnApply.disabled = true;
      btnApply.innerText = RU_TEXT.T_0200;
      runScenarioSimulation(currentSelectedScenario, true)
        .then(data => {
          btnApply.disabled = false;
          btnApply.innerText = RU_TEXT.T_0201;
          if (modal) modal.classList.remove('active');
          showToast(RU_TEXT.T_0202, RU_TEXT.T_0203, 'success', 10000, (data && data.undo_token) || null);
          if (currentSelectedScenario !== 'duration_history') {
            loadAllData();
          }
        })
        .catch(err => {
          btnApply.disabled = false;
          btnApply.innerText = RU_TEXT.T_0201;
          showToast(RU_TEXT.T_0204, String(err), 'error');
        });
    });
  }
}

function selectScenarioCard(scenType) {
  currentSelectedScenario = scenType;
  document.querySelectorAll('.scenario-card').forEach(c => {
    if (c.getAttribute('data-scenario') === scenType) {
      c.classList.add('active');
    } else {
      c.classList.remove('active');
    }
  });

  const customBox = document.getElementById('scenario-custom-staff-box');
  if (customBox) {
    customBox.style.display = (scenType === 'staff_minus_3') ? 'block' : 'none';
  }
  const durStatsEl = document.getElementById('scenario-duration-stats');
  if (durStatsEl && scenType !== 'duration_history') {
    durStatsEl.style.display = 'none';
    durStatsEl.innerHTML = '';
  }

  const statLabel = document.getElementById('scenario-summary-stat');
  if (statLabel) {
    if (scenType === 'staff_minus_3') {
      statLabel.innerText = RU_TEXT.T_0205;
    } else if (scenType === 'backpack_opt') {
      statLabel.innerText = RU_TEXT.T_0206;
    } else if (scenType === 'duty_plus_2') {
      statLabel.innerText = RU_TEXT.T_0207;
    } else if (scenType === 'duration_history') {
      statLabel.innerText = RU_TEXT.T_0208;
    }
  }
}

function runScenarioSimulation(scenType, applyToShift = false) {
  const tbody = document.getElementById('scenario-compare-tbody');
  const insightText = document.getElementById('scenario-insight-text');
  const durStatsEl = document.getElementById('scenario-duration-stats');
  if (tbody && !applyToShift) {
    tbody.innerHTML = RU_TEXT.T_0209;
  }
  if (insightText && !applyToShift) {
    insightText.innerText = RU_TEXT.T_0210;
  }

  if (scenType === 'duration_history') {
    return fetch('/api/v1/ui/whatif/duration', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    })
      .then(r => {
        if (!r.ok) throw new Error(`HTTP error ${r.status}`);
        return r.json();
      })
      .then(data => {
        const baseKpi = computePlanKpiStats(currentPlanData);
        const scenKpi = computePlanKpiStats(data.plan);
        const countRestocks = (p) => (p && p.routes ? p.routes : []).reduce((acc, r) => acc + (r.stops || []).filter(s => s.type === 'RESTOCK').length, 0);
        const baseRestocks = countRestocks(currentPlanData);
        const scenRestocks = countRestocks(data.plan);
        const baseCrews = (currentPlanData && currentPlanData.routes) ? currentPlanData.routes.length : 0;
        const scenCrews = (data.plan && data.plan.routes) ? data.plan.routes.length : 0;
        const baseTMin = Math.round(baseKpi.travelSec / 60);
        const scenTMin = Math.round(scenKpi.travelSec / 60);

        const forecasts = Object.values(data.forecasts || {});
        const histCount = forecasts.filter(f => f.source === 'history').length;
        if (durStatsEl) {
          durStatsEl.style.display = 'block';
          durStatsEl.innerHTML = `Учтена история выполнения для <b>${histCount}</b> из <b>${forecasts.length}</b> нарядов (остальные по нормативу).`;
        }

        const formatted = {
          comparison: {
            staff_count: { base: baseCrews, scenario: scenCrews, delta: scenCrews - baseCrews },
            planned_orders: { base: baseKpi.planned, scenario: scenKpi.planned, delta: scenKpi.planned - baseKpi.planned },
            unassigned_orders: { base: baseKpi.unassigned, scenario: scenKpi.unassigned, delta: scenKpi.unassigned - baseKpi.unassigned },
            sla_percent: { base: Number(baseKpi.slaPct), scenario: Number(scenKpi.slaPct), delta: Number((Number(scenKpi.slaPct) - Number(baseKpi.slaPct)).toFixed(1)) },
            restocks_count: { base: baseRestocks, scenario: scenRestocks, delta: scenRestocks - baseRestocks },
            travel_min_total: { base: baseTMin, scenario: scenTMin, delta: scenTMin - baseTMin }
          },
          insight: `What-If «История длительностей»: ${histCount} заявок откалиброваны по фактической статистике бригад, SLA ${scenKpi.slaPct}%.`
        };
        lastScenarioSimResult = data;
        renderScenarioResults(formatted);
        if (applyToShift && data.plan) {
          renderAll(data.plan);
        }
        return data;
      });
  }

  const reqBody = {
    scenario_type: scenType,
    apply_to_shift: applyToShift
  };
  if (scenType === 'staff_minus_3' && selectedScenarioStaffIds.size > 0) {
    reqBody.staff_ids = Array.from(selectedScenarioStaffIds);
  }

  return fetch('/api/v1/scenario/simulate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(reqBody)
  })
    .then(r => {
      if (!r.ok) throw new Error(`HTTP error ${r.status}`);
      return r.json();
    })
    .then(data => {
      lastScenarioSimResult = data;
      renderScenarioResults(data);
      return data;
    });
}

function renderScenarioResults(data) {
  if (!data || !data.comparison) return;
  const c = data.comparison;
  const tbody = document.getElementById('scenario-compare-tbody');
  const insightText = document.getElementById('scenario-insight-text');

  if (insightText) {
    insightText.innerText = data.insight || RU_TEXT.T_0211;
  }

  if (!tbody) return;

  function fmtDelta(val, isBetterPositive = true, unit = '') {
    if (val === 0) return `<span style="color:#64748b; font-weight:600;">0${unit}</span>`;
    const sign = val > 0 ? `+${val}` : `${val}`;
    const isGood = isBetterPositive ? (val > 0) : (val < 0);
    const color = isGood ? '#16a34a' : '#dc2626';
    return `<span style="color:${color}; font-weight:700;">${sign}${unit}</span>`;
  }

  const rows = [
    {
      label: RU_TEXT.T_0212,
      base: c.staff_count.base,
      scen: c.staff_count.scenario,
      delta: fmtDelta(c.staff_count.delta, true, RU_TEXT.T_0213)
    },
    {
      label: RU_TEXT.T_0214,
      base: c.planned_orders.base,
      scen: c.planned_orders.scenario,
      delta: fmtDelta(c.planned_orders.delta, true, RU_TEXT.T_0215)
    },
    {
      label: RU_TEXT.T_0216,
      base: c.unassigned_orders.base,
      scen: c.unassigned_orders.scenario,
      delta: fmtDelta(c.unassigned_orders.delta, false, RU_TEXT.T_0215)
    },
    {
      label: RU_TEXT.T_0217,
      base: `${c.sla_percent.base}%`,
      scen: `${c.sla_percent.scenario}%`,
      delta: fmtDelta(c.sla_percent.delta, true, '%')
    },
    {
      label: RU_TEXT.T_0218,
      base: c.restocks_count.base,
      scen: c.restocks_count.scenario,
      delta: fmtDelta(c.restocks_count.delta, false, RU_TEXT.T_0219)
    },
    {
      label: RU_TEXT.T_0220,
      base: `${c.travel_min_total.base} мин`,
      scen: `${c.travel_min_total.scenario} мин`,
      delta: fmtDelta(c.travel_min_total.delta, false, RU_TEXT.T_0221)
    }
  ];

  tbody.innerHTML = rows.map(r => `
    <tr>
      <td style="font-weight:600; color:#334155;">${r.label}</td>
      <td style="color:#64748b;">${r.base}</td>
      <td style="font-weight:700; color:#0f172a;">${r.scen}</td>
      <td>${r.delta}</td>
    </tr>
  `).join('');
}

function openForceMajeureModal(preselectedStaffId = null) {
  const modal = document.getElementById('modal-force-majeure');
  const staffSelect = document.getElementById('fm-staff-select');
  const reasonSelect = document.getElementById('fm-reason-select');
  const commentInput = document.getElementById('fm-comment');

  if (!modal || !staffSelect) return;

  staffSelect.innerHTML = '';
  const staffList = (contextData && contextData.staff) ? Object.values(contextData.staff) : [];

  if (currentPlanData && currentPlanData.routes && currentPlanData.routes.length > 0) {
    currentPlanData.routes.forEach(r => {
      const opt = document.createElement('option');
      opt.value = r.staff_id;
      const sMeta = (contextData && contextData.staff) ? contextData.staff[r.staff_id] : null;
      const name = sMeta ? sMeta.display_name.split(' (')[0] : r.staff_id;
      const scShort = r.service_center_id ? r.service_center_id.replace('SC-0', RU_TEXT.T_0084) : '';
      const scName = DISTRICT_NAMES[r.service_center_id] || scShort;
      const isUnav = r.status === 'UNAVAILABLE';
      opt.textContent = `${r.staff_id}: ${name} (${scName})${isUnav ? ' [СНЯТ]' : ''}`;
      if (isUnav) opt.disabled = true;
      staffSelect.appendChild(opt);
    });
  } else if (staffList.length > 0) {
    staffList.forEach(s => {
      const opt = document.createElement('option');
      opt.value = s.staff_id;
      opt.textContent = `${s.staff_id}: ${s.display_name}`;
      staffSelect.appendChild(opt);
    });
  }

  if (preselectedStaffId) {
    const targetOpt = staffSelect.querySelector(`option[value="${preselectedStaffId}"]`);
    if (targetOpt) staffSelect.value = preselectedStaffId;
  }

  if (reasonSelect) reasonSelect.selectedIndex = 0;
  if (commentInput) commentInput.value = '';

  const defaultRadio = document.querySelector('input[name="fm-reassign-strategy"][value="auto"]');
  if (defaultRadio) defaultRadio.checked = true;

  modal.classList.add('active');
}

function initForceMajeureModal() {
  const modal = document.getElementById('modal-force-majeure');
  const btnClose = document.getElementById('btn-close-force-majeure');
  const btnCancel = document.getElementById('btn-cancel-force-majeure');
  const form = document.getElementById('form-force-majeure');
  const btnGanttOpen = document.getElementById('btn-gantt-force-majeure');

  if (btnGanttOpen) {
    btnGanttOpen.addEventListener('click', () => openForceMajeureModal());
  }

  const closeModal = () => {
    if (modal) modal.classList.remove('active');
  };

  if (btnClose) btnClose.addEventListener('click', closeModal);
  if (btnCancel) btnCancel.addEventListener('click', closeModal);

  if (modal) {
    modal.addEventListener('click', (e) => {
      if (e.target === modal) closeModal();
    });
  }

  if (form) {
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const staffSelect = document.getElementById('fm-staff-select');
      const reasonSelect = document.getElementById('fm-reason-select');
      const commentInput = document.getElementById('fm-comment');
      const strategyRadio = document.querySelector('input[name="fm-reassign-strategy"]:checked');

      const staffId = staffSelect ? staffSelect.value : null;
      if (!staffId) {
        showToast(RU_TEXT.T_0059, RU_TEXT.T_0222, 'warning');
        return;
      }

      const reason = reasonSelect ? reasonSelect.value : RU_TEXT.T_0223;
      const comment = commentInput ? commentInput.value.trim() : '';
      const strategy = strategyRadio ? strategyRadio.value : 'auto';

      const submitBtn = document.getElementById('btn-submit-force-majeure');
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.innerText = RU_TEXT.T_0224;
      }

      const eventPayload = {
        event_id: `EV-FM-${Date.now()}`,
        type: 'STAFF_UNAVAILABLE',
        staff_id: staffId,
        reason: reason,
        comment: comment || '-',
        reassign_mode: strategy,
        timestamp: (currentPlanData && currentPlanData.as_of) ? currentPlanData.as_of : getCurrentEventTimestamp()
      };

      fetch('/api/v1/events', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(eventPayload)
      })
        .then(res => {
          if (!res.ok) throw new Error(`HTTP error ${res.status}`);
          return res.json();
        })
        .then(result => {
          closeModal();
          showToast(RU_TEXT.T_0225, result.message || `Инженер ${staffId} снят со смены`, 'warning', 10000, result.undo_token || null);
          loadAllData();
        })
        .catch(err => {
          showToast(RU_TEXT.T_0226, String(err), 'error');
        })
        .finally(() => {
          if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.innerText = RU_TEXT.T_0227;
          }
        });
    });
  }
}
