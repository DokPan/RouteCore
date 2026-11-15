import { confirmAction, showNotice } from './notices.js';

const RU_TEXT = {
  T_0001: 'Запланировать заезд в СЦ',
  T_0002: 'Добавить заезд за оборудованием и пересчитать оставшиеся визиты?',
  T_0003: 'Я на месте',
  T_0004: 'Завершить',
  T_0005: 'Завершён',
  T_0006: 'Звонок отмечен',
  T_0007: 'Позвонил абоненту',
  T_0008: 'Настройки',
  T_0009: 'Демо звонка',
  T_0010: 'Отметить демо-звонок',
  T_0011: 'Сохраняем отметку…',
  T_0012: 'Демо-звонок отмечен. Абоненту звонок не отправлялся.',
  T_0013: 'Маршрут',
  T_0014: 'Визитов нет.',
  T_0015: 'Мой маршрутный лист',
  T_0016: 'Маршрут на день',
  T_0017: 'Не удалось загрузить или обновить визиты.',
  T_0018: 'Передать в профильное подразделение',
  T_0019: 'Неисправность вне нашей зоны',
  T_0020: 'Наряд передан в профильное подразделение.',
  T_0021: 'Окно звонка закрыто',
  T_0022: 'Допуск к высотным работам есть',
  T_0023: 'Допуска к высотным работам нет',
  T_0024: 'Остаток оборудования',
  T_0025: 'Клиент не отвечает',
  T_0026: 'Снять неподтверждённый визит с текущего маршрута?',
  T_0027: 'Визит передан диспетчеру для повторного назначения. Остаток смены пересчитан.',
  T_0028: 'Оптические терминалы',
  T_0029: 'Роутеры',
  T_0030: 'ТВ-приставки',
  T_0031: 'Клиент',
  T_0032: 'Подъезд',
  T_0033: 'Этаж',
  T_0034: 'Не указано',
  T_0035: 'Прибытие',
  T_0036: 'Завершение',
  T_0037: 'Текущий визит',
  T_0038: 'Далее по маршруту',
  T_0039: 'Завершённые визиты',
};


const TEXT = Object.freeze({
  restock: RU_TEXT.T_0001,
  restockConfirm: RU_TEXT.T_0002,
  restockSaved: (start, end) => `Заезд в СЦ запланирован на ${start}–${end}.`,
  arrive: RU_TEXT.T_0003,
  finish: RU_TEXT.T_0004,
  done: RU_TEXT.T_0005,
  called: RU_TEXT.T_0006,
  call: RU_TEXT.T_0007,
  settings: RU_TEXT.T_0008,
  demoSetting: RU_TEXT.T_0009,
  demoCall: RU_TEXT.T_0010,
  callPending: RU_TEXT.T_0011,
  demoCalled: RU_TEXT.T_0012,
  navigate: RU_TEXT.T_0013,
  empty: RU_TEXT.T_0014,
  routeSheet: RU_TEXT.T_0015,
  dayRoute: RU_TEXT.T_0016,
  failed: RU_TEXT.T_0017,
  transfer: RU_TEXT.T_0018,
  transferReason: RU_TEXT.T_0019,
  transferred: RU_TEXT.T_0020,
  eventTime: milliseconds => `Пересчёт маршрута: ${(milliseconds / 1000).toLocaleString('ru-RU', { maximumFractionDigits: 2 })} с.`,
  callClosed: RU_TEXT.T_0021,
  callOpens: time => `Доступно с ${time}`,
  slot: (start, end) => `Окно визита ${start}–${end}`,
  staffSummary: profile => `${profile.name} · ${profile.center} · ${profile.shift.start}–${profile.shift.end}`,
  heightAllowed: RU_TEXT.T_0022,
  heightDenied: RU_TEXT.T_0023,
  equipment: RU_TEXT.T_0024,
  noAnswer: RU_TEXT.T_0025,
  noAnswerConfirm: RU_TEXT.T_0026,
  noAnswerSaved: RU_TEXT.T_0027,
  equipmentNames: { ONT: RU_TEXT.T_0028, ROUTER: RU_TEXT.T_0029, STB: RU_TEXT.T_0030 },
  client: RU_TEXT.T_0031,
  entrance: RU_TEXT.T_0032,
  floor: RU_TEXT.T_0033,
  unspecified: RU_TEXT.T_0034,
  actualStart: RU_TEXT.T_0035,
  actualEnd: RU_TEXT.T_0036,
  currentVisit: RU_TEXT.T_0037,
  nextVisits: RU_TEXT.T_0038,
  completedVisits: RU_TEXT.T_0039,
  why: detail => `Почему вам: ${detail}`,
});

