'use strict';

// Настройки отображения: тема (светлая/тёмная/системная) и масштаб интерфейса.
// Скрипт подключается в <head> без defer, чтобы тема применилась до отрисовки страницы.
(() => {
  const root = document.documentElement;
  const system = window.matchMedia('(prefers-color-scheme: dark)');

  let theme = 'system';
  let scale = 100;

  // Читаем сохранённые значения из localStorage
  try {
    const stored = localStorage.getItem('media-theme');
    if (['light', 'dark', 'system'].includes(stored)) {
      theme = stored;
    }

    const savedScale = Number(localStorage.getItem('media-scale'));
    if (Number.isInteger(savedScale) && savedScale >= 80 && savedScale <= 150) {
      scale = Math.round(savedScale / 10) * 10;
    }
  } catch (_) {
    /* Если хранилище отключено, настройки всё равно работают в рамках сессии. */
  }

  // Применяет текущие настройки к странице
  function apply() {
    root.dataset.theme = theme === 'system' ? (system.matches ? 'dark' : 'light') : theme;
    root.dataset.scale = String(scale);

    const output = document.getElementById('scale-value');
    if (output) {
      output.value = `${scale}%`;
      document.getElementById('scale-down').disabled = scale === 80;
      document.getElementById('scale-up').disabled = scale === 150;
    }
  }

  // Применяет и сохраняет настройки
  function save() {
    apply();
    try {
      localStorage.setItem('media-theme', theme);
      localStorage.setItem('media-scale', String(scale));
    } catch (_) {
      /* Настройка действует только до закрытия вкладки. */
    }
  }

  apply();
  // Следим за сменой системной темы
  system.addEventListener('change', apply);

  // Когда страница загрузится - подключаем элементы управления
  document.addEventListener('DOMContentLoaded', () => {
    const select = document.getElementById('theme-select');
    select.value = theme;

    select.addEventListener('change', () => {
      theme = select.value;
      save();
    });
    document.getElementById('scale-down').addEventListener('click', () => {
      scale = Math.max(80, scale - 10);
      save();
    });
    document.getElementById('scale-up').addEventListener('click', () => {
      scale = Math.min(150, scale + 10);
      save();
    });

    apply();
  });
})();