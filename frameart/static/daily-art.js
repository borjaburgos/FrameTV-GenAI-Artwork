(function() {
  const root = document.getElementById('daily-art-root');
  if (!root) return;
  const field = (name) => document.getElementById('daily-art-' + name);
  let services;
  let snapshot;
  let catalog = [];
  let targets = [];
  let loading = false;
  let mutating = false;

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, (character) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[character]));
  }

  function setError(message = '') {
    field('error').textContent = message;
    field('error').hidden = !message;
  }

  function thumbnail(item) {
    try {
      const url = new URL(item?.thumbnail_url, window.location.origin);
      return url.origin === window.location.origin && /^\/art-catalog\/[^/]+\/thumbnail$/.test(url.pathname)
        ? url.href : null;
    } catch (_) {
      return null;
    }
  }

  function familyAlbumHolds() {
    return (snapshot?.holds || []).filter((hold) => hold.reason === 'family-album');
  }

  function manualHolds() {
    return (snapshot?.holds || []).filter((hold) => hold.reason !== 'family-album');
  }

  function familyPriorityText() {
    const waiting = familyAlbumHolds().filter((hold) => hold.pending);
    const displayed = familyAlbumHolds().filter((hold) => !hold.pending);
    const names = (holds) => holds.map((hold) => hold.tv || hold.tv_profile_id || 'the album TV').join(', ');
    return (displayed.length ? ' New family-album photo takes priority until midnight on ' + names(displayed) +
      '. Daily Art resumes at the next local midnight.' : '') +
      (waiting.length ? ' Family photo waiting for Art Mode on ' + names(waiting) +
        '; Daily Art waits until it is shown, then the following midnight.' : '');
  }

  function preview(day, label) {
    const item = catalog.find((entry) => entry.id === day?.artwork_id);
    const image = thumbnail(item);
    return '<article class="daily-art-preview"><div class="daily-art-preview-image">' +
      (image ? '<img loading="lazy" decoding="async" src="' + esc(image) + '" alt="' + esc(item.title) + '">' :
        '<span>' + (day?.artwork_id ? 'Preview not cached' : 'No artwork assigned') + '</span>') + '</div><div><div class="page-eyebrow">' + label +
      (day?.date ? ' · ' + esc(day.date) : '') + '</div><h3>' + esc(item?.title || day?.artwork_id || 'No artwork scheduled') +
      '</h3><p>' + esc(item?.artist?.name || '') + '</p><p class="daily-art-day-status">' + esc(day?.status || 'Waiting for a calendar') + '</p></div></article>';
  }

  function render() {
    const settings = snapshot.settings || snapshot;
    field('enabled').checked = Boolean(settings.enabled);
    field('timezone').value = settings.timezone || 'America/New_York';
    field('time').value = settings.change_time || '00:00';
    field('target').innerHTML = '<option value="">Choose a TV or group…</option>' + targets.map((target, index) =>
      '<option value="' + index + '">' + esc(target.label) + '</option>').join('');
    const selectedIndex = targets.findIndex((target) => target.target_type === settings.target_type && target.target_id === settings.target_id);
    field('target').value = selectedIndex >= 0 ? String(selectedIndex) : '';
    const hold = settings.hold_until;
    field('status').textContent = (settings.enabled ? 'Daily Art is enabled.' : 'Daily Art is paused.') +
      (hold ? ' Held until ' + new Date(hold).toLocaleString() + '.' : '') +
      (manualHolds().length ? ' ' + manualHolds().length + ' TV(s) held after a manual selection.' : '') +
      familyPriorityText() +
      (snapshot.pending ? ' A safe update is waiting for Art Mode.' : '');
    field('previews').innerHTML = preview(snapshot.today, 'Today') + preview(snapshot.tomorrow, 'Tomorrow');
    field('previews').querySelectorAll('img').forEach((img) => img.addEventListener('error', () => {
      img.replaceWith(Object.assign(document.createElement('span'), {textContent: 'Preview unavailable'}));
    }, {once: true}));
    const years = snapshot.calendar_years || [];
    field('coverage').textContent = years.length > 1
      ? years.join(' + ') + ' calendars · ' + (snapshot.total_calendar_count || 0) + ' total dates assigned' +
        (snapshot.calendar_year ? ' · ' + snapshot.calendar_year + ': ' + (snapshot.calendar_count || 0) + ' dates' : '')
      : snapshot.calendar_year
        ? snapshot.calendar_year + ' calendar · ' + (snapshot.calendar_count || 0) + ' dates assigned' +
          (snapshot.coverage?.missing_dates?.length ? ' · ' + snapshot.coverage.missing_dates.length + ' dates missing' : '')
        : 'No calendar loaded. Import a calendar to assign artwork to dates.';
    field('results').innerHTML = (snapshot.results || []).map((result) => '<li>' +
      esc(result.tv || result.tv_profile_id || 'TV') + ': ' + esc(result.detail || result.status || '') + '</li>').join('');
    field('pause').disabled = !settings.enabled;
    field('resume').disabled = !hold && !manualHolds().length;
  }

  async function load() {
    if (!services || loading) return;
    loading = true;
    field('refresh').disabled = true;
    field('refresh').textContent = 'Loading…';
    root.setAttribute('aria-busy', 'true');
    setError();
    try {
      const responses = await Promise.all([
        services.apiFetch('/modes/daily-art'), services.apiFetch('/art-catalog'),
        services.apiFetch('/settings/tvs'), services.apiFetch('/automation/groups'),
      ]);
      const [daily, artwork, tvData, groups] = await Promise.all(responses.map((response) =>
        services.parseJSONResponse(response, 'Could not load Daily Art settings.')));
      snapshot = daily;
      catalog = artwork.items || [];
      targets = [
        ...(tvData.tvs || tvData || []).map((tv) => ({target_type: 'tv', target_id: tv.profile_id || tv.id || tv.name,
          label: 'TV · ' + (tv.profile_id || tv.name || tv.id)})),
        ...(groups || []).map((group) => ({target_type: 'group', target_id: group.id, label: 'Group · ' + group.name})),
      ];
      render();
    } catch (error) {
      setError(error.message || 'Could not load Daily Art. Try refreshing.');
    } finally {
      loading = false;
      field('refresh').disabled = false;
      field('refresh').textContent = 'Refresh Daily Art';
      root.setAttribute('aria-busy', 'false');
    }
  }

  function settingsPayload() {
    const target = targets[Number(field('target').value)];
    if (field('enabled').checked && field('target').value === '') throw new Error('Choose a TV or TV group before enabling Daily Art.');
    return {
      enabled: field('enabled').checked,
      target_type: field('target').value === '' ? 'tv' : target.target_type,
      target_id: field('target').value === '' ? null : target.target_id,
      timezone: field('timezone').value.trim(), change_time: field('time').value,
    };
  }

  async function update(button, changes) {
    if (mutating || !snapshot) return;
    mutating = true;
    const label = button.textContent;
    button.disabled = true;
    button.textContent = 'Saving…';
    setError();
    try {
      const response = await services.apiFetch('/modes/daily-art', {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({...settingsPayload(), ...changes}),
      });
      snapshot = await services.parseJSONResponse(response, 'Could not save Daily Art settings.');
      render();
    } catch (error) {
      setError(error.message || 'Could not save Daily Art.');
    } finally {
      mutating = false;
      button.disabled = false;
      button.textContent = label;
      const settings = snapshot.settings || snapshot;
      if (button === field('pause')) button.disabled = !settings.enabled;
      if (button === field('resume')) button.disabled = !settings.hold_until && !manualHolds().length;
    }
  }

  function tomorrowMidnight(timezone) {
    const formatter = new Intl.DateTimeFormat('en-CA', {
      timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
    });
    const parts = (date) => Object.fromEntries(formatter.formatToParts(date).map((part) => [part.type, part.value]));
    const now = parts(new Date());
    const desired = Date.UTC(Number(now.year), Number(now.month) - 1, Number(now.day) + 1);
    let candidate = desired;
    for (let index = 0; index < 3; index += 1) {
      const actual = parts(new Date(candidate));
      const local = Date.UTC(Number(actual.year), Number(actual.month) - 1, Number(actual.day), Number(actual.hour), Number(actual.minute), Number(actual.second));
      candidate += desired - local;
    }
    return new Date(candidate).toISOString();
  }

  field('form').addEventListener('submit', (event) => {
    event.preventDefault();
    update(field('save'), {});
  });
  field('pause').addEventListener('click', () => update(field('pause'), {enabled: false}));
  field('hold').addEventListener('click', () => {
    try {
      update(field('hold'), {hold_until: tomorrowMidnight(field('timezone').value.trim())});
    } catch (_) {
      setError('Enter a valid time zone before setting a hold.');
    }
  });
  field('resume').addEventListener('click', () => update(field('resume'), {hold_until: null, clear_holds: true}));
  field('refresh').addEventListener('click', load);
  field('import').addEventListener('click', async () => {
    if (mutating) return;
    const file = field('calendar').files[0];
    if (!file) { setError('Choose a calendar JSON file first.'); return; }
    if (file.size > 512 * 1024) { setError('The calendar file must be 512 KiB or smaller.'); return; }
    mutating = true;
    const button = field('import');
    button.disabled = true;
    button.textContent = 'Importing…';
    setError();
    try {
      const calendar = JSON.parse(await file.text());
      const response = await services.apiFetch('/modes/daily-art/calendar', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(calendar),
      });
      await services.parseJSONResponse(response, 'Could not import the artwork calendar.');
      field('calendar').value = '';
      await load();
      services.showToast('Artwork calendar imported. Rotation settings were preserved.', 'done');
    } catch (error) {
      setError(error instanceof SyntaxError ? 'This file is not valid JSON.' : error.message || 'Could not import the calendar.');
    } finally {
      mutating = false;
      button.disabled = false;
      button.textContent = 'Import calendar';
    }
  });

  window.FrameArtDailyArt = {open(dependencies) { services = dependencies; return load(); }};
})();
