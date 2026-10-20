'use strict';

// ============================================================
// Вспомогательные функции и общее состояние
// ============================================================

// Короткие обёртки над querySelector
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

// CSRF-токен, который сервер положил в <meta>
const csrf = $('meta[name="csrf-token"]').content;

// Подписи для интерфейса
const platformNames = {vk: 'ВКонтакте', ok: 'Одноклассники', max: 'MAX', web: 'Сайты и СМИ'};
const platformMarks = {vk: 'VK', ok: 'ОК', max: 'М', web: 'СМИ'};
const stateNames = {
  queued: 'В очереди',
  running: 'Выполняется',
  completed: 'Завершён',
  partial: 'Частичный результат',
  failed: 'Ошибка',
  interrupted: 'Прерван',
};
const relevanceNames = {
  relevant: 'Связанные',
  indirect: 'Косвенно',
  review: 'Неоднозначное',
  irrelevant: 'Не подходит',
};

// Всё текущее состояние страницы хранится в одном объекте
const state = {
  sources: [],              // список источников пользователя
  search: null,             // текущий открытый поиск
  publications: [],         // публикации текущего поиска
  queryType: 'topic',       // topic - по тезису, article - сравнение с оригиналом
  filter: 'all',            // выбранная вкладка фильтра
  poll: null,               // таймер опроса сервера
  detail: null,             // id публикации, открытой в боковой панели
  loading: false,           // идёт запуск поиска
  selectionBusy: new Set(), // публикации, чьи галочки сейчас сохраняются
};

// Пустое состояние результатов запоминаем, чтобы вернуть его по кнопке «Новый поиск»
const initialEmpty = $('#results-body').innerHTML;
let toastTimer;

// Экранирование HTML: любые данные из публикаций вставляем только через эту функцию
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[char]));
}

// SVG-иконка из спрайта в index.html
function icon(name) {
  return `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
}

// Форматирует дату по-русски; для даты без времени берём полдень, чтобы не съезжал часовой пояс
function dateLabel(value, includeTime = false) {
  if (!value) return 'Дата неизвестна';

  const d = new Date(value.length === 10 ? value + 'T12:00:00' : value);
  if (Number.isNaN(d.getTime())) return 'Дата неизвестна';

  const options = {day: '2-digit', month: '2-digit', year: 'numeric'};
  if (includeTime) {
    options.hour = '2-digit';
    options.minute = '2-digit';
  }
  return new Intl.DateTimeFormat('ru-RU', options).format(d);
}

// Всплывающее уведомление на 5 секунд
function toast(message) {
  $('#toast').textContent = message;
  $('#toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    $('#toast').hidden = true;
  }, 5000);
}

// Показывает (или скрывает при пустом тексте) сообщение об ошибке
function showError(selector, message) {
  $(selector).textContent = message;
  $(selector).hidden = !message;
}

// Запрос к API: добавляет CSRF-токен и превращает ошибки в понятные сообщения
async function api(url, options = {}) {
  let response;
  try {
    response = await fetch(url, {
      credentials: 'same-origin',
      ...options,
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrf,
        ...options.headers,
      },
    });
  } catch (_) {
    throw new Error('Нет связи с сервером. Проверьте соединение и повторите действие.');
  }

  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || `Ошибка сервера (${response.status}). Повторите позже.`);
  }
  return data;
}

// Разметка «пустого состояния» (когда нечего показывать)
function empty(title, description, symbol = 'search') {
  return `<div class="empty-state">
    <div class="empty-symbol">${icon(symbol)}</div>
    <h3>${esc(title)}</h3>
    <p>${esc(description)}</p>
  </div>`;
}

// ============================================================
// Навигация между разделами (по #хэшу в адресе)
// ============================================================

function route() {
  let view = location.hash.slice(1) || 'search';
  if (!['search', 'sources', 'history', 'help'].includes(view)) {
    view = 'search';
  }

  // Показываем нужный раздел, остальные прячем
  $$('.view').forEach(el => {
    el.hidden = el.id !== `view-${view}`;
  });

  // Подсвечиваем пункт меню
  $$('[data-view]').forEach(el => {
    const isActive = el.dataset.view === view;
    el.classList.toggle('active', isActive);
    el.setAttribute('aria-current', isActive ? 'page' : 'false');
  });

  $('#breadcrumb-title').textContent = {
    search: 'Поиск публикаций',
    sources: 'Мои источники',
    history: 'История поисков',
    help: 'Как это работает',
  }[view];

  // История подгружается при каждом открытии
  if (view === 'history') loadHistory();
}

// ============================================================
// Форма поиска
// ============================================================

// Устанавливает период «последние N дней»
function setPeriod(days) {
  const end = new Date();
  const start = new Date();
  start.setDate(start.getDate() - days + 1);

  // Локальная дата в формате ГГГГ-ММ-ДД (toISOString дал бы UTC и мог бы сдвинуть день)
  const localISO = d =>
    `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

  $('#date-from').value = localISO(start);
  $('#date-to').value = localISO(end);
  $$('[data-days]').forEach(el => el.classList.toggle('active', Number(el.dataset.days) === days));
}