const staffSelect = document.getElementById('mobile-staff');
const status = document.getElementById('mobile-status');
const visitList = document.getElementById('mobile-list');
const demoMode = document.getElementById('mobile-demo-mode');
const loginForm = document.getElementById('mobile-login');
const loginStatus = document.getElementById('mobile-login-status');
const profilePanel = document.getElementById('mobile-profile');
const logoutButton = document.getElementById('mobile-logout');
const demoSettings = document.getElementById('mobile-demo-option');
document.getElementById('mobile-settings-title').textContent = TEXT.settings;
document.getElementById('mobile-demo-label').textContent = TEXT.demoSetting;
let signedInStaffId;
let restockPin;
const restockButton = document.getElementById('mobile-restock');
restockButton.textContent = TEXT.restock;
restockButton.addEventListener('click', async () => {
  if (!await confirmAction(TEXT.restockConfirm) || !signedInStaffId) return;
  restockButton.disabled = true;
  try {
    const response = await fetch('/api/v1/ui/staff/' + encodeURIComponent(signedInStaffId) + '/restock', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ staff_id: signedInStaffId, pin: restockPin }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : TEXT.failed);
    await loadVisits();
    showNotice(TEXT.restockSaved(result.start.slice(11, 16), result.end.slice(11, 16)));
  } catch (error) {
    showNotice(error.message);
  } finally {
    restockButton.disabled = false;
  }
});

function renderBackpack(equipment) {
  const inventory = document.createElement('dl');
  for (const [code, quantity] of Object.entries(equipment)) {
    const name = document.createElement('dt');
    name.textContent = TEXT.equipmentNames[code] || code;
    const count = document.createElement('dd');
    count.textContent = quantity;
    inventory.append(name, count);
  }
  return inventory;
}

