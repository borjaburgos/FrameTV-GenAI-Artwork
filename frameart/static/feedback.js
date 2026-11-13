(function() {
  const controllers = new Map();
  let services;
  let summary = null;

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, (char) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[char]));
  }

  function summaryMarkup() {
    if (!summary?.total_count) {
      return '<p class="feedback-learning">Each reaction helps shape your household’s Art Profile. You can start before taking the quiz.</p>';
    }
    const known = summary.classified_count || 0;
    const themes = (summary.preferred_tags || []).slice(0, 4).map((item) =>
      esc(item.tag.replaceAll('_', ' '))
    ).join(' · ');
    return '<div class="feedback-learning"><strong>' + esc(summary.good_count || 0) +
      ' good · ' + esc(summary.bad_count || 0) + ' bad</strong><span>' +
      (themes ? 'Your reactions lean toward ' + themes + '.' : 'Your artwork reactions are saved for future selections.') +
      '</span>' + (known < summary.total_count ? '<span class="hint">Some pictures have no style or artist details yet. Their individual ratings are saved too.</span>' : '') + '</div>';
  }

  function historyMarkup(open, busy) {
    const ratings = (summary?.ratings || []).slice(0, 6);
    if (!ratings.length) return '';
    return '<details class="feedback-history"' + (open ? ' open' : '') + '><summary>Your recent ratings</summary><div>' +
      ratings.map((rating) => {
        const title = rating.artwork?.title || 'Previously rated artwork';
        return '<div class="feedback-history-item"><div><strong>' + esc(title) + '</strong><span>' +
          (rating.value === 'good' ? 'Good' : 'Bad') + '</span></div><div class="feedback-history-actions">' +
          [['good', 'Good'], ['bad', 'Bad'], ['clear', 'Clear']].map(([value, label]) =>
            '<button class="btn btn-ghost btn-small' + (rating.value === value ? ' feedback-selected' : '') +
            '" data-feedback-history-vote="' + value + '" data-feedback-key="' + esc(rating.artwork_key) +
            '" aria-label="' + esc(label + ' rating for ' + title) + '"' +
            (value === 'clear' ? '' : ' aria-pressed="' + (rating.value === value) + '"') +
            (busy ? ' disabled' : '') + '>' + label + '</button>'
          ).join('') + '</div></div>';
      }).join('') + '</div></details>';
  }

  function render(controller) {
    const {root, tvs, current, busy, error} = controller;
    const historyOpen = root.querySelector('.feedback-history')?.open;
    const artwork = current?.status === 'showing' ? current.artwork : null;
    const selectId = root.id + '-tv';
    let display = '<p class="feedback-empty">Choose a TV, then check the picture currently showing.</p>';
    if (current && !artwork) {
      display = '<p class="feedback-empty" role="status">' + esc(current.message || {
        watching: 'This TV is being used. Nothing will interrupt normal viewing.',
        offline: 'This TV is offline. Try checking again when it is available.',
        unknown: 'The TV could not confirm which artwork is showing. Try again.',
      }[current.status] || 'No artwork is available to rate.') + '</p>';
    } else if (artwork) {
      const image = artwork.thumbnail_url
        ? '<img class="feedback-preview" src="' + esc(artwork.thumbnail_url) + '" alt="' + esc(artwork.title || 'Current artwork') + '" decoding="async">'
        : '<div class="feedback-preview feedback-preview-empty">Preview unavailable</div>';
      display = '<div class="feedback-current">' + image + '<div class="feedback-artwork"><span class="page-eyebrow">Now showing · ' +
        esc(current.tv_name || controller.selectedTv) + '</span><h4>' + esc(artwork.title || 'Current artwork') + '</h4>' +
        (artwork.artist ? '<p>' + esc(artwork.artist) + '</p>' : '') +
        '<div class="feedback-votes" role="group" aria-label="Rate current artwork">' +
        '<button class="btn btn-secondary' + (current.rating === 'good' ? ' feedback-selected' : '') +
        '" data-feedback-vote="good" aria-pressed="' + (current.rating === 'good') + '"' + (busy ? ' disabled' : '') +
        '><span aria-hidden="true">👍</span> Good</button>' +
        '<button class="btn btn-secondary' + (current.rating === 'bad' ? ' feedback-selected' : '') +
        '" data-feedback-vote="bad" aria-pressed="' + (current.rating === 'bad') + '"' + (busy ? ' disabled' : '') +
        '><span aria-hidden="true">👎</span> Bad</button>' +
        '<button class="btn btn-ghost btn-small" data-feedback-clear' + (busy || !current.rating ? ' disabled' : '') + '>Clear rating</button></div>' +
        '<p class="feedback-vote-state" role="status">' + (busy === 'rating' ? 'Saving your reaction…' :
          (current.rating ? 'Saved: ' + esc(current.rating) + '. Change your mind anytime.' : 'How does this picture feel in your room?')) +
        '</p><p class="hint">Rating only: the picture stays on the TV.</p></div></div>';
    }
    root.innerHTML = '<section class="feedback-card" aria-labelledby="' + root.id + '-heading">' +
      '<div class="feedback-heading"><div><div class="page-eyebrow">Your taste, over time</div><h3 id="' + root.id +
      '-heading">Rate what’s on your Frame</h3><p>One tap for a picture you enjoy, or one you’d rather see less of.</p></div></div>' +
      (tvs.length ? '<div class="feedback-controls"><div><label for="' + selectId + '">TV to rate</label><select id="' + selectId + '"' +
        (busy ? ' disabled' : '') + '>' + tvs.map((tv) => '<option value="' + esc(tv.profile_id) + '"' +
          (tv.profile_id === controller.selectedTv ? ' selected' : '') + '>' + esc(tv.name || tv.profile_id) + '</option>').join('') +
        '</select></div><button class="btn btn-secondary" data-feedback-refresh' + (busy ? ' disabled' : '') + '>' +
        (busy === 'checking' ? 'Checking…' : (current ? 'Refresh current picture' : 'Check current picture')) + '</button></div>' :
        '<p class="feedback-empty">Add a TV in Settings to rate its current picture.</p>') +
      (error ? '<p class="feedback-error error-text" role="alert">' + esc(error) + '</p>' : '') +
      (tvs.length ? display : '') + summaryMarkup() + historyMarkup(historyOpen, busy) + '</section>';
    const select = root.querySelector('select');
    select?.addEventListener('change', () => {
      controller.selectedTv = select.value;
      controller.current = null;
      controller.error = '';
      localStorage.setItem('frameart.feedback.tv', select.value);
      render(controller);
    });
    root.querySelector('[data-feedback-refresh]')?.addEventListener('click', () => checkCurrent(controller));
    root.querySelectorAll('[data-feedback-vote]').forEach((button) => {
      button.addEventListener('click', () => {
        const value = button.dataset.feedbackVote;
        rate(controller, controller.current.rating === value ? null : value);
      });
    });
    root.querySelector('[data-feedback-clear]')?.addEventListener('click', () => rate(controller, null));
    root.querySelectorAll('[data-feedback-history-vote]').forEach((button) => {
      button.addEventListener('click', () => {
        const rating = summary.ratings.find((item) => item.artwork_key === button.dataset.feedbackKey);
        if (!rating) return;
        const vote = button.dataset.feedbackHistoryVote;
        rate(controller, vote === 'clear' ? null : vote, rating);
      });
    });
    root.querySelector('img.feedback-preview')?.addEventListener('error', (event) => {
      const fallback = document.createElement('div');
      fallback.className = 'feedback-preview feedback-preview-empty';
      fallback.textContent = 'Preview unavailable';
      event.target.replaceWith(fallback);
    }, {once: true});
  }

  async function checkCurrent(controller) {
    if (controller.busy || !controller.selectedTv) return;
    controller.busy = 'checking';
    controller.error = '';
    controller.current = null;
    render(controller);
    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), 60000);
    try {
      const response = await services.apiFetch('/curation/now-showing?tv_profile_id=' + encodeURIComponent(controller.selectedTv), {signal: abort.signal});
      controller.current = await services.parseJSONResponse(response, 'Could not check the current picture.');
    } catch (error) {
      controller.error = error.name === 'AbortError'
        ? 'The TV took too long to respond. Try checking again.'
        : (error.message || 'Could not check the current picture. Try again.');
    } finally {
      clearTimeout(timeout);
      controller.busy = '';
      render(controller);
    }
  }

  async function rate(controller, value, historyRating = null) {
    const reference = historyRating ? {artwork_key: historyRating.artwork_key} : controller.current?.artwork?.reference;
    if (controller.busy || !reference) return;
    controller.busy = 'rating';
    controller.error = '';
    const currentKey = historyRating?.artwork_key || controller.current.artwork.key;
    render(controller);
    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), 30000);
    try {
      const response = await services.apiFetch('/curation/ratings', {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({reference, value}), signal: abort.signal,
      });
      const result = await services.parseJSONResponse(response, 'Could not save your reaction.');
      summary = result.summary;
      controllers.forEach((item) => {
        if (item.current?.artwork?.key === currentKey) item.current.rating = result.rating;
      });
      window.dispatchEvent(new CustomEvent('frameart:feedback-updated', {detail: result}));
    } catch (error) {
      controller.error = error.name === 'AbortError'
        ? 'Saving took too long. You can retry this rating safely.'
        : (error.message || 'Could not save your reaction. Try again.');
    } finally {
      clearTimeout(timeout);
      controller.busy = '';
      controllers.forEach((item) => { if (item.initialized) render(item); });
      const focusTarget = historyRating
        ? [...controller.root.querySelectorAll('[data-feedback-key]')].find((button) =>
          button.dataset.feedbackKey === currentKey && button.dataset.feedbackHistoryVote === (value || 'clear')
        ) || controller.root.querySelector('[data-feedback-refresh]')
        : controller.root.querySelector('[data-feedback-vote="' + (value || 'good') + '"]');
      focusTarget?.focus({preventScroll: true});
    }
  }

  window.FrameArtFeedback = {
    async open(dependencies, rootId) {
      services = dependencies;
      const root = document.getElementById(rootId);
      if (!root) return;
      let controller = controllers.get(rootId);
      if (controller?.initialized) {
        render(controller);
        return;
      }
      if (controller?.loading) return;
      controller = {root, tvs: [], selectedTv: '', current: null, busy: '', error: '', initialized: false, loading: true};
      controllers.set(rootId, controller);
      root.innerHTML = '<p class="page-intro" role="status">Loading artwork ratings…</p>';
      try {
        const [tvsResponse, ratingsResponse] = await Promise.all([
          services.apiFetch('/settings/tvs'), services.apiFetch('/curation/ratings'),
        ]);
        const tvSettings = await services.parseJSONResponse(tvsResponse, 'Could not load configured TVs.');
        const ratings = await services.parseJSONResponse(ratingsResponse, 'Could not load artwork ratings.');
        controller.tvs = tvSettings.tvs || [];
        const remembered = localStorage.getItem('frameart.feedback.tv');
        controller.selectedTv = controller.tvs.some((tv) => tv.profile_id === remembered)
          ? remembered : (controller.tvs[0]?.profile_id || '');
        summary = ratings.summary;
        controller.initialized = true;
        render(controller);
      } catch (error) {
        root.innerHTML = '<p class="error-text" role="alert">' + esc(error.message) + '</p>' +
          '<button class="btn btn-secondary" data-feedback-retry>Try loading ratings again</button>';
        root.querySelector('[data-feedback-retry]').addEventListener('click', () => this.open(dependencies, rootId));
      } finally {
        controller.loading = false;
      }
    },
  };
})();