// Переключает тип запроса: поиск по тезису / сравнение с оригиналом
function setQueryType(type) {
  if (state.queryType !== type) {
    $('#query').value = '';
  }
  state.queryType = type;

  $$('[data-query-type]').forEach(el => {
    const active = el.dataset.queryType === type;
    el.classList.toggle('selected', active);
    el.setAttribute('aria-pressed', String(active));
  });

  const isArticle = type === 'article';
  $('#query').placeholder = isArticle
    ? 'Вставьте текст исходной новости'
    : 'Например: Дорожное движение Красноярск';
  $('#query').maxLength = isArticle ? 12000 : 200;
  $('#mode-caption').textContent = isArticle ? 'Похожая тема, другое событие' : 'Ключевые слова и смысл';
  $('#mode-hint').textContent = isArticle
    ? 'Покажем полный эталон, краткое изложение и найденные новости о другом событии с объяснением отличий.'
    : 'Слова могут стоять в разных частях новости. Названия сохраняем; проверяем связь по тексту. Для точной фразы используйте кавычки. Регион в форме также ограничивает результат.';
  $('#query').rows = isArticle ? 5 : 2;
  $('.examples').hidden = isArticle;
}

// Список источников для выбора в форме (виден только в режиме «По моим источникам»)
function renderSourcePicker() {
  // Запоминаем уже отмеченные, чтобы не сбросить выбор при перерисовке
  const selected = new Set($$('#source-options input:checked').map(el => el.value));
  const platforms = $$('input[name="platform"]:checked').map(el => el.value);
  const available = state.sources.filter(s => platforms.includes(s.platform));

  if (available.length) {
    $('#source-options').innerHTML = available.map(s =>
      `<label><input type="checkbox" value="${esc(s.id)}" ${selected.has(s.id) ? 'checked' : ''}>` +
      `${esc(s.name)} <span class="platform-dot ${s.platform}">${platformMarks[s.platform]}</span></label>`
    ).join('');
  } else {
    $('#source-options').innerHTML =
      '<p>Нет источников для выбранных площадок. <a href="#sources">Добавьте источник</a>, чтобы искать по своему списку.</p>';
  }

  $('#source-picker').hidden = $('#search-mode').value !== 'sources';
}

// Собирает данные формы для отправки на сервер
function formData() {
  const scope = $('#search-mode').value;
  return {
    query: $('#query').value.trim(),
    query_type: state.queryType,
    date_from: $('#date-from').value,
    date_to: $('#date-to').value,
    region: $('#region').value.trim(),
    city: $('#city').value.trim(),
    fresh_only: $('#fresh-only').checked,
    limit: Number($('#result-limit').value),
    mode: state.queryType === 'article' ? 'compare' : 'by_thesis',
    scope: scope,
    platforms: $$('input[name="platform"]:checked').map(el => el.value),
    source_ids: scope === 'sources' ? $$('#source-options input:checked').map(el => el.value) : [],
  };
}

// Отправляет поиск на сервер и открывает его результаты
async function launchSearch(data) {
  if (state.loading) return;

  state.loading = true;
  $('#submit-search').disabled = true;
  showError('#form-error', '');

  try {
    const result = await api('/api/searches', {method: 'POST', body: JSON.stringify(data)});
    state.filter = 'all';
    $('#filter-text').value = '';
    await openSearch(result.id);
  } catch (error) {
    showError('#form-error', error.message);
  } finally {
    state.loading = false;
    $('#submit-search').disabled = false;
  }
}

// ============================================================
// Источники пользователя
// ============================================================

async function loadSources() {
  try {
    state.sources = (await api('/api/sources')).sources;
    $('#source-count').textContent = state.sources.length;

    if (state.sources.length) {
      $('#sources-list').innerHTML = state.sources.map(s =>
        `<div class="source-row">` +
          `<span class="platform-dot ${s.platform}">${platformMarks[s.platform]}</span>` +
          `<div class="source-row-content">` +
            `<strong>${esc(s.name)}</strong>` +
            `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.url)}</a>` +
            `<small class="source-region-label">${esc(s.region)} · Доступность проверяется при поиске</small>` +
          `</div>` +
          `<div class="source-actions">` +
            `<button class="icon-button" data-edit-source="${s.id}" aria-label="Редактировать ${esc(s.name)}">${icon('edit')}</button>` +
            `<button class="icon-button" data-delete-source="${s.id}" aria-label="Удалить ${esc(s.name)}">${icon('trash')}</button>` +
          `</div>` +
        `</div>`
      ).join('');
    } else {
      $('#sources-list').innerHTML = empty(
        'Ваш список источников',
        'Добавьте группу, канал или сайт. Источники будут доступны в форме поиска.',
        'sources'
      );
    }
    renderSourcePicker();
  } catch (error) {
    $('#sources-list').innerHTML = empty('Не удалось загрузить источники', error.message, 'help');
  }
}