async function loadVisits() {
  const staffId = signedInStaffId;
  if (!staffId) return;
  try {
    const response = await fetch('/api/v1/ui/visits?staff_id=' + encodeURIComponent(staffId));
    if (!response.ok) throw new Error(TEXT.failed);
    const visits = await response.json();
    if (signedInStaffId !== staffId) return;
    const profileResponse = await fetch('/api/v1/ui/staff-login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ staff_id: staffId, pin: restockPin }),
    });
    if (profileResponse.ok) {
      const profile = await profileResponse.json();
      if (signedInStaffId !== staffId) return;
      profilePanel.querySelector('dl')?.replaceWith(renderBackpack(profile.equipment));
    }
    status.textContent = visits.length ? '' : TEXT.empty;
    const routeLink = document.getElementById('mobile-route-link');
    routeLink.textContent = TEXT.dayRoute;
    routeLink.href = '/?staff=' + encodeURIComponent(staffId) + '#map';
    routeLink.hidden = !visits.length;
    const sheetLink = document.getElementById('mobile-sheet-link');
    sheetLink.textContent = TEXT.routeSheet;
    sheetLink.href = '/route-sheet.html?from=master&staff=' + encodeURIComponent(staffId);
    sheetLink.hidden = !visits.length;
    const currentVisit = visits.find(visit => visit.status === 'IN_PROGRESS')
      || visits.find(visit => visit.status === 'PLANNED');
    const remainingVisits = visits.filter(visit => visit !== currentVisit && visit.status !== 'DONE');
    const completedVisits = visits.filter(visit => visit.status === 'DONE');
    const renderVisit = visit => {
      const card = document.createElement('article');
      const heading = document.createElement('strong');
      heading.textContent = visit.address || visit.id;
      const time = document.createElement('p');
      time.textContent = TEXT.slot(visit.slot.start.slice(11, 16), visit.slot.end.slice(11, 16));
      const formatActualTime = value => new Date(value).toLocaleString('ru-RU', {
        timeZone: 'Asia/Yekaterinburg', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
      });
      for (const [label, value] of [[TEXT.actualStart, visit.actual_start], [TEXT.actualEnd, visit.actual_end]]) {
        if (!value) continue;
        const fact = document.createElement('span');
        fact.textContent = [label, formatActualTime(value)].join(': ');
        time.append(document.createElement('br'), fact);
      }
      const button = document.createElement('button');
      button.className = 'btn btn-primary';
      if (visit === currentVisit) {
        button.classList.add('mobile-current-action');
      }
      button.disabled = visit.status === 'DONE';
      const action = visit.status === 'IN_PROGRESS' ? 'finish' : 'arrive';
      button.textContent = button.disabled ? TEXT.done : TEXT[action];
      button.onclick = async () => {
        button.disabled = true;
        try {
          const response = await fetch('/api/v1/ui/visits/' + encodeURIComponent(visit.id) + '/' + action, { method: 'POST' });
          if (!response.ok) throw new Error(TEXT.failed);
          await loadVisits();
        } catch {
          status.textContent = TEXT.failed;
          button.disabled = false;
        }
      };
      card.append(heading, time);
      if (visit === currentVisit) card.append(button);
      for (const [label, value] of [[TEXT.client, visit.client_name], [TEXT.entrance, visit.entrance], [TEXT.floor, visit.floor]]) {
        const detail = document.createElement('p');
        detail.textContent = `${label}: ${value ?? TEXT.unspecified}`;
        card.append(detail);
      }
      if (visit.why) {
        const explanation = document.createElement('p');
        explanation.textContent = TEXT.why(visit.why);
        card.append(explanation);
      }
      const navigation = document.createElement('a');
      navigation.textContent = TEXT.navigate;
      navigation.href = '/?' + new URLSearchParams({ staff: staffId, order: visit.id }) + '#map';
      const actions = document.createElement('div');
      actions.className = 'mobile-visit-actions';
      navigation.className = 'btn btn-outline';
      actions.append(navigation);
      card.append(actions);
      if (visit.status === 'IN_PROGRESS') {
        const menu = document.createElement('details');
        const menuLabel = document.createElement('summary');
        menuLabel.textContent = TEXT.transferReason;
        const transfer = document.createElement('button');
        transfer.type = 'button';
        transfer.className = 'btn btn-outline';
        transfer.textContent = TEXT.transfer;
        transfer.onclick = async () => {
          transfer.disabled = true;
          try {
            const response = await fetch('/api/v1/ui/visits/' + encodeURIComponent(visit.id) + '/transfer', { method: 'POST' });
            const result = await response.json();
            if (!response.ok) throw new Error(result.detail || TEXT.failed);
            await loadVisits();
            showNotice([TEXT.transferred, result.processing_ms == null ? '' : TEXT.eventTime(result.processing_ms)].filter(Boolean).join(' '));
          } catch (error) {
            status.textContent = error.message;
            transfer.disabled = false;
          }
        };
        menu.append(menuLabel, transfer);
        card.append(menu);
      }
      if (visit.status === 'PLANNED') {
        const call = document.createElement('button');
        call.className = 'btn btn-outline';
        const canCall = demoMode.checked || visit.call_window.available;
        const availableLabel = demoMode.checked ? TEXT.demoCall : TEXT.call;
        call.textContent = visit.called ? TEXT.called : canCall ? availableLabel
          : visit.call_window.closed ? TEXT.callClosed : TEXT.callOpens(visit.call_window.opens_at.slice(11, 16));
        call.disabled = visit.called || !canCall;
        call.onclick = async () => {
          call.disabled = true;
          call.textContent = TEXT.callPending;
          const isDemo = demoMode.checked;
          try {
            const path = '/api/v1/ui/visits/' + encodeURIComponent(visit.id) + '/call?demo=' + isDemo;
            const response = await fetch(path, { method: 'POST', signal: AbortSignal.timeout(10000) });
            const result = await response.json();
            if (!response.ok) throw new Error(result.detail || TEXT.failed);
            call.textContent = TEXT.called;
            showNotice(isDemo ? TEXT.demoCalled : TEXT.called);
          } catch (error) {
            status.textContent = error.name === 'TimeoutError' ? TEXT.failed : error.message;
            call.textContent = availableLabel;
            call.disabled = false;
          }
        };
        actions.append(call);
        if (visit === currentVisit && canCall) {
          const noAnswer = document.createElement('button');
          noAnswer.type = 'button';
          noAnswer.className = 'btn btn-outline';
          noAnswer.textContent = TEXT.noAnswer;
          noAnswer.onclick = async () => {
            if (!await confirmAction(TEXT.noAnswerConfirm)) return;
            noAnswer.disabled = true;
            try {
              const response = await fetch('/api/v1/ui/visits/' + encodeURIComponent(visit.id) + '/no-answer?demo=' + demoMode.checked,
                { method: 'POST', signal: AbortSignal.timeout(15000) });
              const result = await response.json();
              if (!response.ok) throw new Error(result.detail || TEXT.failed);
              await loadVisits();
              showNotice(TEXT.noAnswerSaved);
            } catch (error) {
              status.textContent = error.message;
              noAnswer.disabled = false;
            }
          };
          actions.append(noAnswer);
        }
      }
      return card;
    };
    visitList.replaceChildren();
    for (const [title, group] of [
      [TEXT.currentVisit, currentVisit ? [currentVisit] : []],
      [TEXT.nextVisits, remainingVisits],
      [TEXT.completedVisits, completedVisits],
    ]) {
      if (!group.length) continue;
      const section = document.createElement('section');
      const heading = document.createElement('h2');
      heading.textContent = title;
      section.append(heading, ...group.map(renderVisit));
      visitList.append(section);
    }
  } catch {
    status.textContent = TEXT.failed;
  }
}

