// Results stay empty until the API returns a complete, verified live index.
let screenings = [];
const API_BASE = window.RERUN_API_BASE || '';
const canUseApi = location.protocol !== 'file:';
const isCalendarPage = document.body.dataset.page === 'calendar';
let apiRequestId = 0;

const storage = (() => {
  try { return window.localStorage; } catch (error) { return null; }
})();
const storedSaved = (() => {
  try { return storage ? JSON.parse(storage.getItem('rerun-saved') || '[]') : []; } catch (error) { return []; }
})();

const state = {
  city: 'los-angeles',
  selectedDate: todayDateKey(),
  query: '',
  sortMode: 'time',
  dateFilter: 'today',
  view: isCalendarPage ? 'calendar' : 'browse',
  calendarMonth: null,
  calendarSelectedDate: '',
  saved: new Set(Array.isArray(storedSaved) ? storedSaved : [])
};

const grid = document.querySelector('#screening-grid');
const emptyState = document.querySelector('#empty-state');
const emptyHeading = emptyState?.querySelector('h3');
const emptyMessage = emptyState?.querySelector('p');
const resultCount = document.querySelector('#result-count');
const template = document.querySelector('#screening-card-template');
const dialog = document.querySelector('#screening-dialog');
const dialogContent = document.querySelector('#dialog-content');
const citySelect = document.querySelector('#city-select');
const searchInput = document.querySelector('#search-input');
const sortSelect = document.querySelector('#sort-select');
const toast = document.querySelector('#toast');
const mobileNav = document.querySelector('#mobile-nav');
const menuButton = document.querySelector('.menu-button');
const dataStatus = document.querySelector('#data-status');
const refreshButton = null;
const calendarView = document.querySelector('#calendar-view');
const calendarTitle = document.querySelector('#calendar-title');
const calendarGrid = document.querySelector('#calendar-grid');
const calendarSelection = document.querySelector('#calendar-selection');
const calendarScreeningGrid = document.querySelector('#calendar-screening-grid');
const calendarPrev = document.querySelector('#calendar-prev');
const calendarNext = document.querySelector('#calendar-next');
const dayPrev = document.querySelector('#day-prev');
const dayNext = document.querySelector('#day-next');
const dayKicker = document.querySelector('#day-kicker');
const dayLabel = document.querySelector('#day-label');
const browseDayWord = document.querySelector('#browse-day-word');

function ensureLogoFallbacks() {
  document.querySelectorAll('img').forEach((image) => {
    const source = image.getAttribute('src') || '';
    if (source.includes('rerun-logo.svg')) image.src = '/assets/rerun-logo.png?v=20260909-mobile-logo';
    if (source.includes('rerun-mark.svg')) image.src = '/assets/rerun-mark.png?v=20260909-mobile-logo';
    image.addEventListener('error', () => {
      if (image.dataset.logoFallbackApplied) return;
      image.dataset.logoFallbackApplied = 'true';
      if (image.classList.contains('brand-logo') || image.closest('.hero-branding, .calendar-branding')) {
        image.src = '/assets/rerun-logo.svg?v=20260909-mobile-logo';
      } else if (image.closest('.footer-brand')) {
        image.src = '/assets/rerun-mark.svg?v=20260909-mobile-logo';
      }
    }, { once: true });
  });
}

ensureLogoFallbacks();

function apiUrl(pathname) {
  return `${API_BASE}${pathname}`;
}

function imageUrl(image) {
  if (!image) return '';
  try {
    const parsed = new URL(image, location.href);
    if (location.protocol === 'file:' || parsed.origin === location.origin) return parsed.href;
    return apiUrl(`/api/image?url=${encodeURIComponent(parsed.href)}`);
  } catch (error) {
    return image;
  }
}

function screeningsApiPath() {
  const params = new URLSearchParams({ city: state.city, limit: '500' });
  // Browse only needs the selected day; calendar/saved views need the full
  // upcoming catalog. Keeping this filter server-side makes the completed
  // live response smaller and faster to render.
  if (state.view === 'browse' && !['weekend', 'upcoming'].includes(state.dateFilter)) params.set('dateKey', state.selectedDate);
  return apiUrl(`/api/screenings?${params.toString()}`);
}

function setDataStatus(message, tone = 'default') {
  if (!dataStatus) return;
  dataStatus.textContent = message;
  dataStatus.dataset.tone = tone;
}

function setLoadingState(message = 'Indexing official feeds…') {
  updateDayNavigator();
  if (grid) {
    grid.replaceChildren();
    grid.hidden = true;
  }
  if (calendarView) calendarView.hidden = true;
  if (calendarScreeningGrid) calendarScreeningGrid.replaceChildren();
  if (emptyState) emptyState.hidden = true;
  if (resultCount) resultCount.textContent = 'Indexing live listings…';
  document.querySelectorAll('[data-view]').forEach((button) => button.classList.toggle('active', button.dataset.view === state.view));
  setDataStatus(message);
}