// Открывает форму добавления/редактирования источника
function sourceForm(source = null) {
  $('#source-form').hidden = false;
  $('#source-form-title').textContent = source ? 'Редактировать источник' : 'Новый источник';
  $('#source-id').value = source?.id || '';
  $('#source-name').value = source?.name || '';
  $('#source-url').value = source?.url || '';
  $('#source-region').value = source?.region || 'Красноярский край';
  showError('#source-error', '');
  $('#source-name').focus();
}

// ============================================================
// Открытие поиска и опрос сервера
// ============================================================

async function openSearch(id) {
  clearTimeout(state.poll);
  state.search = {id};
  location.hash = 'search';
  await refreshSearch(id);
}

// Запрашивает у сервера актуальное состояние поиска; пока он идёт - повторяет каждые 1,8 с
async function refreshSearch(id) {
  try {
    const result = await api(`/api/searches/${id}`);
    // Пользователь уже открыл другой поиск - этот ответ не нужен
    if (state.search?.id !== id) return;

    state.search = result.search;
    state.groups = result.groups;
    state.grouped = result.grouped;

    // Галочки, которые сейчас сохраняются, не перезаписываем ответом сервера
    const current = new Map(state.publications.map(p => [p.id, p.included]));
    state.publications = result.publications.map(p =>
      state.selectionBusy.has(p.id) ? {...p, included: current.get(p.id)} : p
    );

    renderResults();

    if (['queued', 'running'].includes(result.search.state)) {
      state.poll = setTimeout(() => refreshSearch(id), 1800);
    }
  } catch (error) {
    if (state.search?.id !== id) return;
    $('#job-warnings').hidden = false;
    $('#job-warnings').innerHTML =
      `<p>${esc(error.message)}</p><button class="button secondary" id="refresh-results">Обновить результат</button>`;
    $('#job-progress').hidden = true;
  }
}

// ============================================================
// Отрисовка результатов
// ============================================================

