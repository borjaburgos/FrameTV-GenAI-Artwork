(function() {
  const root = document.getElementById('art-catalog-root');
  if (!root) return;
  const refresh = document.getElementById('art-catalog-refresh');
  const status = document.getElementById('art-catalog-status');
  const errorBox = document.getElementById('art-catalog-error');
  const search = document.getElementById('art-catalog-search');
  const grid = document.getElementById('art-catalog-grid');
  const empty = document.getElementById('art-catalog-empty');
  const dialog = document.getElementById('art-catalog-dialog');
  const detail = document.getElementById('art-catalog-detail');
  let services;
  let snapshot = null;
  let loading = false;

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, (char) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[char]));
  }

  function safeLink(value) {
    if (!value) return null;
    try {
      const url = new URL(value);
      return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
    } catch (_) {
      return null;
    }
  }

  function thumbnailURL(value) {
    if (!value) return null;
    try {
      const url = new URL(value, window.location.origin);
      return url.origin === window.location.origin &&
        /^\/art-catalog\/[^/]+\/thumbnail$/.test(url.pathname) ? url.href : null;
    } catch (_) {
      return null;
    }
  }

  function creator(item) {
    return item.artist?.name || 'Creator not recorded';
  }

  function image(item, lazy = true) {
    const src = thumbnailURL(item.thumbnail_url);
    return src ? '<img class="art-catalog-image" src="' + esc(src) + '" alt="' +
      esc(item.title + ' by ' + creator(item)) + '" decoding="async"' +
      (lazy ? ' loading="lazy"' : '') + '>' :
      '<span class="art-catalog-image art-catalog-no-image">Preview not cached</span>';
  }

  function wireImageFallbacks(container) {
    container.querySelectorAll('.art-catalog-image[src]').forEach((img) => {
      img.addEventListener('error', () => {
        const fallback = document.createElement('span');
        fallback.className = 'art-catalog-image art-catalog-no-image';
        fallback.textContent = 'Preview unavailable';
        img.replaceWith(fallback);
      }, {once: true});
    });
  }

  function renderCards() {
    const items = snapshot?.items || [];
    const query = search.value.trim().toLocaleLowerCase();
    const filtered = items.filter((item) => [
      item.title, creator(item), item.date, item.medium, item.movement, ...(item.tags || []),
    ].join(' ').toLocaleLowerCase().includes(query));
    document.getElementById('art-catalog-tools').hidden = !items.length;
    document.getElementById('art-catalog-count').textContent = filtered.length + ' of ' + items.length + ' artworks';
    grid.innerHTML = filtered.map((item) => '<article class="art-catalog-card"><button type="button" data-art-catalog-id="' +
      esc(item.id) + '" aria-label="Artwork details: ' + esc(item.title) + '">' + image(item) +
      '<span class="art-catalog-caption"><strong>' + esc(item.title) + '</strong><span>' +
      esc(creator(item)) + (item.date ? ' · ' + esc(item.date) : '') + '</span>' +
      (item.ai_generated ? '<span class="art-catalog-ai">AI-created original</span>' : '') +
      '<span>View artwork &amp; artist details →</span></span></button></article>').join('');
    empty.hidden = filtered.length > 0;
    if (items.length && !filtered.length) empty.textContent = 'No artwork matches this search. Try another artist, title or tag.';
    else if (snapshot?.configured === false) {
      empty.textContent = 'ResourceSpace is not connected yet. Configure FRAMEART_RESOURCESPACE_URL, FRAMEART_RESOURCESPACE_USER, FRAMEART_RESOURCESPACE_API_KEY and FRAMEART_RESOURCESPACE_COLLECTION on the FrameArt server, then reopen Library. The pilot setup guide explains the read-only NAS configuration.';
    } else empty.textContent = 'No artwork has been cached yet. Refresh metadata after the 20-artwork pilot collection is ready in ResourceSpace.';
    wireImageFallbacks(grid);
  }

  function lastSynced(value) {
    if (!value) return '';
    const date = new Date(typeof value === 'number' ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleString();
  }

  function renderStatus() {
    const state = snapshot?.status || 'empty';
    status.dataset.state = state;
    if (snapshot?.configured === false) status.textContent = 'Not connected · Originals are not being scanned or changed.';
    else if (state === 'stale') status.textContent = 'ResourceSpace unavailable · Showing the last successfully cached catalog.';
    else if ((snapshot?.items || []).length) status.textContent = 'Local catalog · ' + snapshot.items.length + ' artworks · Read-only ResourceSpace integration';
    else status.textContent = 'ResourceSpace configured · Waiting for the first successful metadata refresh.';
    const when = lastSynced(snapshot?.last_synced_at);
    if (when) status.textContent += ' · Last refreshed ' + when;
    errorBox.hidden = !snapshot?.error;
    errorBox.textContent = snapshot?.error || '';
    renderCards();
  }

  function valueText(value) {
    if (value === null || value === undefined || value === '') return 'Not recorded';
    if (Array.isArray(value)) return value.length ? value.join(', ') : 'Not recorded';
    return typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value);
  }

  function fact(label, value) {
    return '<dt>' + esc(label) + '</dt><dd>' + esc(valueText(value)) + '</dd>';
  }

  function link(label, value) {
    const url = safeLink(value);
    return url ? '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + esc(label) + ' ↗</a>' : '';
  }

  function showDetails(item) {
    const artist = item.artist || {};
    const lifespan = [artist.birth_year, artist.death_year].filter((value) => value !== null && value !== undefined && value !== '').join('–');
    detail.innerHTML = '<h2 id="art-catalog-detail-title">' + esc(item.title) + '</h2><p class="art-catalog-detail-artist">' +
      esc(creator(item)) + (item.date ? ' · ' + esc(item.date) : '') + '</p>' +
      (item.ai_generated ? '<p class="art-catalog-ai">AI-created original</p>' : '') + image(item, false) +
      '<dl class="art-catalog-facts">' + fact('Artwork date', item.date) + fact('Medium', item.medium) +
      fact('Movement / style', item.movement) + fact('About the artwork', item.description) +
      (item.institution ? fact('Collection / source', item.institution) : '') +
      (item.accession_number ? fact('Accession number', item.accession_number) : '') +
      (item.work_dimensions ? fact('Original artwork dimensions', item.work_dimensions) : '') +
      fact('Rights / license', item.license) +
      (item.source_publication_date ? fact('Image publication date', item.source_publication_date) : '') +
      fact('TV image dimensions', item.width && item.height ? item.width + ' × ' + item.height + ' px' : null) +
      (item.crop_notes ? fact('TV crop', item.crop_notes) : '') +
      fact('Tags', item.tags) + '</dl><div class="art-catalog-links">' + link('Artwork source', item.source_url) +
      link('License details', item.license_url) + '</div>' +
      '<h3>' + (item.ai_generated ? 'Creator & provenance' : 'About the artist') + '</h3>' +
      '<dl class="art-catalog-facts">' + fact('Creator', artist.name) +
      (lifespan ? fact('Lifespan', lifespan) : '') +
      (artist.nationality ? fact('Nationality', artist.nationality) : '') + '</dl>' +
      '<p class="art-catalog-bio">' + esc(artist.bio || (item.ai_generated ?
        'This work is labeled as AI-created; it is not attributed to a historical or living artist.' :
        'An artist biography has not been added yet.')) + '</p>' +
      (item.ai_generated ? '<dl class="art-catalog-facts">' + fact('AI provenance', item.ai_provenance) + '</dl>' : '') +
      '<div class="art-catalog-links">' + link('Artist reference', artist.source_url) + '</div>';
    wireImageFallbacks(detail);
    dialog.showModal();
  }

  async function load(sync = false) {
    if (!services || loading) return;
    loading = true;
    refresh.disabled = true;
    refresh.textContent = sync ? 'Refreshing…' : 'Loading…';
    root.setAttribute('aria-busy', 'true');
    errorBox.hidden = true;
    try {
      const response = await services.apiFetch(sync ? '/art-catalog/sync' : '/art-catalog',
        sync ? {method: 'POST'} : {});
      const result = await services.parseJSONResponse(response, 'Could not load the artwork catalog.');
      if (!result || !Array.isArray(result.items)) throw new Error('The artwork catalog response was invalid.');
      snapshot = result;
      renderStatus();
    } catch (error) {
      if (snapshot?.items?.length) {
        status.dataset.state = 'stale';
        status.textContent = 'ResourceSpace unavailable · Your cached artwork is still available.';
        const when = lastSynced(snapshot.last_synced_at);
        if (when) status.textContent += ' · Last refreshed ' + when;
      } else {
        status.dataset.state = 'stale';
        status.textContent = 'The artwork catalog could not be refreshed. Try again when the server is available.';
      }
      errorBox.hidden = false;
      errorBox.textContent = error.message || 'Could not refresh metadata.';
    } finally {
      loading = false;
      refresh.disabled = snapshot?.configured === false;
      refresh.textContent = 'Refresh metadata';
      root.setAttribute('aria-busy', 'false');
    }
  }

  search.addEventListener('input', renderCards);
  refresh.addEventListener('click', () => load(true));
  grid.addEventListener('click', (event) => {
    const button = event.target.closest('[data-art-catalog-id]');
    const item = snapshot?.items?.find((entry) => entry.id === button?.dataset.artCatalogId);
    if (item) showDetails(item);
  });
  document.getElementById('art-catalog-close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', (event) => {
    if (event.target !== dialog) return;
    const bounds = dialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right ||
        event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
  });

  window.FrameArtArtCatalog = {
    open(dependencies) {
      services = dependencies;
      return load();
    },
  };
})();