demoMode.addEventListener('change', loadVisits);
loginForm.addEventListener('submit', async event => {
  event.preventDefault();
  const submit = loginForm.querySelector('button:not([type])');
  submit.disabled = true;
  loginStatus.textContent = '';
  try {
    const pin = document.getElementById('mobile-pin');
    const response = await fetch('/api/v1/ui/staff-login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ staff_id: staffSelect.value.trim().toUpperCase(), pin: pin.value.trim() }),
      signal: AbortSignal.timeout(10000),
    });
    const profile = await response.json();
    if (!response.ok) throw new Error(profile.detail || TEXT.failed);
    signedInStaffId = profile.staff_id;
    restockPin = pin.value.trim();
    restockButton.hidden = false;
    pin.value = '';
    profilePanel.replaceChildren(...[
      TEXT.staffSummary(profile), profile.profiles.join(' · '), profile.technologies.join(' · '),
      profile.height_permit ? TEXT.heightAllowed : TEXT.heightDenied, TEXT.equipment,
    ].map(value => {
      const line = document.createElement('p');
      line.textContent = value;
      return line;
    }));
    profilePanel.append(renderBackpack(profile.equipment));
    profilePanel.hidden = false;
    loginForm.hidden = true;
    demoSettings.hidden = false;
    logoutButton.hidden = false;
    await loadVisits();
  } catch (error) {
    loginStatus.textContent = error.name === 'TimeoutError' ? TEXT.failed : error.message;
  } finally {
    submit.disabled = false;
  }
});

logoutButton.addEventListener('click', () => {
  signedInStaffId = undefined;
  restockPin = undefined;
  restockButton.hidden = true;
  profilePanel.hidden = true;
  profilePanel.replaceChildren();
  visitList.replaceChildren();
  document.getElementById('mobile-route-link').hidden = true;
  document.getElementById('mobile-sheet-link').hidden = true;
  status.textContent = '';
  document.getElementById("app-notice").hidden = true;
  demoMode.checked = false;
  logoutButton.hidden = true;
  loginForm.hidden = false;
  demoSettings.open = false;
  demoSettings.hidden = true;
  staffSelect.focus();
});
async function fillDemoLogin(overwrite = false) {
  try {
    const response = await fetch('/api/v1/ui/staff-login/defaults');
    if (!response.ok) throw new Error(TEXT.failed);
    const defaults = await response.json();
    const pin = document.getElementById('mobile-pin');
    if (overwrite || !staffSelect.value) staffSelect.value = defaults.staff_id;
    if (overwrite || !pin.value) pin.value = defaults.pin;
  } catch {
    loginStatus.textContent = TEXT.failed;
  }
}

document.getElementById('fill-demo-login').onclick = () => fillDemoLogin(true);
await fillDemoLogin();