function renderResults() {
  const s = state.search;
  if (!s?.state) return;

  const pubs = state.publications;
  const included = pubs.filter(p => p.included).length;
  const active = ['queued', 'running'].includes(s.state);

  // Шапка блока результатов и кнопка Excel
  $('#results-count').textContent = pubs.length;
  $('#results-subtitle').textContent = `${stateNames[s.state]} · Выбрано для Excel: ${included}`;
  $('#export-button').disabled = !included;
  $('#export-count').textContent = included ? `· ${included}` : '';

  // Строка с условиями запуска
  const queryShort = s.query.length > 180 ? s.query.slice(0, 180) + '…' : s.query;
  $('#run-context').hidden = false;
  $('#run-context').innerHTML =
    `<strong>Результат запроса: ${esc(queryShort)}</strong>` +
    `<small>${dateLabel(s.date_from)} — ${dateLabel(s.date_to)} · ${esc(s.region)}${s.city ? ', ' + esc(s.city) : ''}` +
    ` · ${s.mode === 'compare' ? 'Сравнение с оригиналом' : 'Поиск по тезису'}` +
    ` · ${s.scope === 'sources' ? 'По моим источникам' : 'Расширенный поиск'}` +
    ` · до ${s.limit} записей${s.fresh_only ? ' · За 24 часа к моменту запуска' : ''}` +
    ` · ${s.platforms.map(p => platformNames[p]).join(', ')}</small>` +
    (s.region_defaulted ? '<p>Регион не был указан: использован Красноярский край.</p>' : '');

  renderComparison(s);

  // Индикатор выполнения
  $('#job-progress').hidden = !active;
  $('#progress-label').textContent = s.stage;
  $('#progress-detail').textContent = s.total
    ? `Обработано ${s.progress} из ${s.total} кандидатов. Результаты сохраняются по мере получения.`
    : 'Проверяем поисковые запросы. Можно перейти в другой раздел и вернуться позже.';

  // Предупреждения и кнопка повтора
  $('#job-warnings').hidden = !s.warnings.length && !['failed', 'interrupted'].includes(s.state);
  $('#job-warnings').innerHTML =
    s.warnings.map(w => `<p>${esc(w)}</p>`).join('') +
    (s.state === 'interrupted' ? '<p>Сервер был перезапущен. Полученные результаты сохранены.</p>' : '') +
    (!active && ['failed', 'partial', 'interrupted'].includes(s.state)
      ? '<button id="retry-search" class="button secondary" type="button">Повторить с теми же условиями</button>'
      : '');

  // Вкладки фильтра со счётчиками
  $('#results-toolbar').hidden = !pubs.length;
  const counts = {
    all: pubs.length,
    relevant: pubs.filter(p => p.relevance === 'relevant').length,
    indirect: pubs.filter(p => p.relevance === 'indirect').length,
    review: pubs.filter(p => p.relevance === 'review').length,
    included: included,
  };
  $$('[data-filter]').forEach(el => {
    el.classList.toggle('active', el.dataset.filter === state.filter);
    el.setAttribute('aria-pressed', String(el.dataset.filter === state.filter));
    $('span', el).textContent = counts[el.dataset.filter];
  });

  // Какие публикации показываем: по вкладке и по тексту в поле «Найти в подборке»
  const filterText = $('#filter-text').value.toLocaleLowerCase('ru');
  const visible = pubs.filter(p => {
    const byTab = state.filter === 'all' ||
      (state.filter === 'included' ? p.included : p.relevance === state.filter);
    const haystack = [p.title, p.source, p.original_text, p.snippet].join(' ').toLocaleLowerCase('ru');
    return byTab && haystack.includes(filterText);
  });

  if (visible.length) {
    // Запоминаем, какие группы были раскрыты, чтобы не сворачивать их при обновлении
    const opened = new Set($$('details[data-group][open]').map(el => el.dataset.group));

    $('#results-body').innerHTML = ['relevant', 'indirect', 'review', 'irrelevant'].map(status => {
      const members = visible.filter(p => p.relevance === status);
      if (!members.length) return '';

      let rows = members.map(publicationRow).join('');

      // Если публикаций больше 50, группируем их по источникам
      if (state.grouped) {
        rows = (state.groups || []).map(group => {
          const items = members.filter(p => group.publication_ids.includes(p.id));
          if (!items.length) return '';
          const key = status + ':' + group.id;
          return `<details class="source-group" data-group="${esc(key)}" ${opened.has(key) ? 'open' : ''}>` +
            `<summary><strong>${esc(group.title)}</strong> · ${esc(group.id)} · ${items.length} публикаций ` +
            `<span>Развернуть группу</span></summary>` +
            `<div class="publication-list">${items.map(publicationRow).join('')}</div></details>`;
        }).join('');
      }

      return `<section class="relevance-block ${status}">` +
        `<h3>${relevanceNames[status]} <span>${members.length}</span></h3>` +
        (state.grouped ? rows : `<div class="publication-list">${rows}</div>`) +
        `</section>`;
    }).join('');

  } else if (pubs.length) {
    // Публикации есть, но фильтр всё скрыл
    $('#results-body').innerHTML = empty('Нет материалов по этому фильтру', 'Измените фильтр или строку поиска внутри подборки.');

  } else if (active) {
    // Поиск ещё идёт
    $('#results-body').innerHTML = empty('Собираем вашу подборку', 'Первые материалы появятся после получения и проверки оригиналов.');

  } else if (['failed', 'interrupted'].includes(s.state)) {
    $('#results-body').innerHTML = empty('Поиск не завершён', 'Причина указана выше. Исправьте подключение или повторите попытку.', 'help');

  } else {
    // Поиск завершён, но ничего не найдено: предлагаем смягчить условия
    const unavailable = (s.diagnostics || []).filter(d => d.stage === 'fetch').length;
    $('#results-body').innerHTML =
      empty(
        'Подходящих публикаций не найдено',
        unavailable
          ? `Не удалось проверить ${unavailable} источников. Это не означает, что новостей нет: причины и ссылки показаны ниже.`
          : 'Измените условия поиска. Похожие слова не подставляются автоматически.'
      ) +
      `<div class="empty-actions">` +
        `<button class="button secondary" data-relax="query">Изменить запрос</button>` +
        `<button class="button secondary" data-relax="region">Расширить регион до России</button>` +
        `<button class="button secondary" data-relax="fresh" ${s.fresh_only ? '' : 'disabled'}>Отключить «Свежее»</button>` +
        `<button class="button secondary" data-relax="city" ${s.city ? '' : 'disabled'}>Убрать город</button>` +
      `</div>`;
  }

  // Блок «Что проверено и какие есть ограничения»
  $('#coverage-details').hidden = false;
  const diagnostics = s.diagnostics || [];
  if (!pubs.length && !active && diagnostics.length) {
    $('#coverage-details').open = true;
  }

  // Подписи причин, по которым ссылка не попала в подборку
  const stages = {
    fetch: 'Оригинал недоступен',
    date: 'Вне периода',
    freshness: 'Не подтверждена свежесть',
    exact_match: 'Нет ключевых слов или указанной фразы',
    analysis: 'Не соответствует условиям',
    analysis_error: 'Анализ недоступен',
    duplicate: 'Повтор',
    same_reference: 'Сам эталон',
  };

  $('#coverage-content').innerHTML =
    `<p>Найдено уникальных ссылок: ${esc(s.total)}. Обработано: ${esc(s.progress)}. В подборке: ${pubs.length}.</p>` +
    s.coverage.map(c =>
      `<p>${esc(c.source)} · страница ${c.page} · ${c.status === 'ok' ? `найдено в выдаче: ${c.found}` : 'запрос не выполнен'}</p>`
    ).join('') +
    (diagnostics.length
      ? `<h3>Почему ссылки не вошли в подборку</h3>` +
        `<p>Это найденные Яндексом ссылки, а не подтверждённые результаты. Их можно открыть и проверить вручную.</p>` +
        `<ul class="rejected-links">${diagnostics.map(d =>
          `<li><a href="${esc(d.url)}" target="_blank" rel="noopener noreferrer">${esc(d.title || d.url)}</a>` +
          `<p><strong>${esc(stages[d.stage] || 'Пропущено')}.</strong> ${esc(d.reason)}</p></li>`
        ).join('')}</ul>`
      : '');
}