function setRefreshBusy(busy) {
  if (!refreshButton) return;
  refreshButton.disabled = busy;
  refreshButton.setAttribute('aria-busy', String(busy));
  refreshButton.classList.toggle('is-loading', busy);
  const actionLabel = busy ? 'Refreshing live sources…' : 'Recheck all live sources';
  refreshButton.title = actionLabel;
  refreshButton.setAttribute('aria-label', actionLabel);
  const label = refreshButton.querySelector('.refresh-label');
  if (label) label.textContent = actionLabel;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (character) => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[character]));
}

function cityLabel(value) {
  return { 'los-angeles':'Los Angeles', 'new-york':'New York', 'san-francisco':'San Francisco', arcata:'Arcata' }[value] || value;
}

function timeValue(time) {
  if (!time || typeof time !== 'string') return 0;
  const [clock, meridiem] = time.split(' ');
  let [hours, minutes] = clock.split(':').map(Number);
  if (meridiem === 'PM' && hours !== 12) hours += 12;
  if (meridiem === 'AM' && hours === 12) hours = 0;
  return hours * 60 + minutes;
}

function todayDateKey() {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/Los_Angeles', year: 'numeric', month: '2-digit', day: '2-digit'
  }).formatToParts(new Date());
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function dateKeyFromDate(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

function offsetDateKey(offset) {
  const date = dateFromKey(todayDateKey());
  date.setDate(date.getDate() + offset);
  return dateKeyFromDate(date);
}

function dateFromKey(dateKey) {
  const [year, month, day] = String(dateKey || '').split('-').map(Number);
  return year && month && day ? new Date(year, month - 1, day, 12) : new Date();
}

function relativeDayLabel(dateKey) {
  const today = dateFromKey(todayDateKey());
  const selected = dateFromKey(dateKey);
  const difference = Math.round((selected - today) / 86400000);
  if (difference === 0) return 'Today';
  if (difference === -1) return 'Yesterday';
  if (difference === 1) return 'Tomorrow';
  return 'Screening day';
}

function weekendDateKeys() {
  const today = dateFromKey(todayDateKey());
  const day = today.getDay();
  const start = new Date(today);
  if (day === 0) {
    start.setDate(start.getDate() - 1);
    return [dateKeyFromDate(start), todayDateKey()];
  }
  start.setDate(start.getDate() + (6 - day));
  const end = new Date(start);
  end.setDate(end.getDate() + 1);
  return [dateKeyFromDate(start), dateKeyFromDate(end)];
}

function isUpcoming(item) {
  const timestamp = item.startAt ? new Date(item.startAt).getTime() : NaN;
  return Number.isNaN(timestamp) || timestamp >= Date.now();
}

function matchesDateFilter(item) {
  const key = screeningDateKey(item);
  if (!key) return false;
  if (state.dateFilter === 'upcoming') return isUpcoming(item);
  if (state.dateFilter === 'weekend') {
    const [start, end] = weekendDateKeys();
    return key >= start && key <= end && isUpcoming(item);
  }
  return key === state.selectedDate && isUpcoming(item);
}

function updateDateFilters() {
  document.querySelectorAll('.date-filter').forEach((button) => {
    const active = button.dataset.dateFilter === state.dateFilter;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
}

function updateDayNavigator() {
  const key = state.selectedDate || todayDateKey();
  const date = dateFromKey(key);
  const relative = state.dateFilter === 'weekend' ? 'This weekend' : state.dateFilter === 'upcoming' ? 'All upcoming' : relativeDayLabel(key);
  if (dayKicker) dayKicker.textContent = relative;
  if (dayLabel) dayLabel.textContent = state.dateFilter === 'weekend' ? 'Saturday + Sunday' : state.dateFilter === 'upcoming' ? 'Every upcoming show' : date.toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric' });
  if (browseDayWord) browseDayWord.textContent = state.dateFilter === 'weekend' ? 'this weekend.' : state.dateFilter === 'upcoming' ? 'upcoming.' : state.dateFilter === 'today' ? 'tonight.' : state.dateFilter === 'tomorrow' ? 'tomorrow.' : `${date.toLocaleDateString(undefined, { weekday: 'long' }).toLowerCase()}.`;
  if (dayPrev) dayPrev.disabled = state.dateFilter === 'upcoming';
  if (dayNext) dayNext.disabled = state.dateFilter === 'upcoming';
  updateDateFilters();
}

function shiftSelectedDate(offset) {
  const date = dateFromKey(state.selectedDate || todayDateKey());
  date.setDate(date.getDate() + offset);
  state.selectedDate = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  state.calendarSelectedDate = state.selectedDate;
  state.calendarMonth = new Date(date.getFullYear(), date.getMonth(), 1);
  state.dateFilter = state.selectedDate === todayDateKey() ? 'today' : state.selectedDate === offsetDateKey(1) ? 'tomorrow' : 'day';
  state.view = 'browse';
  render();
  loadScreenings();
}

function screeningDateKey(item) {
  return String(item.startAt || '').slice(0, 10);
}

function groupScreenings(items) {
  const groups = new Map();
  items.forEach((item) => {
    const key = [item.sourceName, item.venue, screeningDateKey(item), String(item.title || '').trim().toLowerCase()].join('|');
    if (!groups.has(key)) {
      groups.set(key, { ...item, screenings: [] });
    }
    groups.get(key).screenings.push(item);
  });
  const result = [...groups.values()].map((group) => {
    group.screenings.sort((a, b) => new Date(a.startAt) - new Date(b.startAt));
    return group;
  });
  if (state.sortMode === 'venue') {
    result.sort((a, b) => {
      const venueOrder = String(a.venue || '').localeCompare(String(b.venue || ''), undefined, { sensitivity: 'base' });
      if (venueOrder !== 0) return venueOrder;
      return new Date(a.screenings[0]?.startAt || a.startAt) - new Date(b.screenings[0]?.startAt || b.startAt);
    });
  } else {
    result.sort((a, b) => new Date(a.screenings[0]?.startAt || a.startAt) - new Date(b.screenings[0]?.startAt || b.startAt));
  }
  return result;
}

function calendarItems() {
  const query = state.query.trim().toLowerCase();
  return screenings
    .filter((item) => item.city === state.city)
    .filter((item) => !query || [item.title, item.director, item.venue, item.genre, item.format, item.notes, item.sourceName].some((field) => String(field || '').toLowerCase().includes(query)))
    .sort((a, b) => new Date(a.startAt) - new Date(b.startAt));
}

function calendarDateLabel(dateKey) {
  if (!dateKey) return 'Choose a day';
  const date = new Date(`${dateKey}T12:00:00`);
  if (Number.isNaN(date.getTime())) return dateKey;
  return date.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' });
}

function ensureCalendarState(items) {
  if (!state.calendarSelectedDate) state.calendarSelectedDate = state.selectedDate || todayDateKey();
  if (!state.calendarMonth) {
    const first = state.calendarSelectedDate ? dateFromKey(state.calendarSelectedDate) : (items[0]?.startAt ? new Date(items[0].startAt) : new Date());
    state.calendarMonth = new Date(first.getFullYear(), first.getMonth(), 1);
  }
}

function renderCalendar(calendarItemsForView = calendarItems()) {
  if (!calendarView || !calendarGrid) return;
  const items = calendarItemsForView;
  ensureCalendarState(items);
  const cursor = state.calendarMonth || new Date();
  const year = cursor.getFullYear();
  const month = cursor.getMonth();
  if (calendarTitle) calendarTitle.textContent = cursor.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });

  const byDate = new Map();
  items.forEach((item) => {
    const key = screeningDateKey(item);
    if (!key) return;
    if (!byDate.has(key)) byDate.set(key, []);
    byDate.get(key).push(item);
  });

  const firstDay = new Date(year, month, 1).getDay();
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const cells = [];
  for (let index = 0; index < firstDay; index += 1) {
    const filler = document.createElement('span');
    filler.className = 'calendar-day calendar-day--empty';
    filler.setAttribute('aria-hidden', 'true');
    cells.push(filler);
  }
  for (let day = 1; day <= daysInMonth; day += 1) {
    const key = `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
    const dayItems = byDate.get(key) || [];
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'calendar-day';
    button.dataset.date = key;
    button.setAttribute('role', 'gridcell');
    button.setAttribute('aria-selected', String(state.calendarSelectedDate === key));
    button.classList.toggle('is-selected', state.calendarSelectedDate === key);
    button.classList.toggle('has-screenings', dayItems.length > 0);
    button.setAttribute('aria-label', `${calendarDateLabel(key)}${dayItems.length ? `, ${dayItems.length} screening${dayItems.length === 1 ? '' : 's'}` : ''}`);
    const number = document.createElement('strong');
    number.className = 'calendar-day-number';
    number.textContent = String(day);
    button.append(number);
    if (dayItems.length) {
      const count = document.createElement('span');
      count.className = 'calendar-day-count';
      count.textContent = `${dayItems.length} show${dayItems.length === 1 ? '' : 's'}`;
      button.append(count);
      const titles = document.createElement('span');
      titles.className = 'calendar-day-titles';
      titles.textContent = groupScreenings(dayItems).slice(0, 2).map((item) => item.title).join(' · ');
      button.append(titles);
    } else {
      const empty = document.createElement('span');
      empty.className = 'calendar-day-count';
      empty.textContent = '—';
      button.append(empty);
    }
    cells.push(button);
  }
  calendarGrid.replaceChildren(...cells);

  const selectedItems = state.calendarSelectedDate ? items.filter((item) => screeningDateKey(item) === state.calendarSelectedDate) : [];
  const selectedGroups = groupScreenings(selectedItems);
  if (!calendarSelection || !calendarScreeningGrid) return;
  calendarSelection.replaceChildren();
  const selectionLabel = document.createElement('span');
  selectionLabel.className = 'eyebrow';
  selectionLabel.textContent = selectedItems.length ? `${selectedGroups.length} film${selectedGroups.length === 1 ? '' : 's'} · ${selectedItems.length} show${selectedItems.length === 1 ? '' : 's'} on` : 'No screenings on';
  const selectionDate = document.createElement('strong');
  selectionDate.textContent = calendarDateLabel(state.calendarSelectedDate);
  calendarSelection.append(selectionLabel, selectionDate);
  calendarScreeningGrid.replaceChildren(...selectedGroups.map(cardFor));
  calendarScreeningGrid.hidden = selectedItems.length === 0;
}

function matchingScreenings() {
  const query = state.query.trim().toLowerCase();
  return screenings
    .filter((item) => item.city === state.city)
    .filter((item) => state.view !== 'browse' || matchesDateFilter(item))
    .filter((item) => state.view !== 'saved' || state.saved.has(item.id))
    .filter((item) => !query || [item.title, item.director, item.venue, item.genre, item.format, item.notes, item.sourceName].some((field) => String(field || '').toLowerCase().includes(query)))
    .sort((a, b) => {
      const aValue = a.startAt ? new Date(a.startAt).getTime() : timeValue(a.time);
      const bValue = b.startAt ? new Date(b.startAt).getTime() : timeValue(b.time);
      const safeA = Number.isNaN(aValue) ? timeValue(a.time) : aValue;
      const safeB = Number.isNaN(bValue) ? timeValue(b.time) : bValue;
      return safeA - safeB;
    });
}

function updateSavedCounts() {
  document.querySelectorAll('.saved-count').forEach((count) => { count.textContent = state.saved.size; });
}

function cardFor(item) {
  const showings = item.screenings || [item];
  const primary = showings[0] || item;
  const fragment = template.content.cloneNode(true);
  const card = fragment.querySelector('.screening-card');
  card.dataset.id = primary.id;
  const poster = fragment.querySelector('.card-poster');
  poster.style.setProperty('--poster-color', primary.color || '#a92f23');
  const posterImage = fragment.querySelector('.poster-image');
  if (primary.image) {
    posterImage.src = imageUrl(primary.image);
    posterImage.alt = primary.imageAlt || `${primary.title} film still`;
    posterImage.addEventListener('error', () => posterImage.remove());
  } else {
    posterImage.remove();
  }
  fragment.querySelector('.poster-kicker').textContent = `${primary.year || '—'} / ${primary.genre || 'Repertory'}`;
  const titleText = primary.title || 'Untitled screening';
  const posterTitle = fragment.querySelector('.poster-title');
  posterTitle.textContent = titleText;
  posterTitle.dataset.id = primary.id;
  posterTitle.setAttribute('aria-label', `View details for ${titleText}`);
  fragment.querySelector('.poster-format').textContent = primary.format || 'Format not listed';
  const filmTitle = fragment.querySelector('.film-title');
  filmTitle.textContent = titleText;
  filmTitle.dataset.id = primary.id;
  filmTitle.setAttribute('aria-label', `View details for ${titleText}`);
  fragment.querySelector('.film-meta').textContent = `${primary.director || 'Director not listed'} · ${primary.year || '—'}`;
  fragment.querySelector('.distance').textContent = primary.neighborhood || primary.distance || 'Los Angeles';
  fragment.querySelector('.showing-date').textContent = primary.date || 'Date TBA';
  const showingTime = fragment.querySelector('.showing-time');
  showingTime.replaceChildren(...showings.map((showing) => {
    const time = document.createElement('a');
    time.className = 'showtime-option';
    time.href = showing.ticketUrl || showing.sourceUrl || '#';
    time.target = '_blank';
    time.rel = 'noopener noreferrer';
    time.textContent = showing.time || 'Time TBA';
    time.title = `${showing.sourceStatus === 'partner-fallback' ? 'Open event listing' : 'Open official tickets'} for ${showing.time || 'this showtime'}`;
    return time;
  }));
  fragment.querySelector('.showing-venue').textContent = primary.venue || 'Venue TBA';
  const sourceLabel = fragment.querySelector('.source-label');
  if (sourceLabel) {
    sourceLabel.textContent = primary.sourceLabel || (primary.sourceName ? `Official · ${primary.sourceName}` : 'Source not listed');
    sourceLabel.title = `${primary.sourceUrl || ''}${primary.sourceLastChecked ? ` · checked ${primary.sourceLastChecked}` : ''}`;
    sourceLabel.href = primary.sourceUrl || primary.ticketUrl || '#';
  }
  const saved = showings.some((showing) => state.saved.has(showing.id));
  fragment.querySelectorAll('.save-button, .save-screening-button').forEach((saveButton) => {
    saveButton.dataset.id = primary.id;
    saveButton.classList.toggle('saved', saved);
    saveButton.setAttribute('aria-pressed', String(saved));
    saveButton.setAttribute('aria-label', saved ? `Remove ${primary.title} from saved screenings` : `Save ${primary.title} screening`);
    const icon = saveButton.querySelector('.save-text-icon') || saveButton.querySelector('span');
    if (icon) icon.textContent = saved ? '♥' : '♡';
    const label = saveButton.querySelector('.save-text-label');
    if (label) label.textContent = saved ? 'Saved' : 'Save';
  });
  const savedActions = fragment.querySelector('.saved-actions');
  if (savedActions) {
    savedActions.hidden = state.view !== 'saved';
    savedActions.querySelectorAll('button').forEach((button) => { button.dataset.id = primary.id; });
  }
  const inlineSave = fragment.querySelector('.save-screening-button');
  if (inlineSave) inlineSave.hidden = state.view === 'saved';
  const detailsButton = fragment.querySelector('.details-button');
  detailsButton.dataset.id = primary.id;
  const ticketLink = fragment.querySelector('.ticket-link');
  ticketLink.href = primary.ticketUrl || primary.sourceUrl || '#';
  ticketLink.setAttribute('aria-label', `Open official listing for ${primary.title} at ${primary.venue}`);
  return fragment;
}

function render() {
  const items = matchingScreenings();
  const groupedItems = groupScreenings(items);
  const calendarMode = state.view === 'calendar';
  updateDayNavigator();
  if (grid) {
    grid.replaceChildren(...(calendarMode ? [] : groupedItems.map(cardFor)));
    grid.hidden = calendarMode || items.length === 0;
  }
  if (calendarView) calendarView.hidden = !calendarMode;
  if (emptyState) emptyState.hidden = calendarMode || items.length !== 0;
  if (!calendarMode && state.view === 'browse') {
    if (emptyHeading) emptyHeading.textContent = `No screenings on ${dayLabel?.textContent || 'this day'}.`;
    if (emptyMessage) emptyMessage.textContent = state.dateFilter === 'upcoming' ? 'The live index has no upcoming screenings yet.' : 'Try another date, filter, venue, or search term.';
  } else if (!calendarMode) {
    if (emptyHeading) emptyHeading.textContent = 'No saved screenings found.';
    if (emptyMessage) emptyMessage.textContent = 'Use Save on any screening card to build your list.';
  }
  if (resultCount) resultCount.textContent = `${groupedItems.length} film${groupedItems.length === 1 ? '' : 's'} · ${items.length} show${items.length === 1 ? '' : 's'} · ${cityLabel(state.city)}`;
  if (citySelect) citySelect.value = state.city;
  updateSavedCounts();
  if (calendarMode) renderCalendar(items);
  document.querySelectorAll('[data-view]').forEach((button) => button.classList.toggle('active', button.dataset.view === state.view));
}

function selectDateFilter(filter) {
  const today = dateFromKey(todayDateKey());
  state.dateFilter = filter;
  if (filter === 'tomorrow') today.setDate(today.getDate() + 1);
  if (filter === 'weekend') today.setDate(today.getDate() + (today.getDay() === 0 ? 0 : 6 - today.getDay()));
  state.selectedDate = dateKeyFromDate(today);
  state.calendarSelectedDate = state.selectedDate;
  state.calendarMonth = new Date(today.getFullYear(), today.getMonth(), 1);
  state.view = 'browse';
  render();
  loadScreenings();
}

function toggleSaved(id) {
  const item = screenings.find((screening) => screening.id === id);
  if (!item) return;
  if (state.saved.has(id)) {
    state.saved.delete(id);
    showToast(`${item.title} removed from saved`);
  } else {
    state.saved.add(id);
    showToast(`${item.title} saved`);
  }
  if (storage) storage.setItem('rerun-saved', JSON.stringify([...state.saved]));
  render();
}

function openDetails(id) {
  const item = screenings.find((screening) => screening.id === id);
  if (!item) return;
  const relatedShowings = screenings
    .filter((screening) => screening.sourceName === item.sourceName && screening.venue === item.venue && screeningDateKey(screening) === screeningDateKey(item) && String(screening.title || '').trim().toLowerCase() === String(item.title || '').trim().toLowerCase())
    .sort((a, b) => new Date(a.startAt) - new Date(b.startAt));
  const whenLabel = relatedShowings.length > 1 ? relatedShowings.map((showing) => showing.time).join(' · ') : (item.time || 'Time TBA');
  history.replaceState(null, '', `${location.pathname}?screening=${encodeURIComponent(id)}`);
  const image = item.image ? `<img class="dialog-poster-image" src="${escapeHtml(imageUrl(item.image))}" alt="${escapeHtml(item.imageAlt || `${item.title} film still`)}">` : '';
  const notesRow = item.notes ? `<div class="detail-row"><span>Program note</span><strong>${escapeHtml(item.notes)}</strong></div>` : '';
  const addressRow = item.address ? `<div class="detail-row"><span>Address</span><strong>${escapeHtml(item.address)}</strong></div>` : '';
  const checkedRow = item.sourceLastChecked ? `<div class="detail-row"><span>Source check</span><strong>${escapeHtml(item.sourceLastChecked)}</strong></div>` : '';
  const sourceName = item.sourceName || 'Official source';
  const sourceUrl = item.sourceUrl || item.ticketUrl || '#';
  const sourceActionLabel = item.sourceStatus === 'partner-fallback' ? 'Open partner listing' : `Verify on ${sourceName}`;
  const ticketActionLabel = item.sourceStatus === 'partner-fallback' ? 'Open event listing' : 'Open official tickets';
  dialogContent.innerHTML = `<div class="dialog-body"><div class="dialog-poster" style="--poster-color:${escapeHtml(item.color || '#a92f23')}">${image}<span class="eyebrow">${escapeHtml(item.year || '—')} / ${escapeHtml(item.genre || 'Repertory')}</span><strong>${escapeHtml(item.title || 'Untitled screening')}</strong><span class="eyebrow">${escapeHtml(item.format || 'Format not listed')}</span></div><div class="dialog-info"><span class="eyebrow">Screening details</span><h2>${escapeHtml(item.title || 'Untitled screening')}</h2><p>${escapeHtml(item.synopsis || 'Check the official program page for the latest details.')}</p><div class="detail-row"><span>When</span><strong>${escapeHtml(item.date || 'Date TBA')} · ${escapeHtml(whenLabel)}</strong></div><div class="detail-row"><span>Where</span><strong>${escapeHtml(item.venue || 'Venue TBA')} · ${escapeHtml(item.neighborhood || item.distance || 'Los Angeles')}</strong></div>${addressRow}<div class="detail-row"><span>Director</span><strong>${escapeHtml(item.director || 'Not listed')}</strong></div>${notesRow}${checkedRow}<a class="dialog-ticket" href="${escapeHtml(item.ticketUrl || sourceUrl)}" target="_blank" rel="noopener noreferrer">${ticketActionLabel} ↗</a><a class="dialog-source" href="${escapeHtml(sourceUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(sourceActionLabel)} ↗</a><div class="dialog-actions"><button class="dialog-calendar" type="button" data-id="${escapeHtml(item.id)}">Add to calendar ↓</button><button class="dialog-share" type="button" data-id="${escapeHtml(item.id)}">Share screening ↗</button></div></div></div>`;
  const dialogImage = dialogContent.querySelector('.dialog-poster-image');
  if (dialogImage) dialogImage.addEventListener('error', () => dialogImage.remove());
  dialog.showModal();
}

function calendarValue(date) {
  return date.toISOString().replace(/[-:]/g, '').replace(/\.\d{3}/, '');
}

function downloadCalendar(id) {
  const item = screenings.find((screening) => screening.id === id);
  if (!item) return;
  const parsedStart = item.startAt ? new Date(item.startAt) : new Date();
  const start = Number.isNaN(parsedStart.getTime()) ? new Date() : parsedStart;
  if (!item.startAt && item.dateGroup === 'weekend') {
    const daysUntilSaturday = (6 - start.getDay() + 7) % 7;
    start.setDate(start.getDate() + daysUntilSaturday);
  }
  if (item.time) {
    const [clock, meridiem] = item.time.split(' ');
    let [hours, minutes] = clock.split(':').map(Number);
    if (meridiem === 'PM' && hours !== 12) hours += 12;
    if (meridiem === 'AM' && hours === 12) hours = 0;
    start.setHours(hours, minutes, 0, 0);
  }
  const end = new Date(start.getTime() + 2 * 60 * 60 * 1000);
  const ics = ['BEGIN:VCALENDAR','VERSION:2.0','PRODID:-//Rerun//Screening//EN','BEGIN:VEVENT',`UID:${item.id}@rerun.app`,`DTSTAMP:${calendarValue(new Date())}`,`DTSTART:${calendarValue(start)}`,`DTEND:${calendarValue(end)}`,`SUMMARY:${item.title} at ${item.venue}`,`LOCATION:${item.venue}`,`DESCRIPTION:${item.synopsis} Tickets: ${item.ticketUrl}`,'END:VEVENT','END:VCALENDAR'].join('\r\n');
  const url = URL.createObjectURL(new Blob([ics], { type:'text/calendar' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = `${item.id}-rerun.ics`;
  link.click();
  URL.revokeObjectURL(url);
  showToast('Calendar file downloaded');
}

async function shareScreening(id) {
  const item = screenings.find((screening) => screening.id === id);
  if (!item) return;
  const shareUrl = new URL(location.href);
  shareUrl.search = `?screening=${encodeURIComponent(id)}`;
  try {
    if (navigator.share) {
      await navigator.share({ title:`${item.title} · Rerun`, text:`${item.title} is playing at ${item.venue}.`, url:shareUrl.href });
    } else if (navigator.clipboard) {
      await navigator.clipboard.writeText(shareUrl.href);
      showToast('Screening link copied');
    } else {
      showToast('Copy the URL to share this screening');
    }
  } catch (error) {
    if (error.name !== 'AbortError') showToast('Could not share this screening');
  }
}

let toastTimer;
let retryTimer = null;
function showToast(message) {
  toast.textContent = message;
  toast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('show'), 2400);
}

function formatCheckedAt(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const minutes = Math.max(0, Math.round((Date.now() - date.getTime()) / 60000));
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

function feedStatus(meta = {}) {
  const sourceCount = meta.sourceCount || meta.sources?.length || 0;
  const requiredCount = meta.requiredSourceCount || meta.sources?.filter((source) => source.required !== false).length || sourceCount;
  const optionalUnavailable = (meta.sources || [])
    .filter((source) => source.required === false && source.status !== 'verified')
    .map((source) => source.name)
    .filter(Boolean);
  const fallbackSources = (meta.sources || [])
    .filter((source) => String(source.sourceMode || '').includes('fallback'))
    .map((source) => source.name)
    .filter(Boolean);
  const sourceNotes = [
    optionalUnavailable.length ? `${optionalUnavailable.join(', ')} unavailable` : '',
    fallbackSources.length ? `${fallbackSources.join(', ')} fallback` : '',
  ].filter(Boolean);
  const optionalNote = sourceNotes.length ? ` · ${sourceNotes.join(' · ')}` : '';
  const checked = formatCheckedAt(meta.lastRefresh);
  if (meta.refreshStatus === 'ok' && !meta.stale) return `Verified feed · ${requiredCount} required sources${optionalNote}${checked ? ` · ${checked}` : ''}`;
  if (meta.refreshStatus === 'degraded') return `Partial feed · ${sourceCount} sources${checked ? ` · ${checked}` : ''}`;
  if (meta.refreshStatus === 'error') return `Official feed unavailable · last verified ${checked || 'unknown'}`;
  return `Official feed indexed · ${sourceCount || 4} sources`;
}

function incompleteFeedStatus(payload = {}) {
  const sources = payload.sources || payload.meta?.sources || [];
  const refreshStatus = payload.meta?.refreshStatus || payload.meta?.status || '';
  const initialIndex = ['seed', 'unknown'].includes(String(refreshStatus).toLowerCase())
    || sources.some((source) => ['seed', 'unknown'].includes(String(source.status || '').toLowerCase()));
  if (initialIndex) return 'Preparing the initial live index…';
  const waitingSources = sources.filter((source) => source.required !== false && source.status !== 'verified');
  const waiting = waitingSources.map((source) => source.name).filter(Boolean);
  const firstError = waitingSources.flatMap((source) => source.errors || []).find(Boolean);
  const reason = firstError ? ` · ${String(firstError).replace(/\s+/g, ' ').slice(0, 120)}` : '';
  return waiting.length
    ? `Waiting for complete live index · ${waiting.join(', ')}${reason}`
    : 'Waiting for complete live index · official sources are still being checked';
}

function scheduleIndexRetry(requestId, retryAfterSeconds = 30) {
  window.clearTimeout(retryTimer);
  const seconds = Number(retryAfterSeconds);
  const delay = Number.isFinite(seconds) ? Math.max(5000, Math.min(900000, seconds * 1000)) : 30000;
  retryTimer = window.setTimeout(() => {
    retryTimer = null;
    if (requestId === apiRequestId) loadScreenings();
  }, delay);
}

async function loadScreenings(forceRefresh = false) {
  if (!canUseApi) {
    setLoadingState('Waiting for complete live index · open with the Python server');
    return false;
  }
  window.clearTimeout(retryTimer);
  retryTimer = null;
  const requestId = ++apiRequestId;
  setRefreshBusy(true);
  setLoadingState('Loading the latest verified index…');
  try {
    const response = await fetch(screeningsApiPath(), { headers: { Accept: 'application/json' } });
    const payload = await response.json().catch(() => ({}));
    if (requestId !== apiRequestId) return;
    if (!response.ok) {
      setDataStatus(incompleteFeedStatus(payload), 'fallback');
      scheduleIndexRetry(requestId, payload.retryAfterSeconds || payload.meta?.retryAfterSeconds || 30);
      return false;
    }
    window.clearTimeout(retryTimer);
    retryTimer = null;
    screenings = Array.isArray(payload.data) ? payload.data : [];
    setDataStatus(feedStatus(payload.meta), payload.meta?.stale ? 'fallback' : 'default');
    render();
    return true;
  } catch (error) {
    if (requestId !== apiRequestId) return;
    setDataStatus('Waiting for complete live index · reconnecting…', 'fallback');
    scheduleIndexRetry(requestId, 30);
    return false;
  } finally {
    if (requestId === apiRequestId) setRefreshBusy(false);
  }
}

document.addEventListener('click', (event) => {
  const dateFilter = event.target.closest('.date-filter');
  if (dateFilter) {
    selectDateFilter(dateFilter.dataset.dateFilter);
    return;
  }
  const viewButton = event.target.closest('[data-view]');
  if (viewButton) {
    state.view = viewButton.dataset.view;
    loadScreenings();
    if (mobileNav) { mobileNav.hidden = true; menuButton?.setAttribute('aria-expanded', 'false'); }
    if ((state.view === 'saved' || state.view === 'calendar') && document.querySelector('#screenings')) document.querySelector('#screenings').scrollIntoView({ behavior:'smooth' });
    return;
  }
  const titleButton = event.target.closest('.title-action');
  if (titleButton) {
    openDetails(titleButton.dataset.id);
    return;
  }
  const calendarDay = event.target.closest('.calendar-day[data-date]');
  if (calendarDay) {
    state.calendarSelectedDate = calendarDay.dataset.date;
    state.selectedDate = calendarDay.dataset.date;
    if (!isCalendarPage) state.view = 'browse';
    loadScreenings();
    if (!isCalendarPage) {
      document.querySelector('#screenings')?.scrollIntoView({ behavior: 'smooth' });
    }
    return;
  }
  const saveButton = event.target.closest('.save-button, .save-screening-button');
  if (saveButton) { toggleSaved(saveButton.dataset.id); return; }
  const detailsButton = event.target.closest('.details-button');
  if (detailsButton) { openDetails(detailsButton.dataset.id); return; }
  const calendarButton = event.target.closest('.dialog-calendar');
  if (calendarButton) { downloadCalendar(calendarButton.dataset.id); return; }
  const shareButton = event.target.closest('.dialog-share');
  if (shareButton) { shareScreening(shareButton.dataset.id); return; }
  const calendarCardButton = event.target.closest('.calendar-card-button');
  if (calendarCardButton) { downloadCalendar(calendarCardButton.dataset.id); return; }
  const shareCardButton = event.target.closest('.share-card-button');
  if (shareCardButton) { shareScreening(shareCardButton.dataset.id); return; }
  const removeSavedButton = event.target.closest('.remove-saved-button');
  if (removeSavedButton) { toggleSaved(removeSavedButton.dataset.id); return; }
  if (event.target.closest('.dialog-close')) dialog?.close();
  if (event.target.closest('#clear-filters')) {
    state.query = ''; if (searchInput) searchInput.value = '';
    render();
  }
});

if (citySelect) citySelect.addEventListener('change', (event) => { state.city = event.target.value; state.view = isCalendarPage ? 'calendar' : 'browse'; state.selectedDate = todayDateKey(); state.calendarMonth = null; state.calendarSelectedDate = state.selectedDate; loadScreenings(); });
if (searchInput) searchInput.addEventListener('input', (event) => { state.query = event.target.value; render(); });
if (sortSelect) sortSelect.addEventListener('change', (event) => { state.sortMode = event.target.value === 'venue' ? 'venue' : 'time'; render(); });
if (dayPrev) dayPrev.addEventListener('click', () => shiftSelectedDate(-1));
if (dayNext) dayNext.addEventListener('click', () => shiftSelectedDate(1));
if (calendarPrev) calendarPrev.addEventListener('click', () => {
  const cursor = state.calendarMonth || new Date();
  state.calendarMonth = new Date(cursor.getFullYear(), cursor.getMonth() - 1, 1);
  state.calendarSelectedDate = '';
  render();
});
if (calendarNext) calendarNext.addEventListener('click', () => {
  const cursor = state.calendarMonth || new Date();
  state.calendarMonth = new Date(cursor.getFullYear(), cursor.getMonth() + 1, 1);
  state.calendarSelectedDate = '';
  render();
});
document.querySelector('.dialog-close')?.addEventListener('click', () => dialog?.close());
dialog?.addEventListener('close', () => history.replaceState(null, '', location.pathname));
dialog?.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });
if (menuButton && mobileNav) menuButton.addEventListener('click', () => {
  const open = menuButton.getAttribute('aria-expanded') !== 'true';
  menuButton.setAttribute('aria-expanded', String(open));
  mobileNav.hidden = !open;
});
document.querySelector('#newsletter-form')?.addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const message = document.querySelector('#form-message');
  const email = form.querySelector('input[type="email"]').value;
  message.textContent = 'Saving your alert…';
  try {
    const response = await fetch(apiUrl('/api/notify'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ email })
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || 'Could not save alert.');
    message.textContent = payload.message || 'You’re on the list — see you at the movies.';
    form.reset();
  } catch (error) {
    message.textContent = canUseApi ? 'The alert service is offline — try again in a moment.' : 'Preview mode: start the Rerun API to save alerts.';
  }
});

loadScreenings();

const initialScreening = new URLSearchParams(location.search).get('screening');
if (initialScreening) openDetails(initialScreening);

if ('serviceWorker' in navigator && location.protocol !== 'file:') {
  window.addEventListener('load', () => navigator.serviceWorker.register('/service-worker.js?v=20260909-planning-filters'));
}
