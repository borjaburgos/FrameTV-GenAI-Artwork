(function() {
  const root = document.getElementById('art-profile-root');
  const emptyHousehold = () => ({
    name: '', location: '', roots: '', kids: '', room_palette: '', favorite_subjects: '',
    avoid_subjects: '', meaningful_dates: '', notes: '', seasonal: 'yes',
    adventure: 'balanced', fit: 'preserve',
  });
  let services;
  let catalog;
  let profile;
  let mode = 'intro';
  let step = 0;
  let loading = false;
  let saving = Promise.resolve();
  let saveTimer;
  let revision = 0;
  let saveError = '';
  let savePending = false;

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, (char) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[char]));
  }

  function freshProfile() {
    return {
      schema_version: 1, status: 'draft', step: 0, choices: {},
      household: {...emptyHousehold(), timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || ''},
    };
  }

  function answeredCount() {
    return Object.values(profile.choices).filter((choice) => choice !== 'skip').length;
  }

  function saveLabel(message, error = false) {
    const label = root.querySelector('#art-profile-save-state');
    if (label) {
      label.textContent = message;
      label.classList.toggle('error-text', error);
    }
    const retry = root.querySelector('#art-profile-retry-save');
    if (retry) retry.hidden = !error;
  }

  function save(status = 'draft') {
    clearTimeout(saveTimer);
    saveTimer = null;
    profile.status = status;
    profile.step = step;
    const currentRevision = ++revision;
    savePending = true;
    const payload = JSON.stringify({
      schema_version: 1, status, step,
      choices: profile.choices, household: profile.household,
    });
    saveLabel('Saving…');
    const operation = saving.catch(() => {}).then(async () => {
      const response = await services.apiFetch('/curation/profile', {
        method: 'PUT', headers: {'Content-Type': 'application/json'}, body: payload,
      });
      const saved = await services.parseJSONResponse(response, 'Could not save your Art Profile.');
      if (currentRevision === revision) {
        profile.created_at = saved.created_at;
        profile.quiz_version = saved.quiz_version;
        profile.derived = saved.derived;
        profile.feedback = saved.feedback;
        profile.quiz_derived = saved.quiz_derived;
        profile.updated_at = saved.updated_at;
        profile.completed_at = saved.completed_at;
        saveError = '';
        savePending = false;
        saveLabel('Saved on this FrameArt server');
      }
      return saved;
    });
    saving = operation;
    operation.catch((error) => {
      if (currentRevision !== revision) return;
      saveError = error.message || 'Could not save.';
      savePending = false;
      saveLabel('Not saved. ' + saveError, true);
    });
    return operation;
  }

  function queueSave() {
    saveLabel('Unsaved changes…');
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => save().catch(() => {}), 450);
  }

  function shell(content, subtitle) {
    root.innerHTML = '<div class="curation-heading"><div><div class="page-eyebrow">Curate · Art Profile</div>' +
      '<h2 tabindex="-1" id="art-profile-heading">' + esc(subtitle) + '</h2></div>' +
      '<span class="curation-local">Your household · Saved locally</span></div>' +
      content + '<div class="curation-save"><span id="art-profile-save-state" role="status">' +
      esc(savePending ? 'Saving…' : (saveError ? 'Not saved. ' + saveError : (profile.updated_at ? 'Saved on this FrameArt server' : 'Your answers stay on this FrameArt server'))) +
      '</span><button class="btn btn-ghost btn-small" id="art-profile-retry-save"' +
      (saveError ? '' : ' hidden') + '>Retry save</button></div>';
    root.querySelector('#art-profile-retry-save').addEventListener('click', () => {
      save(profile.status).catch(() => {});
    });
  }

  function artworkCard(artwork, side, selected) {
    return '<div class="curation-artwork"><button type="button" class="curation-choice' +
      (selected ? ' selected' : '') + '" data-art-choice="' + side + '" aria-pressed="' +
      selected + '" aria-label="Choose ' + side + ': ' + esc(artwork.title) + '">' +
      '<span class="curation-image-wrap"><img src="' + esc(artwork.image_url) + '" alt="' +
      esc(artwork.title + ' by ' + artwork.artist) + '" decoding="async">' +
      '<span class="curation-image-fallback" hidden>Image unavailable. You can skip this pairing.</span></span>' +
      '<span class="curation-art-caption"><strong>' + esc(artwork.title) + '</strong><span>' +
      esc(artwork.artist) + (artwork.date ? ' · ' + esc(artwork.date) : '') +
      '</span><span class="curation-choice-hint">' + (selected ? '✓ Selected' : 'Choose this artwork') +
      '</span></span></button><a class="curation-credit" href="' + esc(artwork.source_url) +
      '" target="_blank" rel="noopener noreferrer">Museum source ↗</a><span class="curation-license">' +
      esc(artwork.license || 'Public domain') + '</span></div>';
  }

  function renderIntro() {
    const count = Object.keys(profile.choices).length;
    shell('<div class="curation-intro"><div class="curation-intro-copy"><p class="curation-lede">A year of art starts with what feels like home.</p>' +
      '<p>Pick between ' + catalog.pairs.length + ' pairs of real artworks, then share a little about your family and the room. There are no right answers: choose both, neither, or skip whenever you like.</p>' +
      '<div class="curation-facts"><span>About 3–5 minutes</span><span>Pause and return anytime</span><span>Public-domain artwork</span></div>' +
      '<button class="btn" id="art-profile-start">' + (count ? 'Continue your quiz' : 'Build my Art Profile') +
      '</button><p class="hint">After you finish, we’ll use your profile to curate 20–30 artworks for your review before building the full year.</p></div>' +
      '<div class="curation-intro-preview">' + catalog.pairs.slice(0, 2).flatMap((pair) => [pair.left, pair.right]).map((art) =>
        '<img src="' + esc(art.image_url) + '" alt="' + esc(art.title) + '" loading="lazy">'
      ).join('') + '</div></div>', 'Art for your everyday');
    root.querySelector('#art-profile-start').addEventListener('click', () => {
      step = Math.min(profile.step || 0, catalog.pairs.length);
      mode = step < catalog.pairs.length ? 'quiz' : 'household';
      render(true);
    });
  }

  function choose(choice) {
    const pair = catalog.pairs[step];
    profile.choices[pair.id] = choice;
    save().catch(() => {});
    renderQuiz();
    root.querySelector('[data-art-choice="' + choice + '"]')?.focus({preventScroll: true});
  }

  function renderQuiz() {
    const pair = catalog.pairs[step];
    const selected = profile.choices[pair.id];
    shell('<div class="curation-progress"><span>Pair ' + (step + 1) + ' of ' + catalog.pairs.length +
      '</span><progress aria-label="Quiz progress" max="' + catalog.pairs.length + '" value="' + step + '"></progress></div>' +
      '<p class="curation-prompt">' + esc(pair.prompt || pair.title || 'Which would you rather live with?') + '</p>' +
      '<div class="curation-pair">' + artworkCard(pair.left, 'left', selected === 'left') +
      artworkCard(pair.right, 'right', selected === 'right') + '</div>' +
      '<div class="curation-alternatives">' + [['both', 'I like both'], ['neither', 'Neither feels right'], ['skip', 'Skip this pair']].map(([value, label]) =>
        '<button class="btn btn-secondary' + (selected === value ? ' selected' : '') +
        '" data-art-choice="' + value + '" aria-pressed="' + (selected === value) + '">' + label + '</button>'
      ).join('') + '</div><div class="curation-navigation"><button class="btn btn-ghost" id="art-profile-back">' +
      (step ? '← Previous pair' : '← Introduction') + '</button><span class="curation-keyboard">Keyboard: ← / →, B for both, N for neither, S to skip</span>' +
      '<button class="btn" id="art-profile-next"' + (selected ? '' : ' disabled') + '>' +
      (step + 1 === catalog.pairs.length ? 'About your home →' : 'Next pair →') + '</button></div>', 'What would you enjoy living with?');
    root.querySelectorAll('[data-art-choice]').forEach((button) => {
      button.addEventListener('click', () => choose(button.dataset.artChoice));
    });
    root.querySelectorAll('.curation-choice img').forEach((img) => {
      img.addEventListener('error', () => {
        img.hidden = true;
        img.nextElementSibling.hidden = false;
      });
    });
    root.querySelector('#art-profile-back').addEventListener('click', () => {
      if (step) step -= 1;
      else mode = 'intro';
      render(true);
    });
    root.querySelector('#art-profile-next').addEventListener('click', () => {
      step += 1;
      if (step >= catalog.pairs.length) mode = 'household';
      save().catch(() => {});
      render(true);
    });
  }

  const textFields = [
    ['name', 'Household name', 'Optional, e.g. Our family', 100],
    ['location', 'Where do you live?', 'City or region, and hemisphere for the seasons', 1000],
    ['roots', 'Where do you come from?', 'Places, cultures, or family roots that matter to you', 1000],
    ['kids', 'What do the kids enjoy?', 'Approximate ages, interests, or things they love', 1000],
    ['room_palette', 'Tell us about the family room', 'Wall colors, furniture, light, and the mood you want', 1000],
    ['favorite_subjects', 'What should we look for?', 'Favorite places, hobbies, artists, animals, or subjects', 1000],
    ['avoid_subjects', 'What should we avoid?', 'Subjects, colors, or imagery you do not want in the room', 1000],
    ['meaningful_dates', 'Any meaningful dates or traditions?', 'Birthdays, holidays, anniversaries, or family traditions; dates are optional', 1000],
  ];

  function selectField(name, label, options) {
    return '<div><label for="art-profile-' + name + '">' + label + '</label><select id="art-profile-' + name +
      '" name="' + name + '">' + options.map(([value, text]) => '<option value="' + value + '"' +
      (profile.household[name] === value ? ' selected' : '') + '>' + text + '</option>').join('') + '</select></div>';
  }

  function renderHousehold() {
    const household = profile.household;
    shell('<p class="page-intro">A few details help us choose art for your family room. Every field is optional. Broad descriptions are enough.</p>' +
      '<form id="art-profile-household" autocomplete="off" data-1p-ignore="true"><div class="curation-context-grid">' +
      textFields.map(([name, label, placeholder, max]) => '<div><label for="art-profile-' + name + '">' + label +
        '</label><textarea id="art-profile-' + name + '" name="' + name + '" maxlength="' + max +
        '" rows="2" placeholder="' + placeholder + '" data-1p-ignore="true">' + esc(household[name]) + '</textarea></div>').join('') +
      '</div><div class="curation-context-grid curation-settings">' +
      selectField('adventure', 'How adventurous should the collection be?', [
        ['familiar', 'Mostly familiar and easy to live with'], ['balanced', 'A balance of comfort and discovery'], ['adventurous', 'Surprise us; explore widely'],
      ]) + selectField('seasonal', 'Follow the seasons and calendar?', [
        ['yes', 'Yes, weave in the seasons and special days'], ['some', 'A little; keep it subtle'], ['no', 'No, keep the selection timeless'],
      ]) + selectField('fit', 'How should artwork fit the Frame TV?', [
        ['preserve', 'Preserve the whole artwork with a border if needed'], ['crop', 'Allow a considered crop to fill the screen'],
      ]) + '</div><label for="art-profile-notes">Anything else we should know?</label><textarea name="notes" id="art-profile-notes" maxlength="1000" rows="2" data-1p-ignore="true">' +
      esc(household.notes) + '</textarea><p class="hint">Artworks will be prepared for your Frame’s 16:9, 3840 × 2160 display without stretching the image.</p>' +
      (answeredCount() < 6 ? '<p class="curation-validation" role="status">Choose left, right, both, or neither in at least 6 pairs before finishing. You have answered ' + answeredCount() + '.</p>' : '') +
      '<div class="curation-navigation"><button type="button" class="btn btn-ghost" id="art-profile-back">← Back to artwork</button>' +
      '<button class="btn" type="submit" id="art-profile-finish"' + (answeredCount() < 6 ? ' disabled' : '') + '>Save my Art Profile</button></div></form>', 'A little about your home');
    const form = root.querySelector('#art-profile-household');
    form.addEventListener('input', (event) => {
      profile.household[event.target.name] = event.target.value;
      queueSave();
    });
    form.addEventListener('change', (event) => {
      profile.household[event.target.name] = event.target.value;
      queueSave();
    });
    root.querySelector('#art-profile-back').addEventListener('click', () => {
      step = catalog.pairs.length - 1;
      mode = 'quiz';
      save().catch(() => {});
      render(true);
    });
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const finish = root.querySelector('#art-profile-finish');
      finish.disabled = true;
      finish.textContent = 'Saving…';
      try {
        await save('complete');
        mode = 'summary';
        render(true);
      } catch (error) {
        services.showToast(error.message || 'Could not save your profile.', 'error');
        finish.disabled = false;
        finish.textContent = 'Save my Art Profile';
      }
    });
  }

  function renderSummary() {
    const derived = profile.derived || {};
    const preferred = derived.preferred_tags || [];
    const avoided = derived.avoided_tags || [];
    const feedback = profile.feedback || {};
    shell('<div class="curation-result"><div class="curation-result-mark" aria-hidden="true">✓</div><p class="curation-lede">Your first chapter is ready.</p>' +
      '<p>Your Art Profile is saved. The next step is a handpicked collection of 20–30 artworks for your review, guided by these choices and your family’s story.</p>' +
      '<p class="hint">The review collection will be curated after your answers are reviewed. No daily rotation has been scheduled.</p></div>' +
      (feedback.total_count ? '<section class="curation-summary-card curation-feedback-summary"><h3>Learning from your daily ratings</h3><p>' +
        esc(feedback.good_count || 0) + ' good · ' + esc(feedback.bad_count || 0) +
        ' bad. Your artwork reactions now contribute alongside the quiz choices below.</p></section>' : '') +
      '<div class="curation-summary-grid"><section class="curation-summary-card"><h3>What you’re drawn to</h3><div class="curation-tags">' +
      (preferred.length ? preferred.slice(0, 12).map((item) => '<span>' + esc(item.tag.replaceAll('_', ' ')) + '</span>').join('') : '<p>Your household notes will guide the first selection.</p>') +
      '</div>' + (avoided.length ? '<h4>Use less of</h4><p>' + avoided.slice(0, 8).map((item) => esc(item.tag.replaceAll('_', ' '))).join(' · ') + '</p>' : '') +
      '<p class="hint">These are starting signals from your quiz and artwork ratings. Individual reactions do not rule out an entire style.</p></section>' +
      '<section class="curation-summary-card"><h3>Your curation brief</h3><ul>' +
      (derived.summary || []).map((line) => '<li>' + esc(line) + '</li>').join('') + '</ul></section></div>' +
      '<section class="curation-summary-card"><h3>About your home</h3><dl class="curation-home-summary">' +
      textFields.filter(([name]) => profile.household[name]).map(([name, label]) => '<div><dt>' + label + '</dt><dd>' + esc(profile.household[name]) + '</dd></div>').join('') +
      (profile.household.notes ? '<div><dt>Other notes</dt><dd>' + esc(profile.household.notes) + '</dd></div>' : '') +
      '</dl><p class="hint">' + answeredCount() + ' visual pairs answered. You can change your choices anytime.</p></section>' +
      '<div class="curation-navigation"><button class="btn btn-secondary" id="art-profile-edit">Refine artwork choices</button>' +
      '<button class="btn btn-secondary" id="art-profile-edit-home">Edit home details</button>' +
      '<button class="btn btn-secondary" id="art-profile-export">Download Art Profile</button><button class="btn btn-ghost" id="art-profile-reset">Start over</button></div>', 'Your household’s Art Profile');
    root.querySelector('#art-profile-edit').addEventListener('click', () => {
      step = 0;
      mode = 'quiz';
      render(true);
    });
    root.querySelector('#art-profile-edit-home').addEventListener('click', () => {
      step = catalog.pairs.length;
      mode = 'household';
      render(true);
    });
    root.querySelector('#art-profile-export').addEventListener('click', async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        if (savePending || saveTimer) await save(profile.status);
        const response = await services.apiFetch('/curation/profile');
        const latest = await services.parseJSONResponse(response, 'Could not load your latest Art Profile.');
        const blob = new Blob([JSON.stringify(latest || profile, null, 2)], {type: 'application/json'});
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = 'frameart-art-profile.json';
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } catch (error) {
        services.showToast(error.message || 'Could not export your Art Profile.', 'error');
      } finally {
        button.disabled = false;
      }
    });
    root.querySelector('#art-profile-reset').addEventListener('click', async () => {
      if (!window.confirm('Clear your quiz answers and home details? Your artwork ratings will be kept.')) return;
      profile = freshProfile();
      step = 0;
      mode = 'intro';
      try {
        await save();
        render(true);
      } catch (error) {
        services.showToast(error.message || 'Could not reset the profile.', 'error');
      }
    });
  }

  function render(focus = false) {
    if (mode === 'summary') renderSummary();
    else if (mode === 'household') renderHousehold();
    else if (mode === 'quiz') renderQuiz();
    else renderIntro();
    if (focus) root.querySelector('#art-profile-heading')?.focus({preventScroll: true});
  }

  document.addEventListener('keydown', (event) => {
    if (mode !== 'quiz' || !root.closest('.panel.active')) return;
    if (event.target.closest('.art-feedback-root')) return;
    if (event.ctrlKey || event.metaKey || event.altKey || event.target.closest('input, textarea, select')) return;
    const choice = {ArrowLeft: 'left', ArrowRight: 'right', b: 'both', n: 'neither', s: 'skip'}[event.key];
    if (choice) {
      event.preventDefault();
      choose(choice);
    }
  });

  window.addEventListener('frameart:feedback-updated', async () => {
    if (!services || !profile) return;
    try {
      await saving.catch(() => {});
      const response = await services.apiFetch('/curation/profile');
      const latest = await services.parseJSONResponse(response, 'Could not refresh your Art Profile.');
      if (!latest) return;
      profile.feedback = latest.feedback;
      profile.derived = latest.derived;
      profile.quiz_derived = latest.quiz_derived;
      if (mode === 'summary' && !savePending) render();
    } catch (error) {
      services.showToast(error.message || 'Your rating is saved. Refresh to update the Art Profile summary.', 'warn');
    }
  });

  window.FrameArtCuration = {
    async open(dependencies) {
      services = dependencies;
      if (catalog || loading) return;
      loading = true;
      root.innerHTML = '<p class="page-intro" role="status">Loading your Art Profile…</p>';
      try {
        const [quizResponse, profileResponse] = await Promise.all([
          services.apiFetch('/curation/quiz'), services.apiFetch('/curation/profile'),
        ]);
        const quiz = await services.parseJSONResponse(quizResponse, 'Could not load the visual quiz.');
        const saved = await services.parseJSONResponse(profileResponse, 'Could not load your Art Profile.');
        if (!Array.isArray(quiz.pairs) || !quiz.pairs.length) throw new Error('The visual quiz has no artwork pairs.');
        catalog = quiz;
        profile = saved ? {...saved, household: {...emptyHousehold(), ...saved.household}} : freshProfile();
        step = Math.min(profile.step || 0, catalog.pairs.length);
        mode = profile.status === 'complete' ? 'summary' : 'intro';
        render();
      } catch (error) {
        root.innerHTML = '<p class="error-text" role="alert">' + esc(error.message) + '</p>' +
          '<button class="btn btn-secondary" id="art-profile-reload">Try again</button>';
        root.querySelector('#art-profile-reload').addEventListener('click', () => this.open(services));
      } finally {
        loading = false;
      }
    },
  };
})();
