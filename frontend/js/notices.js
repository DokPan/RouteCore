const RU_TEXT = {
  T_0001: 'План пересчитан. Показатели не изменились.',
  T_0002: 'План пересчитан. Показатели обновлены.',
  T_0003: 'Подтвердить',
  T_0004: 'Отмена',
  T_0005: 'Отменить',
};

export const NOTICE_TEXT = Object.freeze({
  same: RU_TEXT.T_0001,
  updated: RU_TEXT.T_0002,
  proceed: RU_TEXT.T_0003,
  cancel: RU_TEXT.T_0004,
  undo: RU_TEXT.T_0005,
});

let dismissTimer;

export function showNotice(message, undo, duration = 7500) {
  const notice = document.getElementById('app-notice');
  notice.textContent = message;
  if (undo) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-outline';
    button.textContent = NOTICE_TEXT.undo;
    button.addEventListener('click', undo, { once: true });
    notice.append(' ', button);
  }
  notice.hidden = false;
  clearTimeout(dismissTimer);
  dismissTimer = setTimeout(() => { notice.hidden = true; }, duration);
}

export function confirmAction(message) {
  const dialog = document.getElementById('action-confirmation');
  const previous = document.querySelector('dialog.dispatch-dialog[open]');
  previous?.close();
  dialog.querySelector('p').textContent = message;
  dialog.querySelector('[value="yes"]').textContent = NOTICE_TEXT.proceed;
  dialog.querySelector('[value="no"]').textContent = NOTICE_TEXT.cancel;
  dialog.returnValue = 'no';
  return new Promise(resolve => {
    dialog.addEventListener('close', () => {
      if (previous?.isConnected) previous.showModal();
      resolve(dialog.returnValue === 'yes');
    }, { once: true });
    dialog.showModal();
  });
}