// Метки «Эмоция» и «Стиль» с пояснением
function textLabels(p) {
  return `<div class="text-labels">` +
      `<span class="badge text-label">Эмоция: ${esc(p.emotion || 'не определена')}</span>` +
      `<span class="badge text-label">Стиль: ${esc(p.style || 'не определён')}</span>` +
    `</div>` +
    `<p class="analysis-explanation">${esc(p.emotion_reason || 'Анализ ещё не выполнен.')} ${esc(p.style_reason || '')}</p>`;
}

// Одна строка списка публикаций
function publicationRow(p) {
  const notReceived = p.fetch_status !== 'received';
  const excerpt = p.evidence || p.original_text.slice(0, 350) || p.snippet;

  return `<article class="publication-row">` +
    `<input type="checkbox" data-include="${p.id}" ${p.included ? 'checked' : ''} ` +
      `${state.selectionBusy.has(p.id) ? 'disabled' : ''} aria-label="Включить в Excel: ${esc(p.title)}">` +
    `<div>` +
      `<button class="publication-title" data-detail="${p.id}" type="button">${esc(p.title)}</button>` +
      `<div class="publication-meta">` +
        `<span class="platform-dot ${p.platform}">${platformMarks[p.platform]}</span>` +
        `<span>${esc(p.source)}</span>` +
        (p.placements > 1 ? `<span>· ${p.placements} размещения этого текста</span>` : '') +
      `</div>` +
      `<p class="publication-excerpt">` +
        (notReceived ? '<strong>Описание из поиска · оригинал недоступен.</strong> ' : '') +
        `${esc(excerpt)}</p>` +
      `<p class="result-reason">${esc(p.reason)}</p>` +
      (notReceived ? `<p class="hint">${esc(p.fetch_note)}</p>` : '') +
      textLabels(p) +
      (p.difference
        ? `<p class="difference"><strong>Отличие от эталона:</strong> ${esc(p.difference)}</p>` +
          `<details><summary>Цитата из эталона</summary><blockquote>${esc(p.reference_evidence)}</blockquote></details>`
        : '') +
    `</div>` +
    `<div class="publication-side">` +
      `<span class="badge ${p.relevance}">${relevanceNames[p.relevance]}</span>` +
      (p.fresh ? '<span class="badge fresh">Свежее</span>' : '') +
      (!p.published_at ? '<span class="badge review">Дата не подтверждена</span>' : '') +
      `<time datetime="${esc(p.published_at || '')}">${dateLabel(p.published_at, p.published_at?.length > 10)}</time>` +
      `<a href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">Оригинал ${icon('external')}</a>` +
    `</div>` +
  `</article>`;
}

// Панель «Сравнение с оригиналом» (только для режима compare)
function renderComparison(s) {
  const panel = $('#comparison-panel');
  panel.hidden = s.mode !== 'compare';
  if (panel.hidden) return;

  const reference = s.reference || {};
  // Если пользователь раскрыл эталон, не сворачиваем его при обновлении
  const expanded = $('details[open]', panel) !== null;

  panel.innerHTML =
    `<h3>Сравнение с оригиналом</h3>` +
    `<details ${expanded ? 'open' : ''}><summary>Исходный текст — полный эталон</summary>` +
      (reference.url ? `<a href="${esc(reference.url)}" target="_blank" rel="noopener noreferrer">Открыть источник ${icon('external')}</a>` : '') +
      `<div class="original-text">${esc(reference.original_text || 'Эталон ещё не получен.')}</div>` +
    `</details>` +
    `<h4>Сокращённая версия</h4>` +
    `<p class="reference-summary">${esc(reference.summary || 'Краткое изложение ещё не подготовлено.')}</p>` +
    textLabels(reference) +
    `<h4>Похожие новости о другом событии</h4>` +
    `<p class="hint">Найденные материалы и их отличия показаны ниже. Дата и «Свежее» ограничивают найденные публикации; исходный эталон сохраняется целиком.</p>`;
}

// ============================================================
// Повтор поиска и восстановление формы
// ============================================================

// Заполняет форму условиями прежнего поиска
function restoreForm(s) {
  setQueryType(s.mode === 'compare' ? 'article' : 'topic');
  $('#query').value = s.query;
  $('#region').value = s.region;
  $('#city').value = s.city || '';
  $('#fresh-only').checked = Boolean(s.fresh_only);
  $('#result-limit').value = s.limit || 20;
  $('#date-from').value = s.date_from;
  $('#date-to').value = s.date_to;
  $('#search-mode').value = s.scope || 'extended';
  $$('input[name="platform"]').forEach(el => {
    el.checked = s.platforms.includes(el.value);
  });
  renderSourcePicker();
  $$('#source-options input').forEach(el => {
    el.checked = s.sources.some(source => source.id === el.value);
  });
  updateLocationHints();
}

// Запускает поиск заново с теми же условиями
async function repeatSearch(id) {
  if (state.loading) return;
  state.loading = true;
  try {
    const original = (await api(`/api/searches/${id}`)).search;
    restoreForm(original);
    const result = await api(`/api/searches/${id}/repeat`, {method: 'POST', body: '{}'});
    state.filter = 'all';
    $('#filter-text').value = '';
    await openSearch(result.id);
  } catch (error) {
    toast(error.message);
  } finally {
    state.loading = false;
  }
}

