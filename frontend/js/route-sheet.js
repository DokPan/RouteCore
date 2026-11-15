const RU_TEXT = {
  T_0001: 'Не удалось загрузить маршрутный лист.',
  T_0002: 'Оборудование',
  T_0003: 'На начало',
  T_0004: 'Вместимость',
  T_0005: 'Расход',
  T_0006: 'На конец',
  T_0007: 'Время',
  T_0008: 'Наряд / работа',
  T_0009: 'Адрес',
  T_0010: 'Выдача',
  T_0011: 'Факт / км',
  T_0012: 'Подпись',
  T_0013: 'Мастер',
  T_0014: 'Кладовщик',
  T_0015: 'База',
  T_0016: 'Исполнитель',
  T_0017: 'Сервисный центр',
  T_0018: 'Дата и время смены',
  T_0019: 'Екатеринбург',
  T_0020: 'Доподключение услуги',
  T_0021: 'Доподкл.',
  T_0022: 'частный сектор',
  T_0023: 'ЧС',
  T_0024: '^(?:г\\.\\s*)?',
};

const TEXT = Object.freeze({
  failure: RU_TEXT.T_0001,
  equipment: [RU_TEXT.T_0002, RU_TEXT.T_0003, RU_TEXT.T_0004, RU_TEXT.T_0005, RU_TEXT.T_0006],
  stops: [RU_TEXT.T_0007, RU_TEXT.T_0008, RU_TEXT.T_0009, RU_TEXT.T_0010, RU_TEXT.T_0011, RU_TEXT.T_0012],
  signatures: [RU_TEXT.T_0013, RU_TEXT.T_0014],
  base: RU_TEXT.T_0015,
  staff: RU_TEXT.T_0016,
  center: RU_TEXT.T_0017,
  shift: RU_TEXT.T_0018,
  city: RU_TEXT.T_0019,
  shortWork: [[RU_TEXT.T_0020, RU_TEXT.T_0021], [RU_TEXT.T_0022, RU_TEXT.T_0023]],
});

const staffPicker = document.getElementById('sheet-staff');
const content = document.getElementById('sheet-content');
const status = document.getElementById('sheet-status');
const exportLink = document.getElementById('sheet-export');
const excelLink = document.getElementById('sheet-excel');
const query = new URLSearchParams(location.search);
if (query.get('from') === 'master') document.getElementById('sheet-back').href = '/m';
const printButton = document.getElementById('sheet-print');
printButton.onclick = () => window.print();

async function loadJson(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(TEXT.failure);
  return response.json();
}

function renderTable(columns, rows) {
  const table = document.createElement('table');
  const heading = table.createTHead().insertRow();
  for (const column of columns) {
    const cell = document.createElement('th');
    cell.textContent = column;
    heading.append(cell);
  }
  const body = table.createTBody();
  for (const row of rows) {
    const line = body.insertRow();
    for (const value of row) line.insertCell().textContent = value ?? '';
  }
  return table;
}

async function renderSheet() {
  const staffId = staffPicker.value;
  printButton.disabled = true;
  exportLink.hidden = true;
  excelLink.hidden = true;
  content.replaceChildren();
  status.textContent = '';
  if (!staffId) return;
  try {
    const sheet = await loadJson('/api/v1/routes/' + encodeURIComponent(staffId) + '/sheet');
    if (staffPicker.value !== staffId) return;
    const heading = document.createElement('h2');
    heading.textContent = `${TEXT.staff}: ${sheet.name.replace(/\s*\(«[^»]+»\)$/, '')}`;
    const details = document.createElement('div');
    details.className = 'sheet-details';
    const center = document.createElement('p');
    center.textContent = `${TEXT.center}: ${sheet.center.replace(/^СЦ\s+/, '')}`;
    const shift = document.createElement('p');
    shift.textContent = `${TEXT.shift}: ${sheet.date.split('-').reverse().join('.')}, ${sheet.shift.start}–${sheet.shift.end}`;
    const base = document.createElement('p');
    base.textContent = `${TEXT.base}: ${sheet.base_address}`;
    const inventory = [...new Set([...Object.keys(sheet.capacity), ...Object.keys(sheet.consumption)])];
    const equipment = renderTable(TEXT.equipment, inventory.map(code => [
      code, sheet.initial_load[code] ?? 0, sheet.capacity[code] ?? 0, sheet.consumption[code] ?? 0, sheet.remaining[code] ?? 0,
    ]));
    const stops = renderTable(TEXT.stops, sheet.stops.map(stop => [
      stop.start === stop.end ? stop.start : `${stop.start}–${stop.end}`,
      [stop.order_id, TEXT.shortWork.reduce((work, [full, short]) => work.replace(full, short), stop.work)].filter(Boolean).join('\n'),
      stop.address.replace(new RegExp(RU_TEXT.T_0024 + TEXT.city + ',\\s*', 'i'), ''),
      Object.entries(stop.equipment).filter(([, quantity]) => quantity).map(([code, quantity]) => `${code}: ${quantity}`).join(', '),
      '', '',
    ]));
    stops.className = 'route-stops';
    const signature = document.createElement('div');
    signature.className = 'sheet-signatures';
    for (const label of TEXT.signatures) {
      const line = document.createElement('span');
      line.textContent = label;
      signature.append(line);
    }
    details.append(center, shift, base);
    content.append(heading, details, equipment, stops, signature);
    exportLink.href = '/api/v1/export/route/' + encodeURIComponent(staffId) + '.csv';
    exportLink.hidden = false;
    excelLink.href = '/api/v1/export/route/' + encodeURIComponent(staffId) + '.xlsx';
    excelLink.hidden = false;
    printButton.disabled = false;
  } catch {
    if (staffPicker.value === staffId) status.textContent = TEXT.failure;
  }
}

staffPicker.onchange = renderSheet;
try {
  const [plan, people] = await Promise.all([loadJson('/api/v1/plan'), loadJson('/api/v1/staff-directory')]);
  staffPicker.replaceChildren(...plan.routes.filter(route => route.stops.length).map(route =>
    new Option(people[route.staff_id] || route.staff_id, route.staff_id)));
  const staffId = query.get('staff');
  if ([...staffPicker.options].some(option => option.value === staffId)) staffPicker.value = staffId;
  await renderSheet();
} catch {
  status.textContent = TEXT.failure;
}
