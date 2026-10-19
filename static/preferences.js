'use strict';
(() => {
  const root = document.documentElement;
  const system = window.matchMedia('(prefers-color-scheme: dark)');
  let theme = 'system', scale = 100;
  try {
    const stored = localStorage.getItem('media-theme');
    if (['light', 'dark', 'system'].includes(stored)) theme = stored;
    const savedScale = Number(localStorage.getItem('media-scale'));
    if (Number.isInteger(savedScale) && savedScale >= 80 && savedScale <= 150) scale = Math.round(savedScale / 10) * 10;
  } catch (_) { /* Preferences remain usable when storage is disabled. */ }
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
  function save() {
    apply();
    try { localStorage.setItem('media-theme', theme); localStorage.setItem('media-scale', String(scale)); } catch (_) { /* Session preference still applies. */ }
  }
  apply();
  system.addEventListener('change', apply);
  document.addEventListener('DOMContentLoaded', () => {
    const select = document.getElementById('theme-select');
    select.value = theme;
    select.addEventListener('change', () => { theme = select.value; save(); });
    document.getElementById('scale-down').addEventListener('click', () => { scale = Math.max(80, scale - 10); save(); });
    document.getElementById('scale-up').addEventListener('click', () => { scale = Math.min(150, scale + 10); save(); });
    apply();
  });
})();