// ============================================================
// Регион и город: подсказки из справочника
// ============================================================

let locations = [];

function updateLocationHints() {
  const region = $('#region').value.trim() || 'Красноярский край';
  $('#workspace-region').textContent = region;
  $('#region-hint').textContent = $('#region').value.trim()
    ? `Регион: ${region}. Город проверяется по справочнику.`
    : 'Регион не указан — будет использован Красноярский край.';

  // В подсказки города попадают только города выбранного региона (или все при «Россия»)
  $('#city-list').innerHTML = locations
    .filter(row =>
      row.region.toLowerCase() === region.toLowerCase() ||
      row.region_name.toLowerCase() === region.toLowerCase() ||
      region === 'Россия'
    )
    .map(row => `<option value="${esc(row.city)}">`)
    .join('');
}

async function loadLocations() {
  try {
    locations = (await api('/api/locations')).locations;
    $('#region-list').innerHTML = ['Россия', ...new Set(locations.map(row => row.region))]
      .sort()
      .map(region => `<option value="${esc(region)}">`)
      .join('');
    updateLocationHints();
  } catch (_) {
    $('#region-hint').textContent = 'Подсказки городов недоступны; введите название вручную. Сервер проверит его при поиске.';
  }
}

// ============================================================
// Боковая панель с подробностями публикации
// ============================================================

function renderDetail(id) {
  const p = state.publications.find(pub => pub.id === id);
  if (!p) return;
  state.detail = id;

  // Подсвечиваем цитату в оригинальном тексте
  const escapedText = esc(p.original_text);
  const highlighted = p.evidence && p.original_text.includes(p.evidence)
    ? escapedText.replace(esc(p.evidence), `<mark>${esc(p.evidence)}</mark>`)
    : escapedText;

  $('#publication-detail').innerHTML =
    `<span class="badge ${p.relevance}">${relevanceNames[p.relevance]}</span>` +
    `<h2 id="detail-title">${esc(p.title)}</h2>` +
    `<div class="detail-meta"><span>${esc(p.source)}</span><span>${platformNames[p.platform]}</span><span>${dateLabel(p.published_at)}</span></div>` +
    `<div class="detail-actions">` +
      `<a class="button primary" href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">Открыть оригинал ${icon('external')}</a>` +
      `<button class="button secondary" data-toggle-detail="${p.id}" ${state.selectionBusy.has(p.id) ? 'disabled' : ''}>` +
        `${p.included ? 'Исключить из Excel' : 'Включить в Excel'}</button>` +
    `</div>` +
    `<section class="detail-section"><h3>Почему найдено</h3><p>${esc(p.reason)}</p>` +
      (p.evidence ? `<blockquote>${esc(p.evidence)}</blockquote>` : '') +
    `</section>` +
    `<section class="detail-section"><h3>Связь с регионом</h3><p>${esc(p.region_reason || 'Не установлена. Требуется ручная проверка.')}</p></section>` +
    `<section class="detail-section"><h3>${p.fetch_status === 'received' ? 'Оригинальный текст' : 'Оригинал недоступен'}</h3>` +
      (p.original_text
        ? `<div class="original-text">${highlighted}</div>`
        : `<p>${esc(p.fetch_note)}</p><h3 class="snippet-label">Сниппет поисковика</h3>` +
          `<div class="original-text">${esc(p.snippet || 'Нет сниппета.')}</div>`) +
      `<p class="hint">Получено ${dateLabel(p.fetched_at, true)}. ` +
        `${p.published_at ? '' : 'Дата публикации неизвестна; соответствие периоду не подтверждено.'}</p>` +
    `</section>` +
    `<section class="detail-section"><h3>Проверка утверждений</h3>` +
      `<p>Независимый фактчекинг не проводился. Отметка «Связанные» означает связь с запросом, а не доказанную истинность всех утверждений.</p>` +
    `</section>`;

  // Блок «Эмоция и стиль» (и отличие от эталона в режиме сравнения)
  $('#publication-detail').insertAdjacentHTML('beforeend',
    `<section class="detail-section"><h3>Эмоция и стиль</h3>${textLabels(p)}` +
    (p.difference
      ? `<h3>Отличие от эталона</h3><p>${esc(p.difference)}</p><blockquote>${esc(p.reference_evidence)}</blockquote>`
      : '') +
    `</section>`
  );

  if (!$('#publication-dialog').open) {
    $('#publication-dialog').showModal();
  }
}

// Включает/исключает публикацию из Excel. Галочка меняется сразу (оптимистично),
// а при ошибке сервера откатывается назад.
async function togglePublication(id, included) {
  const p = state.publications.find(pub => pub.id === id);
  if (!p || state.selectionBusy.has(id)) return;

  const searchId = state.search.id;
  state.selectionBusy.add(id);
  const previous = p.included;
  p.included = Number(included);
  renderResults();
  if ($('#publication-dialog').open) renderDetail(id);

  try {
    await api(`/api/searches/${searchId}/publications/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({included}),
    });
  } catch (error) {
    // Откат
    p.included = previous;
    const current = state.publications.find(pub => pub.id === id);
    if (current) current.included = previous;
    toast(error.message);
  } finally {
    state.selectionBusy.delete(id);
    if (state.search?.id === searchId) {
      renderResults();
      if ($('#publication-dialog').open && state.detail === id) renderDetail(id);
    }
  }
}

// ============================================================
// История поисков и справка
// ============================================================

async function loadHistory() {
  try {
    const {searches} = await api('/api/searches');

    if (!searches.length) {
      $('#history-list').innerHTML = empty(
        'Здесь будет история поисков',
        'Запустите первый поиск. Его условия и результаты сохранятся в этом браузере.',
        'history'
      );
      return;
    }

    $('#history-list').innerHTML = searches.map(s => {
      const queryShort = s.query.length > 160 ? s.query.slice(0, 160) + '…' : s.query;
      // Цвет значка по состоянию поиска
      const badge = s.state === 'completed'
        ? 'relevant'
        : ['partial', 'failed', 'interrupted'].includes(s.state) ? 'review' : 'irrelevant';

      return `<article class="history-row">` +
        `<div>` +
          `<button type="button" data-open-search="${s.id}">${esc(queryShort)}</button>` +
          `<p>${s.mode === 'compare' ? 'Сравнение с оригиналом' : 'Поиск по тезису'} · ${esc(s.region)}${s.city ? ', ' + esc(s.city) : ''}` +
            ` · до ${s.limit} записей${s.fresh_only ? ' · Свежее' : ''}<br>` +
            `${dateLabel(s.date_from)} — ${dateLabel(s.date_to)} · Запущен ${dateLabel(s.created_at, true)}</p>` +
          `<button type="button" class="history-repeat" data-repeat-search="${s.id}">Повторить поиск</button>` +
        `</div>` +
        `<span class="badge ${badge}">${stateNames[s.state]}</span>` +
        `<div class="history-count"><strong>${s.results_count}</strong>публикаций</div>` +
      `</article>`;
    }).join('');
  } catch (error) {
    $('#history-list').innerHTML = empty('История недоступна', error.message, 'help');
  }
}

// Подключение сервисов и лимиты (раздел «Как это работает»)
async function loadStatus() {
  try {
    const s = await api('/api/status');
    $('#service-status').innerHTML =
      `<p>Yandex Search API<span>${s.search_configured ? 'Параметры указаны · доступ проверяется при поиске' : 'Нужно настроить доступ'}</span></p>` +
      `<p>YandexGPT 5<span>${s.ai_configured ? 'Параметры указаны · доступ проверяется при анализе' : 'Нужно настроить доступ'}</span></p>`;
    $('#limits-text').textContent =
      `До ${s.max_results} результатов. Бюджет обработки кандидатов и число страниц увеличиваются с выбранным количеством записей. ` +
      `До 20 выбранных источников и 999 запусков в час на пространство. Если подходящих материалов меньше, показываем найденные.`;
  } catch (error) {
    $('#service-status').textContent = error.message;
  }
}

// ============================================================
// Обработчики событий
// ============================================================

window.addEventListener('hashchange', route);

// Ссылка «К содержимому»
$('.skip-link').addEventListener('click', event => {
  event.preventDefault();
  $('#main').focus();
  $('#main').scrollIntoView({block: 'start'});
});

// Один общий обработчик кликов по всем кнопкам (делегирование событий)
document.addEventListener('click', async event => {
  const target = event.target.closest('button');
  if (!target) return;

  // Тип запроса, период, пример запроса
  if (target.dataset.queryType) setQueryType(target.dataset.queryType);
  if (target.dataset.days) setPeriod(Number(target.dataset.days));
  if (target.dataset.example) {
    $('#query').value = target.dataset.example;
    $('#query').focus();
  }

  // Вкладки фильтра и карточки публикаций
  if (target.dataset.filter) {
    state.filter = target.dataset.filter;
    renderResults();
  }
  if (target.dataset.detail) renderDetail(target.dataset.detail);
  if (target.dataset.toggleDetail) {
    const p = state.publications.find(p => p.id === target.dataset.toggleDetail);
    await togglePublication(p.id, !p.included);
  }

  // История
  if (target.dataset.openSearch) {
    state.filter = 'all';
    $('#filter-text').value = '';
    await openSearch(target.dataset.openSearch);
  }
  if (target.dataset.repeatSearch) await repeatSearch(target.dataset.repeatSearch);

  // Кнопки «смягчить условия» при пустом результате
  if (target.dataset.relax && state.search) {
    restoreForm(state.search);
    if (target.dataset.relax === 'query') {
      $('#query').focus();
      $('#query').select();
    }
    if (target.dataset.relax === 'region') {
      $('#region').value = 'Россия';
      $('#city').value = '';
    }
    if (target.dataset.relax === 'fresh') $('#fresh-only').checked = false;
    if (target.dataset.relax === 'city') $('#city').value = '';
    updateLocationHints();
    $('#search-form').scrollIntoView({block: 'start'});
    if (target.dataset.relax !== 'query') toast('Условия изменены. Нажмите «Найти публикации».');
  }

  // Источники: редактирование и удаление
  if (target.dataset.editSource) {
    sourceForm(state.sources.find(s => s.id === target.dataset.editSource));
  }
  if (target.dataset.deleteSource) {
    const source = state.sources.find(s => s.id === target.dataset.deleteSource);
    if (!window.confirm(`Удалить источник «${source.name}» из списка? Результаты прошлых поисков сохранятся.`)) return;
    target.disabled = true;
    try {
      await api(`/api/sources/${source.id}`, {method: 'DELETE'});
      await loadSources();
      toast('Источник удалён из списка');
    } catch (error) {
      toast(error.message);
      target.disabled = false;
    }
  }

  // Повтор и обновление при ошибках
  if (target.id === 'retry-search' && state.search) {
    await repeatSearch(state.search.id);
  }
  if (target.id === 'refresh-results' && state.search) {
    await refreshSearch(state.search.id);
  }
});

// Отправка формы поиска
$('#search-form').addEventListener('submit', event => {
  event.preventDefault();
  launchSearch(formData());
});

// Ctrl+Enter (или Cmd+Enter) в поле запроса отправляет форму
$('#query').addEventListener('keydown', event => {
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    $('#search-form').requestSubmit();
  }
});

$('#search-mode').addEventListener('change', renderSourcePicker);
$('#region').addEventListener('input', updateLocationHints);
$$('input[name="platform"]').forEach(el => el.addEventListener('change', renderSourcePicker));

// Ручное изменение дат снимает подсветку с кнопок «7/30/90 дней»
$$('.date-range input').forEach(el =>
  el.addEventListener('change', () => $$('[data-days]').forEach(b => b.classList.remove('active')))
);

$('#filter-text').addEventListener('input', renderResults);

// Галочки «включить в Excel» в списке результатов
$('#results-body').addEventListener('change', event => {
  if (event.target.dataset.include) {
    togglePublication(event.target.dataset.include, event.target.checked);
  }
});

// Кнопка «Новый поиск»: сбрасываем форму и результаты
$('#new-search').addEventListener('click', () => {
  clearTimeout(state.poll);
  state.search = null;
  state.publications = [];
  state.filter = 'all';
  $('#query').value = '';
  $('#filter-text').value = '';
  $('#results-body').innerHTML = initialEmpty;
  $('#results-count').textContent = '0';
  $('#results-subtitle').textContent = 'Результаты появятся здесь после поиска';
  ['#run-context', '#job-progress', '#job-warnings', '#results-toolbar', '#coverage-details', '#comparison-panel']
    .forEach(id => {
      $(id).hidden = true;
    });
  $('#export-button').disabled = true;
  $('#export-count').textContent = '';
  showError('#form-error', '');
  $('#query').focus();
});

// Форма источника
$('#add-source').addEventListener('click', () => sourceForm());
$('#cancel-source').addEventListener('click', () => {
  $('#source-form').hidden = true;
});

$('#source-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = $('button[type="submit"]', event.target);
  button.disabled = true;
  showError('#source-error', '');

  const id = $('#source-id').value;
  try {
    await api(id ? `/api/sources/${id}` : '/api/sources', {
      method: id ? 'PUT' : 'POST',
      body: JSON.stringify({
        name: $('#source-name').value.trim(),
        url: $('#source-url').value.trim(),
        region: $('#source-region').value.trim(),
      }),
    });
    $('#source-form').hidden = true;
    await loadSources();
    toast(id ? 'Изменения сохранены' : 'Источник добавлен');
  } catch (error) {
    showError('#source-error', error.message);
  } finally {
    button.disabled = false;
  }
});

// Боковая панель: закрытие по кнопке и по клику на затемнённую область слева
$('#close-detail').addEventListener('click', () => $('#publication-dialog').close());
$('#publication-dialog').addEventListener('click', event => {
  if (event.target === event.currentTarget && event.clientX < event.currentTarget.getBoundingClientRect().left) {
    event.currentTarget.close();
  }
});

// Скачивание Excel
$('#export-button').addEventListener('click', async () => {
  if (!state.search) return;

  const button = $('#export-button');
  button.disabled = true;
  try {
    const response = await fetch(`/api/searches/${state.search.id}/export`);
    if (!response.ok) {
      const data = await response.json();
      throw new Error(data.error || 'Не удалось создать файл.');
    }

    // Скачиваем через временную ссылку на blob
    const url = URL.createObjectURL(await response.blob());
    const a = document.createElement('a');
    a.href = url;
    a.download = `Медиаподборка-${state.search.id.slice(0, 8)}.xlsx`;
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast('Подборка выгружена в Excel');
  } catch (error) {
    toast(error.message || 'Не удалось скачать Excel. Проверьте соединение.');
  } finally {
    button.disabled = !state.publications.some(p => p.included);
  }
});

// ============================================================
// Старт страницы
// ============================================================

setPeriod(30);          // по умолчанию период - 30 дней
setQueryType('topic');  // по умолчанию - поиск по тезису
route();
// Грузим источники, статус сервисов и справочник городов параллельно
Promise.allSettled([loadSources(), loadStatus(), loadLocations()]);
