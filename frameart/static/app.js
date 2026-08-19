(function() {
  // =========================================================================
  // State: known TVs (merged from configured + discovered)
  // =========================================================================
  // Each entry: { ip, name, source ('config'|'discovered'), model?, frame_tv? }
  let knownTVs = [];
  let selectedTVArtIds = new Set();
  let loadedTVArtById = {};
  let selectedGalleryJobIds = new Set();
  let configuredProviders = [];
  let managedProviderSettings = null;
  let managedTVSettings = [];
  let managedSettingsBackups = [];
  let managedAccessSettings = null;
  let managedCollections = [];
  let loadedGalleryJobs = {};
  let automationGroups = [];
  let automationPlaylists = [];
  let automationSchedules = [];
  let automationWebhooks = [];
  let automationStatus = null;
  let liveScoreTrackers = [];
  let editingProviderName = null;
  let editingTVProfileId = null;
  const generationJobs = new Map();
  let generationPollTimer = null;
  let authPromptPromise = null;
  const storageKeys = {
    page: 'frameart.page',
    createMode: 'frameart.create.mode',
    tvGenerate: 'frameart.tv.generate',
    tvPublic: 'frameart.tv.public',
    tvOwnUpload: 'frameart.tv.own_upload',
    tvEditUpload: 'frameart.tv.edit_upload',
    tvRemix: 'frameart.tv.remix',
    tvUpload: 'frameart.tv.upload',
    tvArt: 'frameart.tv.art',
    providerGenerate: 'frameart.provider.generate',
    modelGenerate: 'frameart.model.generate',
    providerEdit: 'frameart.provider.edit',
    modelEdit: 'frameart.model.edit',
    providerRemix: 'frameart.provider.remix',
    modelRemix: 'frameart.model.remix',
    mattePublic: 'frameart.matte.public',
    matteOwnUpload: 'frameart.matte.own_upload',
    matteEditUpload: 'frameart.matte.edit_upload',
    matteRemix: 'frameart.matte.remix',
    matteUpload: 'frameart.matte.upload',
  };

  function showToast(message, kind) {
    const wrap = document.getElementById('toast-wrap');
    if (!wrap) return;
    const toast = document.createElement('div');
    toast.className = 'toast' + (kind ? ' ' + kind : '');
    toast.textContent = message;
    wrap.appendChild(toast);
    setTimeout(() => toast.remove(), 3600);
  }

  function defaultDeviceName() {
    return localStorage.getItem('frameart.device.name') ||
      navigator.userAgentData?.platform || navigator.platform || 'Browser device';
  }

  async function completePairingFromUrl() {
    const url = new URL(window.location.href);
    const code = url.searchParams.get('pair');
    if (!code) return;
    const suggestedName = defaultDeviceName();
    const deviceName = window.prompt('Name this FrameArt device:', suggestedName);
    if (!deviceName || !deviceName.trim()) {
      showToast('Device pairing was cancelled. Reopen the link to try again.', 'warn');
      return;
    }
    try {
      const response = await window.fetch('/auth/pair', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code, device_name: deviceName.trim() }),
      });
      if (!response.ok) {
        throw new Error(await readApiError(response, 'Pairing failed.'));
      }
      localStorage.setItem('frameart.device.name', deviceName.trim());
      url.searchParams.delete('pair');
      window.history.replaceState({}, '', url.pathname + url.search + url.hash);
      showToast('This device is paired and ready to use.', 'done');
    } catch (error) {
      showToast(error?.message || 'Pairing failed.', 'error');
    }
  }

  const pairingBootstrapPromise = completePairingFromUrl();

  async function establishAuthSession() {
    const credential = window.prompt('Enter a FrameArt token or device pairing code:');
    if (!credential) return false;
    const isPairingCode = /^[A-HJ-NP-Z2-9]{5}-?[A-HJ-NP-Z2-9]{5}$/i.test(credential.trim());
    const endpoint = isPairingCode ? '/auth/pair' : '/auth/session';
    const payload = isPairingCode
      ? { code: credential.trim(), device_name: defaultDeviceName() }
      : { token: credential, remember_device: true, device_name: defaultDeviceName() };
    const response = await window.fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      showToast('Authentication failed. Check the token or pairing code and try again.', 'error');
      return false;
    }
    if (isPairingCode) showToast('This device is paired and ready to use.', 'done');
    return true;
  }

  async function apiFetch(input, init) {
    await pairingBootstrapPromise;
    let response = await window.fetch(input, init);
    if (response.status !== 401) return response;
    if (!authPromptPromise) {
      authPromptPromise = establishAuthSession().finally(() => {
        authPromptPromise = null;
      });
    }
    if (await authPromptPromise) response = await window.fetch(input, init);
    return response;
  }

  async function parseJSONResponse(response, fallbackMessage) {
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = typeof payload?.detail === 'string'
        ? payload.detail
        : (typeof payload?.detail?.message === 'string'
          ? payload.detail.message
          : (typeof payload?.detail?.error === 'string' ? payload.detail.error : null));
      throw new Error(detail || fallbackMessage || ('Server returned ' + response.status));
    }
    return payload;
  }

  function animateStaggeredChildren(container, selector) {
    if (!container) return;
    const nodes = container.querySelectorAll(selector);
    nodes.forEach((node, index) => {
      node.classList.add('reveal-stagger');
      node.style.animationDelay = (index * 22) + 'ms';
    });
  }

  function findProviderOption(name) {
    return configuredProviders.find((p) => p.name === name) || null;
  }

  function refreshModelSelectFor(providerName, preferredModel, modelSelectId) {
    const modelSel = document.getElementById(modelSelectId);
    if (!modelSel) return;
    const provider = findProviderOption(providerName);
    const models = provider?.models || [];
    const defaultLabel = provider?.default_model
      ? ('Provider default (' + provider.default_model + ')')
      : 'Provider default';

    modelSel.innerHTML = '';
    const defaultOpt = document.createElement('option');
    defaultOpt.value = '';
    defaultOpt.textContent = defaultLabel;
    modelSel.appendChild(defaultOpt);

    for (const model of models) {
      const opt = document.createElement('option');
      opt.value = model;
      opt.textContent = model;
      modelSel.appendChild(opt);
    }

    if (preferredModel && [...modelSel.options].some((o) => o.value === preferredModel)) {
      modelSel.value = preferredModel;
    } else {
      modelSel.value = '';
    }
  }

  function refreshModelSelect(providerName, preferredModel) {
    refreshModelSelectFor(providerName, preferredModel, 'model');
  }

  function refreshProviderSelects(payload) {
    configuredProviders = payload?.providers || [];
    const providerSel = document.getElementById('provider');
    const editProviderSel = document.getElementById('edit-provider');
    const remixProviderSel = document.getElementById('remix-provider');
    const rememberedProvider = localStorage.getItem(storageKeys.providerGenerate);
    const rememberedModel = localStorage.getItem(storageKeys.modelGenerate);
    const rememberedEditProvider = localStorage.getItem(storageKeys.providerEdit);
    const rememberedEditModel = localStorage.getItem(storageKeys.modelEdit);
    const rememberedRemixProvider = localStorage.getItem(storageKeys.providerRemix);
    const rememberedRemixModel = localStorage.getItem(storageKeys.modelRemix);
    const fallback = payload?.default_provider || configuredProviders[0]?.name || '';
    const preferredProvider = rememberedProvider || fallback;
    const preferredEditProvider = rememberedEditProvider || fallback;
    const preferredRemixProvider = rememberedRemixProvider || preferredEditProvider;

    providerSel.innerHTML = '';
    if (editProviderSel) editProviderSel.innerHTML = '';
    if (remixProviderSel) remixProviderSel.innerHTML = '';
    for (const provider of configuredProviders) {
      const opt = document.createElement('option');
      opt.value = provider.name;
      opt.textContent = provider.is_default ? (provider.name + ' (default)') : provider.name;
      providerSel.appendChild(opt);
      if (editProviderSel) {
        const editOpt = document.createElement('option');
        editOpt.value = provider.name;
        editOpt.textContent = provider.is_default ? (provider.name + ' (default)') : provider.name;
        editProviderSel.appendChild(editOpt);
      }
      if (remixProviderSel) {
        const remixOpt = document.createElement('option');
        remixOpt.value = provider.name;
        remixOpt.textContent = provider.is_default ? (provider.name + ' (default)') : provider.name;
        remixProviderSel.appendChild(remixOpt);
      }
    }

    if ([...providerSel.options].some((o) => o.value === preferredProvider)) {
      providerSel.value = preferredProvider;
    } else if (providerSel.options.length) {
      providerSel.selectedIndex = 0;
    }

    refreshModelSelect(providerSel.value, rememberedModel || '');

    if (editProviderSel) {
      if ([...editProviderSel.options].some((o) => o.value === preferredEditProvider)) {
        editProviderSel.value = preferredEditProvider;
      } else if (editProviderSel.options.length) {
        editProviderSel.selectedIndex = 0;
      }
      refreshModelSelectFor(editProviderSel.value, rememberedEditModel || '', 'edit-model');
    }
    if (remixProviderSel) {
      if ([...remixProviderSel.options].some((o) => o.value === preferredRemixProvider)) {
        remixProviderSel.value = preferredRemixProvider;
      } else if (remixProviderSel.options.length) {
        remixProviderSel.selectedIndex = 0;
      }
      refreshModelSelectFor(
        remixProviderSel.value,
        rememberedRemixModel || rememberedEditModel || '',
        'remix-model',
      );
    }
    renderSettingsProviders();
  }

  function renderSettingsProviders() {
    const container = document.getElementById('settings-provider-list');
    if (!container) return;
    const providers = managedProviderSettings?.providers || [];
    if (!managedProviderSettings) {
      container.innerHTML = '<div class="settings-item"><strong>Loading...</strong><span></span></div>';
      return;
    }
    if (!providers.length) {
      container.innerHTML = '<div class="settings-item"><strong>No providers configured</strong><span></span></div>';
      return;
    }
    container.innerHTML = providers.map((provider, index) => {
      const model = provider.model || 'Provider default';
      const keyState = provider.has_api_key
        ? ('Key: ' + (provider.api_key_source || 'configured'))
        : 'No API key';
      const defaultBadge = provider.is_default
        ? '<span class="badge badge-frame">Default</span>'
        : '';
      const deleteDisabled = provider.is_default ? ' disabled title="Choose another default first"' : '';
      return '<div class="settings-item">' +
        '<div class="settings-item-main"><strong>' + esc(provider.name) + '</strong>' +
        '<span>' + esc(model) + ' · ' + esc(keyState) + '</span></div>' +
        '<div class="settings-item-actions">' + defaultBadge +
        '<button class="btn btn-ghost btn-small" data-provider-action="test" data-provider-index="' + index + '">Test</button>' +
        '<button class="btn btn-secondary btn-small" data-provider-action="edit" data-provider-index="' + index + '">Edit</button>' +
        '<button class="btn btn-danger btn-small" data-provider-action="delete" data-provider-index="' + index + '"' + deleteDisabled + '>Delete</button>' +
        '</div></div>';
    }).join('');

    const defaultSelect = document.getElementById('settings-default-provider');
    defaultSelect.innerHTML = providers.map((provider) =>
      '<option value="' + esc(provider.name) + '">' + esc(provider.name) + '</option>'
    ).join('');
    defaultSelect.value = managedProviderSettings.default_provider;
    document.getElementById('settings-default-model').value =
      managedProviderSettings.default_model || '';

    const available = managedProviderSettings.available_types || [];
    const configured = new Set(providers.map((provider) => provider.name));
    document.getElementById('btn-settings-add-provider').disabled =
      !available.some((name) => !configured.has(name));
  }

  function setButtonBusy(btn, busyText) {
    if (!btn) return;
    btn.dataset.originalHtml = btn.innerHTML;
    btn.dataset.originalText = btn.textContent;
    btn.disabled = true;
    btn.classList.add('busy');
    btn.setAttribute('aria-busy', 'true');
    const label = busyText || btn.dataset.originalText || 'Working...';
    btn.innerHTML = '<span class="btn-spinner" aria-hidden="true"></span><span>' + esc(label) + '</span>';
  }

  function clearButtonBusy(btn) {
    if (!btn) return;
    btn.disabled = false;
    btn.classList.remove('busy');
    btn.removeAttribute('aria-busy');
    if (btn.dataset.originalHtml) btn.innerHTML = btn.dataset.originalHtml;
  }

  function addTVs(tvs, source) {
    for (const tv of tvs) {
      const existing = knownTVs.find((known) => known.ip === tv.ip);
      if (!existing) {
        knownTVs.push({ ...tv, source });
      } else if (source === 'config') {
        Object.assign(existing, tv, { source });
      }
    }
    refreshTVSelects();
    renderTVList();
  }

  function removeSessionTV(index) {
    const tv = knownTVs[index];
    if (!tv || tv.source === 'config') return;
    knownTVs.splice(index, 1);
    refreshTVSelects();
    renderTVList();
    showToast('Removed ' + tv.name + ' from this session.', 'done');
  }

  function refreshTVSelects() {
    // Update all TV <select> elements throughout the UI
    const selectors = [
      'tv-select',
      'tv-art-select',
      'upload-tv-select',
      'batch-upload-tv-select',
      'remix-tv-select',
      'public-tv-select',
      'own-upload-tv-select',
      'edit-upload-tv-select',
    ];
    for (const id of selectors) {
      const sel = document.getElementById(id);
      if (!sel) continue;
      const prev = sel.value;
      if (id === 'tv-select') {
        sel.innerHTML = '<option value="">Generate only (no TV)</option>';
      } else if (id === 'public-tv-select') {
        sel.innerHTML = '<option value="">Select TV...</option>';
      } else if (id === 'own-upload-tv-select') {
        sel.innerHTML = '<option value="">Select TV...</option>';
      } else if (id === 'edit-upload-tv-select') {
        sel.innerHTML = '<option value="">Edit only (no TV upload)</option>';
      } else if (id === 'remix-tv-select') {
        sel.innerHTML = '<option value="">Create only (save to Gallery)</option>';
      } else {
        sel.innerHTML = '';
      }
      for (const tv of knownTVs) {
        const opt = document.createElement('option');
        opt.value = tv.ip;
        const label = tv.name + ' (' + tv.ip + ')';
        opt.textContent = label;
        sel.appendChild(opt);
      }
      // Restore previous selection if still valid
      if (prev && [...sel.options].some(o => o.value === prev)) {
        sel.value = prev;
      } else {
        const remembered = localStorage.getItem(
          id === 'tv-select' ? storageKeys.tvGenerate :
          id === 'public-tv-select' ? storageKeys.tvPublic :
          id === 'own-upload-tv-select' ? storageKeys.tvOwnUpload :
          id === 'edit-upload-tv-select' ? storageKeys.tvEditUpload :
          id === 'remix-tv-select' ? storageKeys.tvRemix :
          id === 'upload-tv-select' || id === 'batch-upload-tv-select' ? storageKeys.tvUpload :
          storageKeys.tvArt
        );
        if (remembered && [...sel.options].some(o => o.value === remembered)) {
          sel.value = remembered;
        }
      }
    }
    // Show the TV art section once we have TVs
    const artSection = document.getElementById('tv-art-section');
    artSection.style.display = knownTVs.length > 0 ? 'block' : 'none';
  }

  function renderTVList() {
    const list = document.getElementById('tv-list');
    const empty = document.getElementById('tv-empty');
    if (!knownTVs.length) {
      list.innerHTML = '';
      empty.textContent = 'No TVs found. Click "Scan Network", use "Add by IP", or configure config.yaml.';
      empty.style.display = 'block';
      renderSettingsTVSummary();
      return;
    }
    empty.style.display = 'none';
    list.innerHTML = knownTVs.map((t, index) => {
      let badgeClass = 'badge-config';
      let badgeText = 'Configured';
      if (t.source === 'discovered') {
        badgeClass = t.frame_tv ? 'badge-frame' : 'badge-samsung';
        badgeText = t.frame_tv ? 'Frame TV' : 'Samsung TV';
      } else if (t.source === 'manual') {
        badgeClass = 'badge-manual';
        badgeText = 'Manual · session';
      }
      const detail = t.model ? (esc(t.model) + ' &middot; ' + esc(t.ip)) : esc(t.ip);
      const removeButton = t.source === 'config' ? '' :
        '<button class="btn btn-ghost btn-small" data-remove-tv-index="' +
        index + '">Remove</button>';
      const saveButton = t.source === 'config' ? '' :
        '<button class="btn btn-secondary btn-small" data-save-tv-index="' +
        index + '">Save</button>';
      return `
        <div class="tv-card">
          <div class="tv-info">
            <div class="tv-name">${esc(t.name)}</div>
            <div class="tv-detail">${detail}</div>
          </div>
          <div class="tv-actions">
            <span class="badge ${badgeClass}">${badgeText}</span>
            ${saveButton}
            ${removeButton}
          </div>
        </div>`;
    }).join('');
    animateStaggeredChildren(list, '.tv-card');
    renderSettingsTVSummary();
  }

  function renderSettingsTVSummary() {
    const container = document.getElementById('settings-tv-list');
    if (!container) return;
    if (!managedTVSettings.length) {
      container.innerHTML = '<div class="settings-item"><div class="settings-item-main"><strong>No persistent TVs</strong><span>Use Add TV or save a discovered TV.</span></div></div>';
      return;
    }
    container.innerHTML = managedTVSettings.map((tv, index) => {
      const tokenState = tv.token_configured ? 'Paired' : 'Not paired';
      const conflicts = tv.conflicts_with || [];
      const conflictText = conflicts.length
        ? (' · ⚠ Duplicate of ' + conflicts.join(', '))
        : '';
      const consolidateButton = conflicts.length
        ? ('<button class="btn btn-secondary btn-small" data-settings-tv-action="consolidate" data-settings-tv-index="' + index + '">Consolidate</button>')
        : '';
      return '<div class="settings-item">' +
        '<div class="settings-item-main"><strong>' + esc(tv.profile_id) + '</strong>' +
        '<span>' + esc(tv.ip) + ':' + tv.port + ' · ' + esc(tokenState + conflictText) + '</span></div>' +
        '<div class="settings-item-actions">' +
        '<button class="btn btn-ghost btn-small" data-settings-tv-action="test" data-settings-tv-index="' + index + '">Test</button>' +
        '<button class="btn btn-ghost btn-small" data-settings-tv-action="pair" data-settings-tv-index="' + index + '">Pair</button>' +
        '<button class="btn btn-secondary btn-small" data-settings-tv-action="edit" data-settings-tv-index="' + index + '">Edit</button>' +
        consolidateButton +
        '<button class="btn btn-danger btn-small" data-settings-tv-action="delete" data-settings-tv-index="' + index + '">Delete</button>' +
        '</div></div>';
    }).join('');
  }

  document.getElementById('tv-list').addEventListener('click', (event) => {
    const saveButton = event.target.closest('[data-save-tv-index]');
    if (saveButton) {
      const tv = knownTVs[Number(saveButton.dataset.saveTvIndex)];
      if (tv) openTVSettingsEditor(null, tv);
      return;
    }
    const button = event.target.closest('[data-remove-tv-index]');
    if (!button) return;
    removeSessionTV(Number(button.dataset.removeTvIndex));
  });

  // =========================================================================
  // Navigation (top-level pages + create modes)
  // =========================================================================
  const pageTabs = document.querySelectorAll('.tabs button, .mobile-nav button');
  const panels = document.querySelectorAll('.panel');
  const createModeTabs = document.querySelectorAll('.create-modes button');
  const createPanels = document.querySelectorAll('.create-panel');

  function setActiveCreateMode(modeName) {
    createModeTabs.forEach((tab) => tab.classList.toggle('active', tab.dataset.mode === modeName));
    createPanels.forEach((panel) => panel.classList.toggle('active', panel.id === ('create-panel-' + modeName)));
    localStorage.setItem(storageKeys.createMode, modeName);
    if (modeName === 'ai') loadGenerationJobsFromAPI();
  }

  function setActivePage(pageName) {
    pageTabs.forEach((tab) => tab.classList.toggle('active', tab.dataset.page === pageName));
    panels.forEach((panel) => panel.classList.toggle('active', panel.id === ('panel-' + pageName)));
    localStorage.setItem(storageKeys.page, pageName);
    if (pageName === 'library') {
      loadLibraryCollections().then(loadGallery).catch((error) => {
        showToast(error?.message || 'Could not load library metadata.', 'error');
      });
    }
    if (pageName === 'automations') loadAutomations().catch((error) => {
      showToast(error?.message || 'Could not load automations.', 'error');
    });
    if (pageName === 'modes') loadLiveScores().catch((error) => {
      showToast(error?.message || 'Could not load live modes.', 'error');
    });
    if (pageName === 'create' && getActiveCreateModeName() === 'ai') loadGenerationJobsFromAPI();
  }

  pageTabs.forEach(btn => {
    btn.addEventListener('click', () => {
      setActivePage(btn.dataset.page);
    });
  });

  createModeTabs.forEach((btn) => {
    btn.addEventListener('click', () => {
      setActiveCreateMode(btn.dataset.mode);
    });
  });

  function getActivePageName() {
    const activeTab = document.querySelector('.tabs button.active');
    return activeTab ? activeTab.dataset.page : 'create';
  }

  function getActiveCreateModeName() {
    const activeMode = document.querySelector('.create-modes button.active');
    return activeMode ? activeMode.dataset.mode : 'ai';
  }

  const rememberedCreateMode = localStorage.getItem(storageKeys.createMode);
  if (rememberedCreateMode && [...createModeTabs].some((tab) => tab.dataset.mode === rememberedCreateMode)) {
    setActiveCreateMode(rememberedCreateMode);
  } else {
    setActiveCreateMode('ai');
  }

  const rememberedPage = localStorage.getItem(storageKeys.page);
  if (rememberedPage && [...pageTabs].some((tab) => tab.dataset.page === rememberedPage)) {
    setActivePage(rememberedPage);
  } else {
    setActivePage('create');
  }

  // =========================================================================
  // Startup: load styles + configured TVs in parallel
  // =========================================================================
  apiFetch('/styles').then(r => r.json()).then(styles => {
    const sel = document.getElementById('style');
    for (const [key] of Object.entries(styles)) {
      const opt = document.createElement('option');
      opt.value = key; opt.textContent = key.replace(/_/g, ' ');
      sel.appendChild(opt);
    }
  }).catch(() => {});

  apiFetch('/providers').then(r => r.json()).then((payload) => {
    refreshProviderSelects(payload);
  }).catch(() => {
    configuredProviders = [{
      name: 'openai',
      is_default: true,
      models: [],
      default_model: '',
    }];
    const providerSel = document.getElementById('provider');
    const modelSel = document.getElementById('model');
    const editProviderSel = document.getElementById('edit-provider');
    const editModelSel = document.getElementById('edit-model');
    const remixProviderSel = document.getElementById('remix-provider');
    const remixModelSel = document.getElementById('remix-model');
    providerSel.innerHTML = '<option value="openai">openai</option>';
    providerSel.value = 'openai';
    modelSel.innerHTML = '<option value="">Provider default</option>';
    editProviderSel.innerHTML = '<option value="openai">openai</option>';
    editProviderSel.value = 'openai';
    editModelSel.innerHTML = '<option value="">Provider default</option>';
    remixProviderSel.innerHTML = '<option value="openai">openai</option>';
    remixProviderSel.value = 'openai';
    remixModelSel.innerHTML = '<option value="">Provider default</option>';
    renderSettingsProviders();
    showToast('Failed to load configured providers; using fallback defaults.', 'warn');
  });

  // Load pre-configured TVs and editable management settings on startup.
  reloadConfiguredTVs().catch(() => {
    document.getElementById('tv-empty').textContent =
      'No configured TVs. Click "Scan Network" to find Samsung TVs.';
  });
  loadManagementSettings();
  loadLibraryCollections().catch(() => {});

  document.getElementById('tv-select').addEventListener('change', (e) => {
    localStorage.setItem(storageKeys.tvGenerate, e.target.value || '');
  });
  document.getElementById('provider').addEventListener('change', (e) => {
    const provider = e.target.value || '';
    localStorage.setItem(storageKeys.providerGenerate, provider);
    refreshModelSelect(provider, '');
    localStorage.setItem(storageKeys.modelGenerate, '');
  });
  document.getElementById('model').addEventListener('change', (e) => {
    localStorage.setItem(storageKeys.modelGenerate, e.target.value || '');
  });
  document.getElementById('edit-provider').addEventListener('change', (e) => {
    const provider = e.target.value || '';
    localStorage.setItem(storageKeys.providerEdit, provider);
    refreshModelSelectFor(provider, '', 'edit-model');
    localStorage.setItem(storageKeys.modelEdit, '');
  });
  document.getElementById('edit-model').addEventListener('change', (e) => {
    localStorage.setItem(storageKeys.modelEdit, e.target.value || '');
  });
  document.getElementById('remix-provider').addEventListener('change', (e) => {
    const provider = e.target.value || '';
    localStorage.setItem(storageKeys.providerRemix, provider);
    refreshModelSelectFor(provider, '', 'remix-model');
    localStorage.setItem(storageKeys.modelRemix, '');
  });
  document.getElementById('remix-model').addEventListener('change', (e) => {
    localStorage.setItem(storageKeys.modelRemix, e.target.value || '');
  });
  document.getElementById('tv-art-select').addEventListener('change', (e) => {
    localStorage.setItem(storageKeys.tvArt, e.target.value || '');
  });
  document.getElementById('upload-tv-select').addEventListener('change', (e) => {
    localStorage.setItem(storageKeys.tvUpload, e.target.value || '');
  });
  document.getElementById('batch-upload-tv-select').addEventListener('change', (e) => {
    localStorage.setItem(storageKeys.tvUpload, e.target.value || '');
  });
  document.getElementById('remix-tv-select').addEventListener('change', (e) => {
    localStorage.setItem(storageKeys.tvRemix, e.target.value || '');
  });
  document.getElementById('public-tv-select').addEventListener('change', (e) => {
    localStorage.setItem(storageKeys.tvPublic, e.target.value || '');
  });
  document.getElementById('own-upload-tv-select').addEventListener('change', (e) => {
    localStorage.setItem(storageKeys.tvOwnUpload, e.target.value || '');
  });
  document.getElementById('edit-upload-tv-select').addEventListener('change', (e) => {
    localStorage.setItem(storageKeys.tvEditUpload, e.target.value || '');
  });
  document.getElementById('public-matte-select').addEventListener('change', (e) => {
    localStorage.setItem(storageKeys.mattePublic, e.target.value || '');
  });
  document.getElementById('own-upload-matte-select').addEventListener('change', (e) => {
    localStorage.setItem(storageKeys.matteOwnUpload, e.target.value || '');
  });
  document.getElementById('edit-upload-matte-select').addEventListener('change', (e) => {
    localStorage.setItem(storageKeys.matteEditUpload, e.target.value || '');
  });
  document.getElementById('upload-matte-select').addEventListener('change', (e) => {
    localStorage.setItem(storageKeys.matteUpload, e.target.value || '');
  });
  document.getElementById('batch-upload-matte-select').addEventListener('change', (e) => {
    localStorage.setItem(storageKeys.matteUpload, e.target.value || '');
  });
  document.getElementById('remix-matte-select').addEventListener('change', (e) => {
    localStorage.setItem(storageKeys.matteRemix, e.target.value || '');
  });
  const rememberedPublicMatte = localStorage.getItem(storageKeys.mattePublic);
  if (rememberedPublicMatte) {
    const sel = document.getElementById('public-matte-select');
    if ([...sel.options].some(o => o.value === rememberedPublicMatte)) sel.value = rememberedPublicMatte;
  }
  const rememberedOwnUploadMatte = localStorage.getItem(storageKeys.matteOwnUpload);
  if (rememberedOwnUploadMatte) {
    const sel = document.getElementById('own-upload-matte-select');
    if ([...sel.options].some(o => o.value === rememberedOwnUploadMatte)) sel.value = rememberedOwnUploadMatte;
  }
  const rememberedEditUploadMatte = localStorage.getItem(storageKeys.matteEditUpload);
  if (rememberedEditUploadMatte) {
    const sel = document.getElementById('edit-upload-matte-select');
    if ([...sel.options].some(o => o.value === rememberedEditUploadMatte)) sel.value = rememberedEditUploadMatte;
  }
  const rememberedUploadMatte = localStorage.getItem(storageKeys.matteUpload);
  if (rememberedUploadMatte) {
    const sel = document.getElementById('upload-matte-select');
    if ([...sel.options].some(o => o.value === rememberedUploadMatte)) sel.value = rememberedUploadMatte;
    const batchSel = document.getElementById('batch-upload-matte-select');
    if ([...batchSel.options].some(o => o.value === rememberedUploadMatte)) batchSel.value = rememberedUploadMatte;
  }
  const rememberedRemixMatte = localStorage.getItem(storageKeys.matteRemix);
  if (rememberedRemixMatte) {
    const sel = document.getElementById('remix-matte-select');
    if ([...sel.options].some(o => o.value === rememberedRemixMatte)) sel.value = rememberedRemixMatte;
  }

  // =========================================================================
  // Status helpers
  // =========================================================================
  const statusBar = document.getElementById('gen-status');
  const statusText = document.getElementById('gen-status-text');
  function showStatus(msg, cls) {
    statusBar.className = 'status-bar visible' + (cls ? ' ' + cls : '');
    statusText.textContent = msg;
  }

  function gallerySkeleton(count) {
    return Array.from({ length: count }).map(() => `
      <div class="skeleton-card">
        <div class="sk-thumb"></div>
        <div class="sk-line"></div>
        <div class="sk-line short"></div>
      </div>
    `).join('');
  }

  // =========================================================================
  // Generate
  // =========================================================================
  function renderGenerationJobs() {
    const grid = document.getElementById('gen-jobs-grid');
    const empty = document.getElementById('gen-jobs-empty');
    const jobs = Array.from(generationJobs.values())
      .sort((a, b) => b.createdAt - a.createdAt);

    if (!jobs.length) {
      grid.innerHTML = '';
      empty.style.display = 'block';
      return;
    }
    empty.style.display = 'none';
    grid.innerHTML = jobs.map((job) => {
      const providerLabel = job.provider || 'default';
      const modelLabel = job.model || 'default';
      const styleLabel = job.style || 'none';
      const tvLabel = job.tvIp || 'no TV';
      const cardClass = job.status === 'completed' ? ' done' : (job.status === 'failed' ? ' error' : '');
      const prompt = esc(job.prompt || '');
      const errorBlock = job.error ? `<div class="gen-job-meta" style="color:var(--err)">Error: ${esc(job.error)}</div>` : '';
      const deliveryBlock = job.deliveryStatus && job.deliveryStatus !== 'not_requested'
        ? `<div class="gen-job-meta">Delivery: ${esc(job.deliveryStatus.replaceAll('_', ' '))}</div>`
        : '';
      const previewToken = job.previewNonce || '';
      const imageJobId = job.resultJobId || job.jobId;
      const previewBlock = job.imageAvailable
        ? `<div class="gen-job-thumb-wrap">
             <img src="/jobs/${esc(imageJobId)}/image?${previewToken}" alt="${prompt}" loading="lazy"
               onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
             <div class="gen-job-thumb-fallback">No image available</div>
           </div>`
        : '';
      const actions = [];
      if (job.imageAvailable) {
        actions.push(`<button class="btn btn-secondary btn-small btn-gen-open" data-job-id="${esc(imageJobId)}">Open Image</button>`);
      }
      if (job.errorCode === 'tv_unreachable' || job.errorCode === 'tv_art_mode_unavailable') {
        if (job.generationSucceeded && job.imageAvailable && job.tvIp) {
          actions.push(`<button class="btn btn-small btn-gen-retry-tv" data-queue-id="${esc(job.jobId)}">Retry TV</button>`);
        } else if (!job.generationSucceeded) {
          actions.push(`<button class="btn btn-small btn-gen-anyway" data-queue-id="${esc(job.jobId)}">Generate Anyway</button>`);
        }
      }
      const actionsBlock = actions.length
        ? `<div class="gen-job-actions">${actions.join('')}</div>`
        : '';
      return `
        <div class="gen-job-card${cardClass}">
          <div class="gen-job-row">
            <span class="gen-job-id">${esc(job.jobId)}</span>
            <span class="gen-job-status ${esc(job.status)}">${esc(job.status)}</span>
          </div>
          <div class="gen-job-prompt">${prompt}</div>
          <div class="gen-job-meta">Provider: ${esc(providerLabel)} · Model: ${esc(modelLabel)}</div>
          <div class="gen-job-meta">Style: ${esc(styleLabel)} · TV: ${esc(tvLabel)}</div>
          ${errorBlock}
          ${deliveryBlock}
          ${previewBlock}
          ${actionsBlock}
        </div>
      `;
    }).join('');

    grid.querySelectorAll('.btn-gen-open').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        const jobId = e.currentTarget.dataset.jobId;
        if (!jobId) return;
        window.showPreview(jobId);
      });
    });
    grid.querySelectorAll('.btn-gen-anyway').forEach((btn) => {
      btn.addEventListener('click', async (event) => {
        const job = generationJobs.get(event.currentTarget.dataset.queueId);
        if (!job) return;
        setButtonBusy(event.currentTarget, 'Queueing...');
        try {
          const queued = await queueGeneration(job, { generateAnyway: true });
          showStatus('Queued generate-anyway job ' + queued.job_id + '.', '');
        } catch (error) {
          showStatus('Failed: ' + error.message, 'error');
        } finally {
          clearButtonBusy(event.currentTarget);
        }
      });
    });
    grid.querySelectorAll('.btn-gen-retry-tv').forEach((btn) => {
      btn.addEventListener('click', async (event) => {
        const job = generationJobs.get(event.currentTarget.dataset.queueId);
        if (!job) return;
        setButtonBusy(event.currentTarget, 'Retrying...');
        try {
          const response = await apiFetch(
            '/jobs/' + encodeURIComponent(job.resultJobId || job.jobId) + '/apply',
            {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ tv_ip: job.tvIp, matte: 'none' }),
            },
          );
          const result = await parseJSONResponse(response, 'TV delivery retry failed.');
          job.status = 'completed';
          job.error = null;
          job.errorCode = null;
          job.deliveryStatus = result.delivery_status || 'displayed';
          renderGenerationJobs();
          showStatus('Delivered saved artwork to the TV.', 'done');
        } catch (error) {
          showStatus('Delivery failed: ' + error.message, 'error');
        } finally {
          clearButtonBusy(event.currentTarget);
        }
      });
    });
  }

  async function queueGeneration(source, { generateAnyway = false } = {}) {
    const useTV = Boolean(source.tvIp);
    const endpoint = useTV ? '/async/generate-and-apply' : '/async/generate';
    const body = {
      prompt: source.prompt,
      style: source.style || undefined,
      provider: source.provider || undefined,
      model: source.model || undefined,
    };
    if (useTV) body.tv_ip = source.tvIp;
    if (generateAnyway) body.generate_anyway = true;
    const response = await apiFetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await parseJSONResponse(response, 'Could not queue generation.');
    generationJobs.set(data.job_id, {
      jobId: data.job_id,
      prompt: source.prompt,
      provider: source.provider,
      model: source.model,
      style: source.style,
      tvIp: source.tvIp,
      status: data.status || 'pending',
      error: null,
      errorCode: null,
      generationSucceeded: false,
      deliveryStatus: generateAnyway ? 'skipped' : 'not_attempted',
      imageAvailable: false,
      createdAt: Date.now(),
    });
    renderGenerationJobs();
    ensureGenerationPolling();
    return data;
  }

  function clearFinishedGenerationJobs() {
    for (const [jobId, job] of generationJobs.entries()) {
      if (job.status === 'completed' || job.status === 'failed') {
        generationJobs.delete(jobId);
      }
    }
    renderGenerationJobs();
  }

  function ensureGenerationPolling() {
    if (generationPollTimer) return;
    generationPollTimer = setInterval(pollGenerationJobs, 1800);
  }

  function stopGenerationPollingIfIdle() {
    const hasActive = Array.from(generationJobs.values())
      .some((j) => j.status === 'pending' || j.status === 'running');
    if (!hasActive && generationPollTimer) {
      clearInterval(generationPollTimer);
      generationPollTimer = null;
    }
  }

  async function pollGenerationJobs() {
    const activeJobs = Array.from(generationJobs.values())
      .filter((j) => j.status === 'pending' || j.status === 'running');
    if (!activeJobs.length) {
      stopGenerationPollingIfIdle();
      return;
    }

    let changed = false;
    await Promise.all(activeJobs.map(async (job) => {
      try {
        const resp = await apiFetch('/jobs/' + job.jobId + '/status');
        if (!resp.ok) {
          const nextError = 'Status check failed (' + resp.status + ')';
          if (job.status !== 'failed' || job.error !== nextError) {
            job.status = 'failed';
            job.error = nextError;
            changed = true;
          }
          return;
        }
        const data = await resp.json();
        const nextStatus = data.status || job.status;
        const result = data.result || null;
        const nextError = data.error || result?.error || null;
        if (nextStatus !== job.status || nextError !== job.error) {
          changed = true;
          job.status = nextStatus;
          job.error = nextError;
        }
        if (result) {
          const nextResultJobId = result.job_id || job.jobId;
          const nextImageAvailable = Boolean(result.final_path);
          if (job.resultJobId !== nextResultJobId || job.imageAvailable !== nextImageAvailable) {
            changed = true;
            job.resultJobId = nextResultJobId;
            job.imageAvailable = nextImageAvailable;
          }
          if (!job.previewNonce) {
            changed = true;
            job.previewNonce = Date.now();
          }
          job.errorCode = result.error_code || null;
          job.generationSucceeded = Boolean(result.generation_succeeded);
          job.deliveryStatus = result.delivery_status || 'not_requested';
        }
        if (data.status === 'completed' && result) {
          showStatus('Done: ' + job.jobId, 'done');
          if (job.imageAvailable) {
            const img = document.getElementById('gen-preview-img');
            img.src = '/jobs/' + job.resultJobId + '/image?' + Date.now();
            document.getElementById('gen-preview').style.display = 'block';
          }
        } else if (data.status === 'failed') {
          showStatus('Failed: ' + (data.error || job.jobId), 'error');
        }
      } catch (e) {
        if (job.status !== 'failed' || job.error !== e.message) {
          job.status = 'failed';
          job.error = e.message;
          changed = true;
        }
      }
    }));

    if (changed) renderGenerationJobs();
    stopGenerationPollingIfIdle();
  }

  const btnGen = document.getElementById('btn-generate');
  document.getElementById('btn-gen-jobs-clear').addEventListener('click', clearFinishedGenerationJobs);
  btnGen.addEventListener('click', async () => {
    const prompt = document.getElementById('prompt').value.trim();
    if (!prompt) { showStatus('Please enter a prompt.', 'error'); return; }

    const style = document.getElementById('style').value || undefined;
    const provider = document.getElementById('provider').value || undefined;
    const model = document.getElementById('model').value || undefined;
    const tvIp = document.getElementById('tv-select').value || undefined;

    setButtonBusy(btnGen, 'Queueing...');
    showStatus('Submitting job...');

    try {
      const data = await queueGeneration({ prompt, provider, model, style, tvIp });
      showStatus('Queued job ' + data.job_id + '.', '');
    } catch (e) {
      showStatus('Failed: ' + e.message, 'error');
    } finally {
      clearButtonBusy(btnGen);
    }
  });

  async function loadGenerationJobsFromAPI() {
    try {
      const resp = await apiFetch('/async/jobs?limit=60');
      if (!resp.ok) throw new Error('Server returned ' + resp.status);
      const items = await resp.json();
      for (const item of items) {
        const req = item.request || {};
        const kind = req.type || '';
        if (kind !== 'generate' && kind !== 'generate-and-apply') continue;
        const existing = generationJobs.get(item.job_id);
        const current = existing || {
          jobId: item.job_id,
          createdAt: Date.now(),
        };
        const resultJobId = item.result && item.result.job_id ? item.result.job_id : item.job_id;
        current.prompt = req.prompt || current.prompt || '';
        current.provider = req.provider || current.provider || '';
        current.model = req.model || current.model || '';
        current.style = req.style || current.style || '';
        current.tvIp = req.tv_ip || current.tvIp || '';
        current.status = item.status || current.status || 'pending';
        current.error = item.error || null;
        current.resultJobId = resultJobId;
        current.imageAvailable = Boolean(item.result && item.result.final_path);
        current.errorCode = item.result?.error_code || null;
        current.generationSucceeded = Boolean(item.result?.generation_succeeded);
        current.deliveryStatus = item.result?.delivery_status || 'not_requested';
        if (current.imageAvailable && !current.previewNonce) current.previewNonce = Date.now();
        generationJobs.set(item.job_id, current);
      }
      renderGenerationJobs();
      const hasActive = Array.from(generationJobs.values())
        .some((j) => j.status === 'pending' || j.status === 'running');
      if (hasActive) ensureGenerationPolling();
    } catch (e) {
      showToast('Failed to load async jobs: ' + e.message, 'warn');
    }
  }

  // =========================================================================
  // Gallery
  // =========================================================================
  async function loadLibraryCollections() {
    const response = await apiFetch('/library/collections');
    managedCollections = await parseJSONResponse(response, 'Could not load collections.');
    const options = managedCollections.map((collection) =>
      '<option value="' + esc(collection.id) + '">' + esc(collection.name) +
      ' (' + collection.item_count + ')</option>'
    ).join('');
    const filter = document.getElementById('gallery-collection-filter');
    const target = document.getElementById('library-target-collection');
    const filterValue = filter.value;
    const targetValue = target.value;
    filter.innerHTML = '<option value="">All collections</option>' + options;
    target.innerHTML = '<option value="">Choose collection...</option>' + options;
    if ([...filter.options].some((option) => option.value === filterValue)) {
      filter.value = filterValue;
    }
    if ([...target.options].some((option) => option.value === targetValue)) {
      target.value = targetValue;
    }
  }

  async function setTagsForJobs(jobIds) {
    if (!jobIds.length) return;
    const existing = jobIds.length === 1 ? (loadedGalleryJobs[jobIds[0]]?.tags || []).join(', ') : '';
    const value = window.prompt('Tags (comma separated). Leave blank to clear.', existing);
    if (value === null) return;
    const tags = value.split(',').map((tag) => tag.trim()).filter(Boolean);
    for (const jobId of jobIds) {
      const response = await apiFetch('/jobs/' + encodeURIComponent(jobId) + '/tags', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tags }),
      });
      await parseJSONResponse(response, 'Could not save tags.');
    }
    await loadGallery();
    showToast('Artwork tags saved.', 'done');
  }

  async function addJobsToSelectedCollection(jobIds) {
    const collectionId = document.getElementById('library-target-collection').value;
    if (!collectionId) {
      showToast('Choose a target collection first.', 'warn');
      return;
    }
    const response = await apiFetch(
      '/library/collections/' + encodeURIComponent(collectionId) + '/items',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job_ids: jobIds }),
      },
    );
    await parseJSONResponse(response, 'Could not update collection.');
    await Promise.all([loadLibraryCollections(), loadGallery()]);
    showToast('Artwork added to collection.', 'done');
  }

  function updateGallerySelectionUI() {
    const btn = document.getElementById('btn-gallery-delete-selected');
    const inlineDeleteBtn = document.getElementById('btn-library-delete-selected-inline');
    const inlineDisplayBtn = document.getElementById('btn-library-display-selected');
    const inlineTagBtn = document.getElementById('btn-library-tag-selected');
    const inlineCollectBtn = document.getElementById('btn-library-collect-selected');
    const inlineUncollectBtn = document.getElementById('btn-library-uncollect-selected');
    const inlineClearBtn = document.getElementById('btn-library-clear-selection-inline');
    const bar = document.getElementById('library-selection-bar');
    const text = document.getElementById('library-selection-text');
    const n = selectedGalleryJobIds.size;
    const hasSelection = n > 0;
    const label = n === 1 ? '1 artwork selected' : (n + ' artwork selected');

    btn.disabled = n === 0;
    btn.textContent = n > 0 ? ('Delete Selected (' + n + ')') : 'Delete Selected';
    inlineDeleteBtn.disabled = !hasSelection;
    inlineDisplayBtn.disabled = !hasSelection;
    inlineTagBtn.disabled = !hasSelection;
    inlineCollectBtn.disabled = !hasSelection;
    inlineUncollectBtn.disabled = !hasSelection;
    inlineClearBtn.disabled = !hasSelection;
    text.textContent = label;
    bar.classList.toggle('visible', hasSelection);
  }

  function bindGallerySelectionHandlers() {
    const items = document.querySelectorAll('.gallery-select-item');
    items.forEach((item) => {
      item.addEventListener('change', (e) => {
        const checkbox = e.target;
        const card = checkbox.closest('.gallery-item');
        const jobId = checkbox.dataset.jobId;
        if (!jobId) return;
        if (checkbox.checked) selectedGalleryJobIds.add(jobId);
        else selectedGalleryJobIds.delete(jobId);
        if (card) card.classList.toggle('selected', checkbox.checked);
        updateGallerySelectionUI();
      });
    });
  }

  function setAllGallerySelections(checked) {
    document.querySelectorAll('.gallery-select-item').forEach((el) => {
      const jobId = el.dataset.jobId;
      if (!jobId) return;
      el.checked = checked;
      const card = el.closest('.gallery-item');
      if (card) card.classList.toggle('selected', checked);
      if (checked) selectedGalleryJobIds.add(jobId);
      else selectedGalleryJobIds.delete(jobId);
    });
    updateGallerySelectionUI();
  }

  async function deleteSelectedGalleryJobs() {
    const selectedIds = Array.from(selectedGalleryJobIds);
    if (!selectedIds.length) return;

    if (!confirm('Delete ' + selectedIds.length + ' selected artwork item(s) from host storage?')) return;

    const btn = document.getElementById('btn-gallery-delete-selected');
    setButtonBusy(btn, 'Deleting...');

    try {
      const resp = await apiFetch('/jobs/delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ job_ids: selectedIds }),
      });
      if (!resp.ok) throw new Error('Server returned ' + resp.status);
      const data = await resp.json();
      if (data.failed && Object.keys(data.failed).length) {
        showToast('Some items failed to delete: ' + Object.keys(data.failed).join(', '), 'warn');
      }
      if (data.not_found && data.not_found.length) {
        showToast('Some items were not found: ' + data.not_found.join(', '), 'warn');
      }
      selectedGalleryJobIds = new Set();
      updateGallerySelectionUI();
      await loadGallery();
      showToast('Deleted selected gallery items.', 'done');
    } catch (e) {
      showToast('Failed to delete selected artwork: ' + e.message, 'error');
      updateGallerySelectionUI();
    } finally {
      clearButtonBusy(btn);
    }
  }

  function openBatchUploadModal() {
    const selectedIds = Array.from(selectedGalleryJobIds);
    if (!selectedIds.length) return;
    document.getElementById('batch-upload-count').textContent = String(selectedIds.length);
    const statusEl = document.getElementById('batch-upload-status');
    const statusTextEl = document.getElementById('batch-upload-status-text');
    statusEl.classList.remove('visible');

    refreshTVSelects();
    const batchTvSel = document.getElementById('batch-upload-tv-select');
    const rememberedTv = localStorage.getItem(storageKeys.tvUpload);
    if (rememberedTv && [...batchTvSel.options].some((o) => o.value === rememberedTv)) {
      batchTvSel.value = rememberedTv;
    }
    if (!knownTVs.length) {
      statusEl.className = 'status-bar visible error';
      statusTextEl.textContent = 'No TVs available. Configure one or run Scan Network first.';
    }
    loadMattesForSelect('batch-upload-matte-select', batchTvSel.value || knownTVs[0]?.ip);
    openModal('batch-upload-modal', '#batch-upload-tv-select');
  }

  async function applyBatchUpload() {
    const selectedIds = Array.from(selectedGalleryJobIds);
    const tvIp = document.getElementById('batch-upload-tv-select').value;
    const matte = document.getElementById('batch-upload-matte-select').value || 'none';
    const statusEl = document.getElementById('batch-upload-status');
    const statusTextEl = document.getElementById('batch-upload-status-text');
    const btn = document.getElementById('btn-batch-upload-apply');

    if (!selectedIds.length) {
      statusEl.className = 'status-bar visible error';
      statusTextEl.textContent = 'No library items selected.';
      return;
    }
    if (!tvIp) {
      statusEl.className = 'status-bar visible error';
      statusTextEl.textContent = 'Please select a TV.';
      return;
    }

    setButtonBusy(btn, 'Displaying...');
    statusEl.className = 'status-bar visible';
    statusTextEl.textContent = 'Uploading selected artwork...';

    let success = 0;
    const failed = [];
    for (const jobId of selectedIds) {
      try {
        const resp = await apiFetch('/jobs/' + encodeURIComponent(jobId) + '/apply', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ tv_ip: tvIp, matte: matte }),
        });
        if (!resp.ok) throw new Error('Server returned ' + resp.status);
        success += 1;
      } catch {
        failed.push(jobId);
      }
    }

    if (success > 0) {
      selectedGalleryJobIds = new Set();
      updateGallerySelectionUI();
      await loadGallery();
    }

    if (failed.length) {
      statusEl.className = 'status-bar visible error';
      statusTextEl.textContent = 'Displayed ' + success + ', failed ' + failed.length + '.';
      showToast('Some selected items failed to display on TV.', 'warn');
    } else {
      statusEl.className = 'status-bar visible done';
      statusTextEl.textContent = 'Displayed ' + success + ' selected artwork on TV.';
      showToast('Displayed selected artwork on TV.', 'done');
      closeModal('batch-upload-modal');
    }
    clearButtonBusy(btn);
  }

  async function loadGallery() {
    const grid = document.getElementById('gallery-grid');
    const empty = document.getElementById('gallery-empty');
    selectedGalleryJobIds = new Set();
    updateGallerySelectionUI();
    grid.innerHTML = gallerySkeleton(6);
    empty.style.display = 'none';
    try {
      const params = new URLSearchParams({ limit: '50' });
      const query = document.getElementById('gallery-search').value.trim();
      const tag = document.getElementById('gallery-tag-filter').value.trim();
      const collection = document.getElementById('gallery-collection-filter').value;
      if (query) params.set('q', query);
      if (tag) params.set('tag', tag);
      if (collection) params.set('collection', collection);
      const resp = await apiFetch('/jobs?' + params.toString());
      const jobs = await parseJSONResponse(resp, 'Could not load library.');
      loadedGalleryJobs = Object.fromEntries(jobs.map((job) => [job.job_id, job]));
      if (!jobs.length) { grid.innerHTML = ''; empty.style.display = 'block'; return; }
      empty.style.display = 'none';
      grid.innerHTML = jobs.map(j => {
        const promptText = esc(j.prompt || j.job_id);
        const promptShort = esc((j.prompt || '').substring(0, 40));
        const chips = [...(j.tags || []), ...(j.collections || []).map((name) => '#' + name)];
        return `
        <div class="gallery-item">
          <img src="/jobs/${esc(j.job_id)}/image" alt="${promptShort}" loading="lazy"
               data-preview-job-id="${esc(j.job_id)}"
               onerror="this.style.display='none'">
          <div class="info">
            <label class="select-row">
              <input type="checkbox" class="gallery-select-item" data-job-id="${esc(j.job_id)}">
              <span>Select</span>
            </label>
            <div class="prompt">${promptText}</div>
            <div class="meta">${esc(j.provider || '')} ${j.content_id ? '&middot; on TV' : ''}</div>
            <div class="library-chips">${chips.map((chip) =>
              '<span class="library-chip">' + esc(chip) + '</span>'
            ).join('')}</div>
          </div>
          <div class="actions">
            <button class="btn btn-secondary btn-small"
                    data-upload-job-id="${esc(j.job_id)}">
              Upload to TV</button>
            <button class="btn btn-secondary btn-small"
                    data-remix-job-id="${esc(j.job_id)}">
              Edit / Generate New</button>
            <button class="btn btn-ghost btn-small" data-tag-job-id="${esc(j.job_id)}">Tags</button>
          </div>
        </div>`;
      }).join('');
      animateStaggeredChildren(grid, '.gallery-item');
      bindGallerySelectionHandlers();
    } catch (e) {
      grid.innerHTML = '<div class="empty">Failed to load gallery. Use Refresh to retry.</div>';
      showToast('Failed to load gallery: ' + e.message, 'error');
    } finally {
      updateGallerySelectionUI();
    }
  }

  document.getElementById('btn-refresh-gallery').addEventListener('click', loadGallery);
  document.getElementById('btn-gallery-select-all').addEventListener('click', () => setAllGallerySelections(true));
  document.getElementById('btn-gallery-clear-selection').addEventListener('click', () => setAllGallerySelections(false));
  document.getElementById('btn-gallery-delete-selected').addEventListener('click', deleteSelectedGalleryJobs);
  document.getElementById('btn-library-display-selected').addEventListener('click', openBatchUploadModal);
  document.getElementById('btn-library-tag-selected').addEventListener('click', () => {
    setTagsForJobs(Array.from(selectedGalleryJobIds)).catch((error) => {
      showToast(error?.message || 'Could not save tags.', 'error');
    });
  });
  document.getElementById('btn-library-collect-selected').addEventListener('click', () => {
    addJobsToSelectedCollection(Array.from(selectedGalleryJobIds)).catch((error) => {
      showToast(error?.message || 'Could not update collection.', 'error');
    });
  });
  document.getElementById('btn-library-uncollect-selected').addEventListener('click', async () => {
    const collectionId = document.getElementById('library-target-collection').value;
    const jobIds = Array.from(selectedGalleryJobIds);
    if (!collectionId) {
      showToast('Choose a target collection first.', 'warn');
      return;
    }
    try {
      const response = await apiFetch(
        '/library/collections/' + encodeURIComponent(collectionId) + '/items',
        {
          method: 'DELETE',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ job_ids: jobIds }),
        },
      );
      await parseJSONResponse(response, 'Could not update collection.');
      await Promise.all([loadLibraryCollections(), loadGallery()]);
      showToast('Artwork removed from collection.', 'done');
    } catch (error) {
      showToast(error?.message || 'Could not update collection.', 'error');
    }
  });
  document.getElementById('btn-library-delete-selected-inline').addEventListener('click', deleteSelectedGalleryJobs);
  document.getElementById('btn-library-clear-selection-inline').addEventListener('click', () => setAllGallerySelections(false));
  document.getElementById('btn-batch-upload-cancel').addEventListener('click', () => {
    closeModal('batch-upload-modal');
  });
  document.getElementById('btn-batch-upload-apply').addEventListener('click', applyBatchUpload);
  document.getElementById('gallery-grid').addEventListener('click', (event) => {
    const target = event.target;
    const uploadButton = target.closest('[data-upload-job-id]');
    if (uploadButton) {
      const jobId = uploadButton.dataset.uploadJobId;
      const job = loadedGalleryJobs[jobId];
      window.openUploadModal(jobId, job?.prompt || jobId);
      return;
    }
    const remixButton = target.closest('[data-remix-job-id]');
    if (remixButton) {
      const jobId = remixButton.dataset.remixJobId;
      const job = loadedGalleryJobs[jobId];
      window.openRemixFromJob(jobId, job?.prompt || jobId);
      return;
    }
    const preview = target.closest('[data-preview-job-id]');
    if (preview) {
      window.showPreview(preview.dataset.previewJobId);
      return;
    }
    const tagButton = target.closest('[data-tag-job-id]');
    if (tagButton) {
      setTagsForJobs([tagButton.dataset.tagJobId]).catch((error) => {
        showToast(error?.message || 'Could not save tags.', 'error');
      });
    }
  });
  document.getElementById('btn-gallery-filter').addEventListener('click', loadGallery);
  document.getElementById('gallery-search').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') loadGallery();
  });
  document.getElementById('btn-gallery-clear-filters').addEventListener('click', () => {
    document.getElementById('gallery-search').value = '';
    document.getElementById('gallery-tag-filter').value = '';
    document.getElementById('gallery-collection-filter').value = '';
    loadGallery();
  });
  document.getElementById('btn-library-create-collection').addEventListener('click', async () => {
    const input = document.getElementById('library-collection-name');
    const name = input.value.trim();
    if (!name) return;
    try {
      const response = await apiFetch('/library/collections', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      await parseJSONResponse(response, 'Could not create collection.');
      input.value = '';
      await loadLibraryCollections();
      showToast('Collection created.', 'done');
    } catch (error) {
      showToast(error?.message || 'Could not create collection.', 'error');
    }
  });
  document.getElementById('btn-library-delete-collection').addEventListener('click', async () => {
    const select = document.getElementById('library-target-collection');
    const collectionId = select.value;
    const collection = managedCollections.find((item) => item.id === collectionId);
    if (!collection || !window.confirm('Delete collection ' + collection.name + '? Artwork is kept.')) {
      return;
    }
    try {
      const response = await apiFetch(
        '/library/collections/' + encodeURIComponent(collectionId),
        { method: 'DELETE' },
      );
      await parseJSONResponse(response, 'Could not delete collection.');
      await Promise.all([loadLibraryCollections(), loadGallery()]);
      showToast('Collection deleted; artwork was kept.', 'done');
    } catch (error) {
      showToast(error?.message || 'Could not delete collection.', 'error');
    }
  });
  document.getElementById('btn-library-history').addEventListener('click', async () => {
    const container = document.getElementById('library-history-list');
    if (container.style.display !== 'none') {
      container.style.display = 'none';
      return;
    }
    try {
      const response = await apiFetch('/library/history?limit=50');
      const history = await parseJSONResponse(response, 'Could not load display history.');
      container.innerHTML = history.length ? history.map((item) =>
        '<div class="settings-item"><div class="settings-item-main"><strong>' +
        esc(item.job_id || item.content_id || 'TV artwork') + '</strong><span>' +
        esc(item.source) + ' · ' + esc(item.tv_target || 'TV') + ' · ' +
        esc(new Date(item.displayed_at * 1000).toLocaleString()) +
        '</span></div></div>'
      ).join('') : '<div class="settings-item"><strong>No display history yet</strong></div>';
      container.style.display = 'flex';
    } catch (error) {
      showToast(error?.message || 'Could not load display history.', 'error');
    }
  });
  document.getElementById('batch-upload-tv-select').addEventListener('change', (e) => {
    loadMattesForSelect('batch-upload-matte-select', e.target.value);
  });

  const modalIds = [
    'modal',
    'matte-modal',
    'upload-modal',
    'batch-upload-modal',
    'remix-modal',
    'provider-settings-modal',
    'tv-settings-modal',
    'device-pairing-modal',
    'add-tv-modal',
    'shortcuts-modal',
  ];
  let lastFocusedBeforeModal = null;

  function openModal(modalId, focusSelector) {
    const modal = document.getElementById(modalId);
    if (!modal) return;
    lastFocusedBeforeModal = document.activeElement;
    modal.classList.add('visible');
    if (!focusSelector) return;
    const focusTarget = modal.querySelector(focusSelector);
    if (focusTarget) setTimeout(() => focusTarget.focus(), 20);
  }

  function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (!modal) return;
    modal.classList.remove('visible');
    if (lastFocusedBeforeModal && typeof lastFocusedBeforeModal.focus === 'function') {
      setTimeout(() => lastFocusedBeforeModal.focus(), 20);
    }
  }

  function closeAllModals() {
    modalIds.forEach((id) => {
      const modal = document.getElementById(id);
      if (modal) modal.classList.remove('visible');
    });
  }

  document.getElementById('btn-shortcuts').addEventListener('click', () => {
    openModal('shortcuts-modal', '#btn-shortcuts-close');
  });
  document.getElementById('btn-shortcuts-close').addEventListener('click', () => {
    closeModal('shortcuts-modal');
  });

  window.showPreview = function(jobId) {
    document.getElementById('modal-img').src = '/jobs/' + jobId + '/image';
    openModal('modal');
  };
  document.getElementById('modal').addEventListener('click', function() {
    closeModal('modal');
  });

  // =========================================================================
  // Persistent settings management
  // =========================================================================
  function accessMethodLabel(method) {
    return {
      off: 'Authentication disabled',
      token: 'API token',
      token_session: 'Token session',
      paired_device: 'Paired device',
      tailscale: 'Tailscale identity',
      trusted_lan: 'Trusted LAN',
    }[method] || 'Authenticated';
  }

  function renderAccessSettings() {
    const summary = document.getElementById('settings-access-summary');
    const list = document.getElementById('settings-device-list');
    const pairButton = document.getElementById('btn-settings-pair-device');
    if (!managedAccessSettings) {
      summary.innerHTML = '<div class="settings-item"><strong>Loading...</strong></div>';
      list.innerHTML = '';
      return;
    }

    const authState = managedAccessSettings.auth_enabled
      ? accessMethodLabel(managedAccessSettings.method)
      : 'Authentication disabled';
    const identity = managedAccessSettings.identity
      ? (' · ' + managedAccessSettings.identity)
      : '';
    const tailscale = managedAccessSettings.tailscale_auth_enabled
      ? 'Enabled'
      : 'Disabled';
    const trustedLan = managedAccessSettings.trusted_lan_cidrs?.length
      ? managedAccessSettings.trusted_lan_cidrs.join(', ')
      : 'Disabled';
    summary.innerHTML =
      '<div class="settings-item"><div class="settings-item-main"><strong>' +
      esc(authState) + '</strong><span>Current access' + esc(identity) + '</span></div></div>' +
      '<div class="settings-item"><div class="settings-item-main"><strong>Tailscale</strong><span>' +
      esc(tailscale) + '</span></div></div>' +
      '<div class="settings-item"><div class="settings-item-main"><strong>Trusted LAN</strong><span>' +
      esc(trustedLan) + '</span></div></div>';
    pairButton.disabled = !managedAccessSettings.auth_enabled;

    const devices = managedAccessSettings.devices || [];
    if (!devices.length) {
      list.innerHTML = '<div class="settings-item"><div class="settings-item-main">' +
        '<strong>No paired devices</strong><span>Pair a browser without sharing the admin token.</span>' +
        '</div></div>';
      return;
    }
    list.innerHTML = devices.map((device) => {
      const currentBadge = device.current
        ? '<span class="badge badge-frame">Current</span>'
        : '';
      const lastSeen = new Date(device.last_seen_at * 1000).toLocaleString();
      const expires = new Date(device.expires_at * 1000).toLocaleDateString();
      return '<div class="settings-item"><div class="settings-item-main"><strong>' +
        esc(device.name) + '</strong><span>Last used ' + esc(lastSeen) +
        ' · expires ' + esc(expires) + '</span></div><div class="settings-item-actions">' +
        currentBadge + '<button class="btn btn-danger btn-small" data-device-id="' +
        esc(device.id) + '">Revoke</button></div></div>';
    }).join('');
  }

  async function createDevicePairing(button) {
    setButtonBusy(button, 'Creating...');
    try {
      const response = await apiFetch('/auth/pairings', { method: 'POST' });
      const pairing = await parseJSONResponse(response, 'Could not create pairing link.');
      document.getElementById('device-pairing-qr').src = pairing.qr_data_url;
      document.getElementById('device-pairing-code').textContent = pairing.code;
      document.getElementById('device-pairing-link').value = pairing.pairing_url;
      document.getElementById('device-pairing-expiry').textContent =
        'Expires ' + new Date(pairing.expires_at * 1000).toLocaleTimeString();
      openModal('device-pairing-modal', '#btn-device-pairing-copy');
    } catch (error) {
      showToast(error?.message || 'Could not create pairing link.', 'error');
    } finally {
      clearButtonBusy(button);
    }
  }

  async function revokePairedDevice(deviceId) {
    if (!window.confirm('Revoke this device? It will need to pair or enter a token again.')) return;
    try {
      const response = await apiFetch('/auth/devices/' + encodeURIComponent(deviceId), {
        method: 'DELETE',
      });
      await parseJSONResponse(response, 'Could not revoke device.');
      await loadManagementSettings();
      showToast('Device access revoked.', 'done');
    } catch (error) {
      showToast(error?.message || 'Could not revoke device.', 'error');
    }
  }

  function setSettingsModalError(prefix, message) {
    const status = document.getElementById(prefix + '-error');
    const text = document.getElementById(prefix + '-error-text');
    text.textContent = message || '';
    status.classList.toggle('visible', Boolean(message));
  }

  // =========================================================================
  // Live Score mode
  // =========================================================================
  function renderLiveScores() {
    const groups = automationGroups || [];
    document.getElementById('live-score-group').innerHTML = groups.map((group) =>
      '<option value="' + esc(group.id) + '">' + esc(group.name) + '</option>'
    ).join('');
    document.getElementById('live-score-feed-tracker').innerHTML = liveScoreTrackers.map((tracker) =>
      '<option value="' + esc(tracker.id) + '">' + esc(tracker.name) + '</option>'
    ).join('');
    const list = document.getElementById('live-score-list');
    if (!liveScoreTrackers.length) {
      list.innerHTML = '<div class="settings-item"><span>No live-score trackers yet.</span></div>';
      return;
    }
    list.innerHTML = liveScoreTrackers.map((tracker) => {
      const group = groups.find((item) => item.id === tracker.group_id);
      const event = tracker.last_event || {};
      const score = event.home_team
        ? (event.home_team + ' ' + event.home_score + ' – ' + event.away_score + ' ' + event.away_team)
        : 'Waiting for a matching live event';
      const preview = tracker.last_rendered
        ? '<img class="live-score-preview" src="/modes/live-score/' + esc(tracker.id) +
          '/image?' + Date.now() + '" alt="Current scoreboard preview">'
        : '';
      return '<div class="settings-item"><div class="settings-item-main"><strong>' +
        esc(tracker.name) + '</strong><span>' + esc(score) + '</span><span>' +
        esc(tracker.provider) + ' · ' + esc(tracker.tracking_kind) + ': ' +
        esc(tracker.tracking_value) + ' · ' + esc(group?.name || tracker.group_id) +
        ' · ' + esc(tracker.last_status || 'new') +
        (tracker.last_error ? ' · ' + esc(tracker.last_error) : '') + '</span>' + preview +
        '</div><div class="settings-item-actions">' +
        '<button class="btn btn-secondary btn-small" data-live-score-refresh="' +
        esc(tracker.id) + '">Refresh</button>' +
        '<button class="btn btn-secondary btn-small" data-live-score-toggle="' +
        esc(tracker.id) + '" data-enabled="' + String(tracker.enabled) + '">' +
        (tracker.enabled ? 'Pause' : 'Resume') + '</button>' +
        '<button class="btn btn-danger btn-small" data-live-score-delete="' +
        esc(tracker.id) + '">Delete</button></div></div>';
    }).join('');
  }

  async function loadLiveScores(triggerButton) {
    if (triggerButton) setButtonBusy(triggerButton, 'Refreshing...');
    try {
      const [trackerResponse, groupResponse] = await Promise.all([
        apiFetch('/modes/live-score'),
        apiFetch('/automation/groups'),
      ]);
      liveScoreTrackers = await parseJSONResponse(
        trackerResponse,
        'Could not load live-score trackers.',
      );
      automationGroups = await parseJSONResponse(groupResponse, 'Could not load TV groups.');
      renderLiveScores();
    } finally {
      if (triggerButton) clearButtonBusy(triggerButton);
    }
  }

  document.getElementById('btn-modes-refresh').addEventListener('click', (event) => {
    loadLiveScores(event.currentTarget).catch((error) => showToast(error.message, 'error'));
  });
  document.getElementById('live-score-provider').addEventListener('change', (event) => {
    const keyInput = document.getElementById('live-score-key');
    keyInput.disabled = event.target.value === 'manual';
    keyInput.placeholder = event.target.value === 'manual'
      ? 'Not used for a manual feed'
      : 'Required for TheSportsDB';
  });
  document.getElementById('btn-live-score-create').addEventListener('click', async (event) => {
    const provider = document.getElementById('live-score-provider').value;
    const body = {
      name: document.getElementById('live-score-name').value.trim(),
      provider,
      tracking_kind: document.getElementById('live-score-kind').value,
      tracking_value: document.getElementById('live-score-value').value.trim(),
      group_id: document.getElementById('live-score-group').value,
      poll_seconds: Number(document.getElementById('live-score-poll').value),
      refresh_seconds: Number(document.getElementById('live-score-refresh').value),
      theme: document.getElementById('live-score-theme').value,
      enabled: true,
    };
    const apiKey = document.getElementById('live-score-key').value.trim();
    if (apiKey) body.api_key = apiKey;
    if (!body.name || !body.tracking_value || !body.group_id) {
      showToast('Enter a name and target, and choose a TV group.', 'warn'); return;
    }
    setButtonBusy(event.currentTarget, 'Creating...');
    try {
      const response = await apiFetch('/modes/live-score', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
      });
      await parseJSONResponse(response, 'Could not create live-score tracker.');
      document.getElementById('live-score-name').value = '';
      document.getElementById('live-score-key').value = '';
      await loadLiveScores();
      showToast('Live-score tracker created.', 'done');
    } catch (error) { showToast(error.message, 'error'); }
    finally { clearButtonBusy(event.currentTarget); }
  });
  document.getElementById('btn-live-score-feed').addEventListener('click', async (event) => {
    const trackerId = document.getElementById('live-score-feed-tracker').value;
    const progress = document.getElementById('live-score-feed-progress').value.trim() || 'Live';
    const body = {
      event_id: document.getElementById('live-score-feed-event').value.trim(),
      league: document.getElementById('live-score-feed-league').value.trim(),
      sport: document.getElementById('live-score-feed-sport').value.trim(),
      home_team: document.getElementById('live-score-feed-home').value.trim(),
      away_team: document.getElementById('live-score-feed-away').value.trim(),
      home_score: document.getElementById('live-score-feed-home-score').value.trim() || '-',
      away_score: document.getElementById('live-score-feed-away-score').value.trim() || '-',
      status: progress,
      progress,
      highlights: document.getElementById('live-score-feed-highlights').value
        .split('\n').map((item) => item.trim()).filter(Boolean),
    };
    if (!trackerId || !body.event_id || !body.league || !body.home_team || !body.away_team) {
      showToast('Choose a tracker and complete event, league, and team names.', 'warn'); return;
    }
    setButtonBusy(event.currentTarget, 'Sending...');
    try {
      const response = await apiFetch('/modes/live-score/' + trackerId + '/feed', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
      });
      const result = await parseJSONResponse(response, 'Could not apply score update.');
      await loadLiveScores();
      showToast('Score update ' + result.status + '.', result.status === 'error' ? 'error' : 'done');
    } catch (error) { showToast(error.message, 'error'); }
    finally { clearButtonBusy(event.currentTarget); }
  });
  document.getElementById('live-score-list').addEventListener('click', async (event) => {
    const refresh = event.target.closest('[data-live-score-refresh]');
    const toggle = event.target.closest('[data-live-score-toggle]');
    const remove = event.target.closest('[data-live-score-delete]');
    const button = refresh || toggle || remove;
    if (!button) return;
    setButtonBusy(button, refresh ? 'Refreshing...' : 'Saving...');
    try {
      if (refresh) {
        const response = await apiFetch('/modes/live-score/' + refresh.dataset.liveScoreRefresh + '/refresh', {method: 'POST'});
        const result = await parseJSONResponse(response, 'Score refresh failed.');
        showToast('Live score ' + result.status + '.', result.status === 'error' ? 'error' : 'done');
      } else if (toggle) {
        const response = await apiFetch('/modes/live-score/' + toggle.dataset.liveScoreToggle + '/enabled', {
          method: 'PUT', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({enabled: toggle.dataset.enabled !== 'true'}),
        });
        await parseJSONResponse(response, 'Could not update tracker.');
      } else if (remove) {
        if (!window.confirm('Delete this live-score tracker and its current TV image?')) return;
        const response = await apiFetch('/modes/live-score/' + remove.dataset.liveScoreDelete, {method: 'DELETE'});
        await parseJSONResponse(response, 'Could not delete tracker.');
      }
      await loadLiveScores();
    } catch (error) { showToast(error.message, 'error'); }
    finally { clearButtonBusy(button); }
  });

  // =========================================================================
  // TV groups, playlists, schedules, and integration hooks
  // =========================================================================
  function renderAutomationTVChoices() {
    const container = document.getElementById('automation-group-tvs');
    if (!container) return;
    if (!managedTVSettings.length) {
      container.innerHTML = '<span class="empty">Add a persistent TV in Settings first.</span>';
      return;
    }
    container.innerHTML = managedTVSettings.map((tv) =>
      '<label><input type="checkbox" value="' + esc(tv.profile_id) + '">' +
      '<span>' + esc(tv.profile_id) + ' · ' + esc(tv.ip) + '</span></label>'
    ).join('');
  }

  function renderAutomationState() {
    const groupList = document.getElementById('automation-group-list');
    groupList.innerHTML = automationGroups.length ? automationGroups.map((group) =>
      '<div class="settings-item"><div class="settings-item-main"><strong>' + esc(group.name) +
      '</strong><span>' + esc(group.tv_profile_ids.join(', ')) + '</span></div>' +
      '<div class="settings-item-actions"><button class="btn btn-danger btn-small" ' +
      'data-automation-delete="group" data-id="' + esc(group.id) + '">Delete</button></div></div>'
    ).join('') : '<div class="settings-item"><span>No TV groups yet.</span></div>';

    const playlistList = document.getElementById('automation-playlist-list');
    playlistList.innerHTML = automationPlaylists.length ? automationPlaylists.map((playlist) =>
      '<div class="settings-item"><div class="settings-item-main"><strong>' + esc(playlist.name) +
      '</strong><span>' + playlist.job_ids.length + ' artwork(s)</span></div>' +
      '<div class="settings-item-actions"><button class="btn btn-danger btn-small" ' +
      'data-automation-delete="playlist" data-id="' + esc(playlist.id) + '">Delete</button></div></div>'
    ).join('') : '<div class="settings-item"><span>No playlists yet.</span></div>';

    const playlistSelect = document.getElementById('automation-schedule-playlist');
    playlistSelect.innerHTML = automationPlaylists.map((item) =>
      '<option value="' + esc(item.id) + '">' + esc(item.name) + '</option>'
    ).join('');
    const groupSelect = document.getElementById('automation-schedule-group');
    groupSelect.innerHTML = automationGroups.map((item) =>
      '<option value="' + esc(item.id) + '">' + esc(item.name) + '</option>'
    ).join('');

    const scheduleList = document.getElementById('automation-schedule-list');
    scheduleList.innerHTML = automationSchedules.length ? automationSchedules.map((schedule) => {
      const playlist = automationPlaylists.find((item) => item.id === schedule.playlist_id);
      const group = automationGroups.find((item) => item.id === schedule.group_id);
      const last = schedule.last_status
        ? ('Last: ' + schedule.last_status + (schedule.last_error ? ' · ' + schedule.last_error : ''))
        : 'Not run yet';
      return '<div class="settings-item"><div class="settings-item-main"><strong>' +
        esc(schedule.name) + '</strong><span>' + esc(playlist?.name || schedule.playlist_id) +
        ' → ' + esc(group?.name || schedule.group_id) + ' · every ' +
        esc(String(schedule.interval_seconds)) + 's · ' + esc(last) + '</span></div>' +
        '<div class="settings-item-actions"><button class="btn btn-secondary btn-small" ' +
        'data-automation-run="' + esc(schedule.id) + '">Run Now</button>' +
        '<button class="btn btn-secondary btn-small" data-automation-toggle="' +
        esc(schedule.id) + '" data-enabled="' + String(schedule.enabled) + '">' +
        (schedule.enabled ? 'Pause' : 'Resume') + '</button>' +
        '<button class="btn btn-danger btn-small" data-automation-delete="schedule" data-id="' +
        esc(schedule.id) + '">Delete</button></div></div>';
    }).join('') : '<div class="settings-item"><span>No schedules yet.</span></div>';

    const webhookList = document.getElementById('automation-webhook-list');
    webhookList.innerHTML = automationWebhooks.length ? automationWebhooks.map((webhook) =>
      '<div class="settings-item"><div class="settings-item-main"><strong>' + esc(webhook.name) +
      '</strong><span>' + esc(webhook.url) + ' · ' + esc(webhook.events.join(', ')) + '</span></div>' +
      '<div class="settings-item-actions"><button class="btn btn-danger btn-small" ' +
      'data-automation-delete="webhook" data-id="' + esc(webhook.id) + '">Delete</button></div></div>'
    ).join('') : '<div class="settings-item"><span>No outbound webhooks.</span></div>';

    const mqtt = automationStatus?.mqtt || {};
    document.getElementById('automation-integration-status').innerHTML =
      '<div class="settings-item"><div class="settings-item-main"><strong>Scheduler</strong><span>' +
      (automationStatus?.scheduler_running ? 'Running' : 'Stopped') + '</span></div></div>' +
      '<div class="settings-item"><div class="settings-item-main"><strong>MQTT</strong><span>' +
      (mqtt.configured
        ? ('Configured for ' + esc(mqtt.broker || 'broker') +
          (mqtt.dependency_installed ? '' : ' · install frameart[integrations]'))
        : 'Set FRAMEART_MQTT_BROKER to publish schedule events') +
      '</span></div></div>';
  }

  async function loadAutomations(triggerButton) {
    if (triggerButton) setButtonBusy(triggerButton, 'Refreshing...');
    try {
      const responses = await Promise.all([
        apiFetch('/automation/groups'),
        apiFetch('/automation/playlists'),
        apiFetch('/automation/schedules'),
        apiFetch('/automation/webhooks'),
        apiFetch('/automation/status'),
        apiFetch('/jobs?limit=200'),
      ]);
      const payloads = await Promise.all(responses.map((response) =>
        parseJSONResponse(response, 'Could not load automation data.')
      ));
      [automationGroups, automationPlaylists, automationSchedules, automationWebhooks,
        automationStatus] = payloads;
      const jobs = payloads[5] || [];
      document.getElementById('automation-playlist-jobs').innerHTML = jobs.map((job) =>
        '<option value="' + esc(job.job_id) + '">' + esc(job.prompt || job.job_id) +
        ' · ' + esc(job.job_id) + '</option>'
      ).join('');
      renderAutomationTVChoices();
      renderAutomationState();
    } finally {
      if (triggerButton) clearButtonBusy(triggerButton);
    }
  }

  async function writeAutomation(url, method, body, fallback) {
    const response = await apiFetch(url, {
      method,
      headers: body ? {'Content-Type': 'application/json'} : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    return parseJSONResponse(response, fallback);
  }

  document.getElementById('btn-automation-refresh').addEventListener('click', (event) => {
    loadAutomations(event.currentTarget).catch((error) => showToast(error.message, 'error'));
  });
  document.getElementById('btn-automation-group-create').addEventListener('click', async (event) => {
    const name = document.getElementById('automation-group-name').value.trim();
    const ids = [...document.querySelectorAll('#automation-group-tvs input:checked')]
      .map((input) => input.value);
    if (!name || !ids.length) { showToast('Enter a group name and select at least one TV.', 'warn'); return; }
    setButtonBusy(event.currentTarget, 'Creating...');
    try {
      await writeAutomation('/automation/groups', 'POST', {name, tv_profile_ids: ids}, 'Could not create group.');
      document.getElementById('automation-group-name').value = '';
      await loadAutomations();
      showToast('TV group created.', 'done');
    } catch (error) { showToast(error.message, 'error'); }
    finally { clearButtonBusy(event.currentTarget); }
  });
  document.getElementById('btn-automation-playlist-create').addEventListener('click', async (event) => {
    const name = document.getElementById('automation-playlist-name').value.trim();
    const jobIds = [...document.getElementById('automation-playlist-jobs').selectedOptions]
      .map((option) => option.value);
    if (!name || !jobIds.length) { showToast('Enter a playlist name and select artwork.', 'warn'); return; }
    setButtonBusy(event.currentTarget, 'Creating...');
    try {
      await writeAutomation('/automation/playlists', 'POST', {name, job_ids: jobIds}, 'Could not create playlist.');
      document.getElementById('automation-playlist-name').value = '';
      await loadAutomations();
      showToast('Playlist created.', 'done');
    } catch (error) { showToast(error.message, 'error'); }
    finally { clearButtonBusy(event.currentTarget); }
  });
  document.getElementById('btn-automation-schedule-create').addEventListener('click', async (event) => {
    const body = {
      name: document.getElementById('automation-schedule-name').value.trim(),
      playlist_id: document.getElementById('automation-schedule-playlist').value,
      group_id: document.getElementById('automation-schedule-group').value,
      interval_seconds: Number(document.getElementById('automation-schedule-interval').value),
      enabled: true,
    };
    if (!body.name || !body.playlist_id || !body.group_id) {
      showToast('Enter a name and create a playlist and TV group first.', 'warn'); return;
    }
    setButtonBusy(event.currentTarget, 'Creating...');
    try {
      await writeAutomation('/automation/schedules', 'POST', body, 'Could not create schedule.');
      document.getElementById('automation-schedule-name').value = '';
      await loadAutomations();
      showToast('Schedule created.', 'done');
    } catch (error) { showToast(error.message, 'error'); }
    finally { clearButtonBusy(event.currentTarget); }
  });
  document.getElementById('btn-automation-webhook-create').addEventListener('click', async (event) => {
    const name = document.getElementById('automation-webhook-name').value.trim();
    const url = document.getElementById('automation-webhook-url').value.trim();
    if (!name || !url) { showToast('Enter a webhook name and URL.', 'warn'); return; }
    setButtonBusy(event.currentTarget, 'Adding...');
    try {
      const created = await writeAutomation('/automation/webhooks', 'POST', {
        name, url, events: [
          'schedule.completed', 'schedule.partial', 'schedule.failed',
          'live_score.displayed', 'live_score.partial', 'live_score.error', 'integration.test',
        ],
      }, 'Could not add webhook.');
      window.alert('Save this webhook signing secret now; it will not be shown again:\n\n' + created.secret);
      document.getElementById('automation-webhook-name').value = '';
      document.getElementById('automation-webhook-url').value = '';
      await loadAutomations();
    } catch (error) { showToast(error.message, 'error'); }
    finally { clearButtonBusy(event.currentTarget); }
  });
  document.getElementById('btn-automation-webhook-test').addEventListener('click', async (event) => {
    setButtonBusy(event.currentTarget, 'Sending...');
    try {
      const result = await writeAutomation('/automation/webhooks/test', 'POST', null, 'Webhook test failed.');
      const failed = result.deliveries.filter((item) => !item.ok).length;
      showToast(failed ? (failed + ' webhook delivery(s) failed.') : 'Webhook test delivered.', failed ? 'error' : 'done');
    } catch (error) { showToast(error.message, 'error'); }
    finally { clearButtonBusy(event.currentTarget); }
  });
  document.getElementById('panel-automations').addEventListener('click', async (event) => {
    const run = event.target.closest('[data-automation-run]');
    const toggle = event.target.closest('[data-automation-toggle]');
    const remove = event.target.closest('[data-automation-delete]');
    if (!run && !toggle && !remove) return;
    const button = run || toggle || remove;
    setButtonBusy(button, run ? 'Running...' : 'Saving...');
    try {
      if (run) {
        const result = await writeAutomation('/automation/schedules/' + run.dataset.automationRun + '/run', 'POST', null, 'Schedule run failed.');
        showToast('Schedule ' + result.status + ' for ' + result.job_id + '.', result.status === 'completed' ? 'done' : 'warn');
      } else if (toggle) {
        await writeAutomation('/automation/schedules/' + toggle.dataset.automationToggle + '/enabled', 'PUT', {
          enabled: toggle.dataset.enabled !== 'true',
        }, 'Could not update schedule.');
      } else {
        const paths = {group: 'groups', playlist: 'playlists', schedule: 'schedules', webhook: 'webhooks'};
        await writeAutomation('/automation/' + paths[remove.dataset.automationDelete] + '/' + remove.dataset.id, 'DELETE', null, 'Could not delete automation.');
      }
      await loadAutomations();
    } catch (error) { showToast(error.message, 'error'); }
    finally { clearButtonBusy(button); }
  });

  async function reloadRuntimeProviders() {
    const response = await apiFetch('/providers');
    refreshProviderSelects(await parseJSONResponse(response, 'Could not reload providers.'));
  }

  async function reloadConfiguredTVs() {
    const response = await apiFetch('/tv/configured');
    const configured = await parseJSONResponse(response, 'Could not reload configured TVs.');
    knownTVs = knownTVs.filter((tv) => tv.source !== 'config');
    addTVs(configured.map((tv) => ({
      ip: tv.ip,
      name: tv.name,
      profile_id: tv.name,
      model: null,
      frame_tv: true,
    })), 'config');
  }

  async function loadManagementSettings(triggerButton) {
    if (triggerButton) setButtonBusy(triggerButton, 'Refreshing...');
    try {
      const [providerResponse, tvResponse, backupResponse, accessResponse] = await Promise.all([
        apiFetch('/settings/providers'),
        apiFetch('/settings/tvs'),
        apiFetch('/settings/backups'),
        apiFetch('/auth/access'),
      ]);
      managedProviderSettings = await parseJSONResponse(
        providerResponse,
        'Could not load provider settings.',
      );
      const tvPayload = await parseJSONResponse(tvResponse, 'Could not load TV settings.');
      const backupPayload = await parseJSONResponse(
        backupResponse,
        'Could not load settings backups.',
      );
      managedAccessSettings = await parseJSONResponse(
        accessResponse,
        'Could not load access settings.',
      );
      managedTVSettings = tvPayload.tvs || [];
      managedSettingsBackups = backupPayload.backups || [];
      renderSettingsProviders();
      renderSettingsTVSummary();
      renderSettingsBackups();
      renderAccessSettings();
      renderAutomationTVChoices();
    } catch (error) {
      const message = error?.message || 'Settings could not be loaded.';
      document.getElementById('settings-provider-list').innerHTML =
        '<div class="settings-item"><strong>Unavailable</strong><span>' + esc(message) + '</span></div>';
      document.getElementById('settings-tv-list').innerHTML =
        '<div class="settings-item"><strong>Unavailable</strong><span>' + esc(message) + '</span></div>';
      document.getElementById('settings-backup-list').innerHTML =
        '<div class="settings-item"><strong>Unavailable</strong><span>' + esc(message) + '</span></div>';
      document.getElementById('settings-access-summary').innerHTML =
        '<div class="settings-item"><strong>Unavailable</strong><span>' + esc(message) + '</span></div>';
      document.getElementById('settings-device-list').innerHTML = '';
      showToast('Settings management: ' + message, 'error');
    } finally {
      if (triggerButton) clearButtonBusy(triggerButton);
    }
  }

  function renderSettingsBackups() {
    const container = document.getElementById('settings-backup-list');
    if (!managedSettingsBackups.length) {
      container.innerHTML = '<div class="settings-item"><div class="settings-item-main">' +
        '<strong>No backups yet</strong><span>Create one before a major configuration change.</span>' +
        '</div></div>';
      return;
    }
    container.innerHTML = managedSettingsBackups.map((backup, index) =>
      '<div class="settings-item"><div class="settings-item-main">' +
      '<strong>' + esc(backup.created_at || backup.backup_id) + '</strong>' +
      '<span>' + esc(backup.reason || 'snapshot') + ' · ' + esc(backup.backup_id) + '</span>' +
      '</div><div class="settings-item-actions">' +
      '<button class="btn btn-secondary btn-small" data-backup-index="' + index + '">Restore</button>' +
      '</div></div>'
    ).join('');
  }

  async function downloadAdminFile(url, fallbackName) {
    const response = await apiFetch(url);
    if (!response.ok) throw new Error(await readApiError(response, 'Download failed.'));
    const disposition = response.headers.get('content-disposition') || '';
    const match = disposition.match(/filename="?([^";]+)"?/i);
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = objectUrl;
    anchor.download = match?.[1] || fallbackName;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(objectUrl);
  }

  async function runSettingsDiagnostics(button) {
    setButtonBusy(button, 'Checking...');
    try {
      const response = await apiFetch('/settings/diagnostics');
      const diagnostics = await parseJSONResponse(response, 'Diagnostics failed.');
      const checks = diagnostics.checks || [];
      document.getElementById('settings-diagnostics-list').innerHTML = checks.map((check) =>
        '<div class="settings-item"><div class="settings-item-main"><strong>' +
        esc(check.name.replaceAll('_', ' ')) + '</strong><span>' + esc(check.status) +
        '</span></div><span class="badge ' +
        (check.status === 'error' ? 'badge-error' : 'badge-frame') + '">' +
        esc(check.status) + '</span></div>'
      ).join('');
      showToast(
        diagnostics.status === 'ok' ? 'Local diagnostics passed.' : 'Diagnostics found a problem.',
        diagnostics.status === 'ok' ? 'done' : 'error',
      );
    } catch (error) {
      document.getElementById('settings-diagnostics-list').innerHTML =
        '<div class="settings-item"><strong>Diagnostics failed</strong><span>' +
        esc(error?.message || 'Unknown error') + '</span></div>';
      showToast(error?.message || 'Diagnostics failed.', 'error');
    } finally {
      clearButtonBusy(button);
    }
  }

  document.getElementById('btn-settings-diagnostics').addEventListener('click', (event) => {
    runSettingsDiagnostics(event.currentTarget);
  });
  document.getElementById('btn-settings-support').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    setButtonBusy(button, 'Preparing...');
    try {
      await downloadAdminFile('/settings/diagnostics/support-bundle', 'frameart-support.json');
      showToast('Redacted support bundle downloaded.', 'done');
    } catch (error) {
      showToast(error?.message || 'Support bundle download failed.', 'error');
    } finally {
      clearButtonBusy(button);
    }
  });
  document.getElementById('btn-settings-export').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    setButtonBusy(button, 'Exporting...');
    try {
      await downloadAdminFile('/settings/export', 'frameart-settings.json');
      showToast('Non-secret settings exported.', 'done');
    } catch (error) {
      showToast(error?.message || 'Settings export failed.', 'error');
    } finally {
      clearButtonBusy(button);
    }
  });
  document.getElementById('btn-settings-backup').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    setButtonBusy(button, 'Creating...');
    try {
      const response = await apiFetch('/settings/backups', { method: 'POST' });
      const backup = await parseJSONResponse(response, 'Could not create backup.');
      managedSettingsBackups = [backup, ...managedSettingsBackups].slice(0, 20);
      renderSettingsBackups();
      showToast('Settings backup created.', 'done');
    } catch (error) {
      showToast(error?.message || 'Could not create backup.', 'error');
    } finally {
      clearButtonBusy(button);
    }
  });
  document.getElementById('settings-backup-list').addEventListener('click', async (event) => {
    const button = event.target.closest('[data-backup-index]');
    if (!button) return;
    const backup = managedSettingsBackups[Number(button.dataset.backupIndex)];
    if (!backup || !window.confirm(
      'Restore settings backup ' + backup.backup_id + '? Current settings will be backed up first.',
    )) return;
    setButtonBusy(button, 'Restoring...');
    try {
      const response = await apiFetch(
        '/settings/backups/' + encodeURIComponent(backup.backup_id) + '/restore',
        { method: 'POST' },
      );
      await parseJSONResponse(response, 'Could not restore backup.');
      await loadManagementSettings();
      await Promise.all([reloadRuntimeProviders(), reloadConfiguredTVs()]);
      showToast('Settings backup restored.', 'done');
    } catch (error) {
      showToast(error?.message || 'Could not restore backup.', 'error');
    } finally {
      clearButtonBusy(button);
    }
  });
  document.getElementById('btn-settings-import').addEventListener('click', () => {
    document.getElementById('settings-import-file').click();
  });
  document.getElementById('settings-import-file').addEventListener('change', async (event) => {
    const input = event.currentTarget;
    const file = input.files?.[0];
    if (!file) return;
    try {
      const payload = JSON.parse(await file.text());
      if (!payload || typeof payload !== 'object' || typeof payload.settings !== 'object') {
        throw new Error('Select a FrameArt settings export JSON file.');
      }
      if (!window.confirm(
        'Import these non-secret settings? Provider keys are preserved and a backup is created first.',
      )) return;
      const response = await apiFetch('/settings/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      await parseJSONResponse(response, 'Settings import failed.');
      await loadManagementSettings();
      await Promise.all([reloadRuntimeProviders(), reloadConfiguredTVs()]);
      showToast('Settings imported.', 'done');
    } catch (error) {
      showToast(error?.message || 'Settings import failed.', 'error');
    } finally {
      input.value = '';
    }
  });

  function openProviderSettingsEditor(provider) {
    editingProviderName = provider?.name || null;
    const nameSelect = document.getElementById('provider-settings-name');
    const configured = new Set(
      (managedProviderSettings?.providers || []).map((item) => item.name),
    );
    const choices = editingProviderName
      ? [editingProviderName]
      : (managedProviderSettings?.available_types || []).filter((name) => !configured.has(name));
    if (!choices.length) {
      showToast('All available provider types are already configured.', 'warn');
      return;
    }
    nameSelect.innerHTML = choices.map((name) =>
      '<option value="' + esc(name) + '">' + esc(name) + '</option>'
    ).join('');
    nameSelect.disabled = Boolean(editingProviderName);
    document.getElementById('provider-settings-title').textContent =
      editingProviderName ? ('Edit ' + editingProviderName) : 'Add Provider';
    document.getElementById('provider-settings-base-url').value = provider?.base_url || '';
    document.getElementById('provider-settings-model').value = provider?.model || '';
    document.getElementById('provider-settings-timeout').value = provider?.timeout || 120;
    document.getElementById('provider-settings-models').value =
      (provider?.models || []).join('\n');
    document.getElementById('provider-settings-api-key').value = '';
    const clearKey = document.getElementById('provider-settings-clear-key');
    clearKey.checked = false;
    clearKey.disabled = provider?.api_key_source === 'environment' || !provider?.has_api_key;
    const keyState = provider?.has_api_key
      ? ('A key is configured via ' + provider.api_key_source + '. Leave blank to keep it.')
      : 'No API key is currently configured.';
    document.getElementById('provider-settings-key-state').textContent = keyState;
    setSettingsModalError('provider-settings', '');
    openModal('provider-settings-modal', '#provider-settings-name');
  }

  async function saveProviderSettings() {
    const button = document.getElementById('btn-provider-settings-save');
    const name = document.getElementById('provider-settings-name').value;
    const apiKey = document.getElementById('provider-settings-api-key').value.trim();
    const models = document.getElementById('provider-settings-models').value
      .split(/[\n,]+/)
      .map((model) => model.trim())
      .filter(Boolean);
    const payload = {
      base_url: document.getElementById('provider-settings-base-url').value.trim() || null,
      model: document.getElementById('provider-settings-model').value.trim() || null,
      timeout: Number(document.getElementById('provider-settings-timeout').value),
      models,
      clear_api_key: document.getElementById('provider-settings-clear-key').checked,
    };
    if (apiKey) payload.api_key = apiKey;
    if (!editingProviderName) payload.name = name;

    setSettingsModalError('provider-settings', '');
    setButtonBusy(button, 'Saving...');
    try {
      const endpoint = editingProviderName
        ? ('/settings/providers/' + encodeURIComponent(editingProviderName))
        : '/settings/providers';
      const response = await apiFetch(endpoint, {
        method: editingProviderName ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      managedProviderSettings = await parseJSONResponse(response, 'Could not save provider.');
      renderSettingsProviders();
      closeModal('provider-settings-modal');
      await reloadRuntimeProviders();
      showToast('Provider settings saved.', 'done');
    } catch (error) {
      setSettingsModalError('provider-settings', error?.message || 'Could not save provider.');
    } finally {
      clearButtonBusy(button);
    }
  }

  document.getElementById('settings-provider-list').addEventListener('click', async (event) => {
    const button = event.target.closest('[data-provider-action]');
    if (!button) return;
    const provider = managedProviderSettings?.providers?.[Number(button.dataset.providerIndex)];
    if (!provider) return;
    const action = button.dataset.providerAction;
    if (action === 'edit') {
      openProviderSettingsEditor(provider);
      return;
    }
    if (action === 'delete') {
      if (!window.confirm('Delete provider ' + provider.name + ' and its managed key?')) return;
      setButtonBusy(button, 'Deleting...');
      try {
        const response = await apiFetch(
          '/settings/providers/' + encodeURIComponent(provider.name),
          { method: 'DELETE' },
        );
        managedProviderSettings = await parseJSONResponse(response, 'Could not delete provider.');
        renderSettingsProviders();
        await reloadRuntimeProviders();
        showToast('Provider deleted.', 'done');
      } catch (error) {
        showToast(error?.message || 'Could not delete provider.', 'error');
      } finally {
        clearButtonBusy(button);
      }
      return;
    }
    if (action === 'test') {
      setButtonBusy(button, 'Testing...');
      try {
        const response = await apiFetch(
          '/settings/providers/' + encodeURIComponent(provider.name) + '/test',
          { method: 'POST' },
        );
        const result = await parseJSONResponse(response, 'Provider test failed.');
        showToast(result.detail, result.ok ? 'done' : 'error');
      } catch (error) {
        showToast(error?.message || 'Provider test failed.', 'error');
      } finally {
        clearButtonBusy(button);
      }
    }
  });

  document.getElementById('btn-settings-add-provider').addEventListener('click', () => {
    openProviderSettingsEditor(null);
  });
  document.getElementById('btn-provider-settings-cancel').addEventListener('click', () => {
    closeModal('provider-settings-modal');
  });
  document.getElementById('btn-provider-settings-save').addEventListener(
    'click',
    saveProviderSettings,
  );

  document.getElementById('btn-settings-save-defaults').addEventListener('click', async () => {
    const button = document.getElementById('btn-settings-save-defaults');
    setButtonBusy(button, 'Saving...');
    try {
      const response = await apiFetch('/settings/defaults', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider: document.getElementById('settings-default-provider').value,
          model: document.getElementById('settings-default-model').value.trim() || null,
        }),
      });
      managedProviderSettings = await parseJSONResponse(response, 'Could not save defaults.');
      renderSettingsProviders();
      await reloadRuntimeProviders();
      showToast('Generation defaults saved.', 'done');
    } catch (error) {
      showToast(error?.message || 'Could not save defaults.', 'error');
    } finally {
      clearButtonBusy(button);
    }
  });

  function suggestedProfileId(name) {
    const candidate = (name || 'frame_tv').toLowerCase()
      .replace(/[^a-z0-9_-]+/g, '_')
      .replace(/^_+|_+$/g, '')
      .slice(0, 64);
    return candidate || 'frame_tv';
  }

  function openTVSettingsEditor(tv, discoveredTV) {
    editingTVProfileId = tv?.profile_id || null;
    const profileId = document.getElementById('tv-settings-profile-id');
    profileId.disabled = false;
    profileId.value = editingTVProfileId || suggestedProfileId(discoveredTV?.name);
    document.getElementById('tv-settings-title').textContent =
      editingTVProfileId ? ('Edit ' + editingTVProfileId) : 'Add TV';
    document.getElementById('tv-settings-ip').value = tv?.ip || discoveredTV?.ip || '';
    document.getElementById('tv-settings-port').value = tv?.port || 8002;
    document.getElementById('tv-settings-client-name').value = tv?.client_name || 'FrameArt';
    document.getElementById('tv-settings-ssl').checked = tv ? tv.ssl : true;
    setSettingsModalError('tv-settings', '');
    openModal('tv-settings-modal', '#tv-settings-profile-id');
  }

  async function saveTVSettings() {
    const button = document.getElementById('btn-tv-settings-save');
    const profileId = document.getElementById('tv-settings-profile-id').value.trim();
    const ip = normalizePrivateIPv4(document.getElementById('tv-settings-ip').value);
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(profileId)) {
      setSettingsModalError('tv-settings', 'Profile ID may contain letters, numbers, _ and -.');
      return;
    }
    if (!ip) {
      setSettingsModalError('tv-settings', 'Enter an RFC1918 private IPv4 address.');
      return;
    }
    const payload = {
      ip,
      port: Number(document.getElementById('tv-settings-port').value),
      client_name: document.getElementById('tv-settings-client-name').value.trim(),
      ssl: document.getElementById('tv-settings-ssl').checked,
    };
    payload.profile_id = profileId;

    setSettingsModalError('tv-settings', '');
    setButtonBusy(button, 'Saving...');
    try {
      const endpoint = editingTVProfileId
        ? ('/settings/tvs/' + encodeURIComponent(editingTVProfileId))
        : '/settings/tvs';
      let response = await apiFetch(endpoint, {
        method: editingTVProfileId ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (response.status === 409 && !editingTVProfileId) {
        const conflictPayload = await response.json().catch(() => null);
        const conflict = conflictPayload?.detail;
        if (conflict?.code === 'tv_profile_conflict' && conflict.existing_profile_id) {
          const confirmed = window.confirm(
            conflict.message + ' Update that profile and rename it to ' + profileId + '?',
          );
          if (!confirmed) throw new Error(conflict.message);
          response = await apiFetch(
            '/settings/tvs/' + encodeURIComponent(conflict.existing_profile_id),
            {
              method: 'PUT',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(payload),
            },
          );
        } else {
          throw new Error('A matching TV profile already exists.');
        }
      }
      const result = await parseJSONResponse(response, 'Could not save TV.');
      managedTVSettings = result.tvs || [];
      renderSettingsTVSummary();
      closeModal('tv-settings-modal');
      await reloadConfiguredTVs();
      showToast('TV profile saved.', 'done');
    } catch (error) {
      setSettingsModalError('tv-settings', error?.message || 'Could not save TV.');
    } finally {
      clearButtonBusy(button);
    }
  }

  document.getElementById('settings-tv-list').addEventListener('click', async (event) => {
    const button = event.target.closest('[data-settings-tv-action]');
    if (!button) return;
    const tv = managedTVSettings[Number(button.dataset.settingsTvIndex)];
    if (!tv) return;
    const action = button.dataset.settingsTvAction;
    if (action === 'edit') {
      openTVSettingsEditor(tv, null);
      return;
    }
    if (action === 'delete') {
      if (!window.confirm('Delete TV profile ' + tv.profile_id + '? Pairing tokens are retained.')) return;
      setButtonBusy(button, 'Deleting...');
      try {
        const response = await apiFetch(
          '/settings/tvs/' + encodeURIComponent(tv.profile_id),
          { method: 'DELETE' },
        );
        const result = await parseJSONResponse(response, 'Could not delete TV.');
        managedTVSettings = result.tvs || [];
        renderSettingsTVSummary();
        await reloadConfiguredTVs();
        showToast('TV profile deleted.', 'done');
      } catch (error) {
        showToast(error?.message || 'Could not delete TV.', 'error');
      } finally {
        clearButtonBusy(button);
      }
      return;
    }
    if (action === 'consolidate') {
      if (!window.confirm(
        'Keep ' + tv.profile_id + ' and remove its duplicate aliases? Pairing tokens are retained.',
      )) return;
      setButtonBusy(button, 'Consolidating...');
      try {
        const response = await apiFetch(
          '/settings/tvs/' + encodeURIComponent(tv.profile_id) + '/consolidate',
          { method: 'POST' },
        );
        const result = await parseJSONResponse(response, 'Could not consolidate TV profiles.');
        managedTVSettings = result.tvs || [];
        renderSettingsTVSummary();
        await reloadConfiguredTVs();
        renderAutomationTVChoices();
        showToast('Duplicate TV profiles consolidated.', 'done');
      } catch (error) {
        showToast(error?.message || 'Could not consolidate TV profiles.', 'error');
      } finally {
        clearButtonBusy(button);
      }
      return;
    }
    if (action === 'pair' && !window.confirm(
      'Start pairing with ' + tv.profile_id + '? Accept the prompt shown on the TV.',
    )) return;

    setButtonBusy(button, action === 'pair' ? 'Pairing...' : 'Testing...');
    try {
      const response = await apiFetch(
        '/settings/tvs/' + encodeURIComponent(tv.profile_id) + '/' + action,
        { method: action === 'pair' ? 'POST' : 'GET' },
      );
      const result = await parseJSONResponse(response, 'TV ' + action + ' failed.');
      showToast(result.detail, result.ok ? 'done' : 'error');
      if (action === 'pair' && result.ok) await loadManagementSettings();
    } catch (error) {
      showToast(error?.message || ('TV ' + action + ' failed.'), 'error');
    } finally {
      clearButtonBusy(button);
    }
  });

  document.getElementById('btn-settings-add-tv').addEventListener('click', () => {
    openTVSettingsEditor(null, null);
  });
  document.getElementById('btn-tv-settings-cancel').addEventListener('click', () => {
    closeModal('tv-settings-modal');
  });
  document.getElementById('btn-tv-settings-save').addEventListener('click', saveTVSettings);
  document.getElementById('btn-settings-refresh').addEventListener('click', (event) => {
    loadManagementSettings(event.currentTarget);
  });
  document.getElementById('btn-settings-pair-device').addEventListener('click', (event) => {
    createDevicePairing(event.currentTarget);
  });
  document.getElementById('settings-device-list').addEventListener('click', (event) => {
    const button = event.target.closest('[data-device-id]');
    if (button) revokePairedDevice(button.dataset.deviceId);
  });
  document.getElementById('btn-device-pairing-close').addEventListener('click', () => {
    closeModal('device-pairing-modal');
  });
  document.getElementById('btn-device-pairing-copy').addEventListener('click', async () => {
    const input = document.getElementById('device-pairing-link');
    try {
      await navigator.clipboard.writeText(input.value);
    } catch {
      input.select();
      document.execCommand('copy');
    }
    showToast('Pairing link copied.', 'done');
  });

  // =========================================================================
  // Public domain catalog
  // =========================================================================
  const publicStatus = document.getElementById('public-status');
  const publicStatusText = document.getElementById('public-status-text');

  function showPublicStatus(msg, cls) {
    publicStatus.className = 'status-bar visible' + (cls ? ' ' + cls : '');
    publicStatusText.textContent = msg;
  }

  async function readApiError(resp, fallbackPrefix) {
    const contentType = resp.headers.get('content-type') || '';
    if (contentType.includes('application/json')) {
      const err = await resp.json().catch(() => ({}));
      if (typeof err?.detail === 'string') return err.detail;
      if (err?.detail && typeof err.detail === 'object') return JSON.stringify(err.detail);
      if (typeof err?.error === 'string') return err.error;
    }
    const text = await resp.text().catch(() => '');
    const trimmed = (text || '').trim();
    if (trimmed) return trimmed.slice(0, 300);
    return fallbackPrefix + ' ' + resp.status;
  }

  function renderPublicResults(items) {
    const grid = document.getElementById('public-grid');
    const empty = document.getElementById('public-empty');

    if (!items.length) {
      grid.innerHTML = '';
      empty.textContent = 'No public-domain results found for this query.';
      empty.style.display = 'block';
      return;
    }

    empty.style.display = 'none';
    grid.innerHTML = items.map(item => {
      const title = esc(item.title || (item.source + ' ' + item.artwork_id));
      const artist = esc(item.artist || 'Unknown artist');
      const date = esc(item.date || '');
      const meta = date ? (artist + ' · ' + date) : artist;
      const thumb = esc(item.thumbnail_url || item.image_url || '');
      const link = esc(item.source_url || '#');
      return `
        <div class="gallery-item">
          <img src="${thumb}" alt="${title}" loading="lazy" onerror="this.style.display='none'">
          <div class="info">
            <div class="prompt">${title}</div>
            <div class="meta">${meta}</div>
          </div>
          <div class="actions">
            <button class="btn btn-small btn-pd-display"
                    data-source="${esc(item.source)}"
                    data-id="${esc(item.artwork_id)}">
              Display on TV</button>
            <a class="btn btn-secondary btn-small" href="${link}" target="_blank" rel="noopener noreferrer">
              Source</a>
          </div>
        </div>
      `;
    }).join('');
    animateStaggeredChildren(grid, '.gallery-item');

    grid.querySelectorAll('.btn-pd-display').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        const source = e.currentTarget.dataset.source;
        const artworkId = e.currentTarget.dataset.id;
        await applyPublicArtwork(source, artworkId, e.currentTarget);
      });
    });
  }

  async function searchPublicDomain() {
    const searchBtn = document.getElementById('btn-public-search');
    const source = document.getElementById('public-source').value;
    const q = document.getElementById('public-query').value.trim();
    const grid = document.getElementById('public-grid');
    const empty = document.getElementById('public-empty');

    if (!q) {
      showPublicStatus('Enter search text first.', 'error');
      return;
    }

    showPublicStatus('Searching ' + source.toUpperCase() + '...', '');
    setButtonBusy(searchBtn, 'Searching...');
    grid.innerHTML = '<div class="empty">Searching...</div>';
    empty.style.display = 'none';

    try {
      const resp = await apiFetch(
        '/catalog/search?source=' + encodeURIComponent(source) +
        '&q=' + encodeURIComponent(q) + '&limit=20'
      );
      if (!resp.ok) {
        const detail = await readApiError(resp, 'Server returned');
        throw new Error(detail);
      }
      const items = await resp.json();
      renderPublicResults(items);
      showPublicStatus('Found ' + items.length + ' result(s).', items.length ? 'done' : '');
      if (items.length) showToast('Public results updated (' + items.length + ').', 'done');
    } catch (e) {
      grid.innerHTML = '';
      empty.textContent = 'Search failed.';
      empty.style.display = 'block';
      showPublicStatus('Search failed: ' + e.message, 'error');
      showToast('Public search failed: ' + e.message, 'error');
    } finally {
      clearButtonBusy(searchBtn);
    }
  }

  async function applyPublicArtwork(source, artworkId, btn) {
    const tvIp = document.getElementById('public-tv-select').value;
    const matte = document.getElementById('public-matte-select').value || 'none';
    if (!tvIp) {
      showPublicStatus('Select a TV first.', 'error');
      return;
    }

    setButtonBusy(btn, 'Applying...');
    showPublicStatus('Downloading and uploading artwork...', '');
    try {
      const resp = await apiFetch('/catalog/apply', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          source: source,
          artwork_id: artworkId,
          tv_ip: tvIp,
          matte: matte,
        }),
      });
      if (!resp.ok) {
        const detail = await readApiError(resp, 'Server returned');
        throw new Error(detail);
      }

      const data = await resp.json();
      showPublicStatus('Displayed on TV. Job ' + data.job_id, 'done');
      showToast('Displayed on TV from public catalog.', 'done');
    } catch (e) {
      showPublicStatus('Failed: ' + e.message, 'error');
      showToast('Failed to display public artwork: ' + e.message, 'error');
    } finally {
      clearButtonBusy(btn);
    }
  }

  document.getElementById('btn-public-search').addEventListener('click', searchPublicDomain);
  document.getElementById('public-query').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      searchPublicDomain();
    }
  });
  document.getElementById('public-tv-select').addEventListener('change', (e) => {
    loadMattesForSelect('public-matte-select', e.target.value);
  });
  document.getElementById('own-upload-tv-select').addEventListener('change', (e) => {
    loadMattesForSelect('own-upload-matte-select', e.target.value);
  });
  document.getElementById('edit-upload-tv-select').addEventListener('change', (e) => {
    loadMattesForSelect('edit-upload-matte-select', e.target.value);
  });
  document.getElementById('remix-tv-select').addEventListener('change', (e) => {
    loadMattesForSelect('remix-matte-select', e.target.value);
  });

  async function uploadOwnImage() {
    const fileInput = document.getElementById('own-image-file');
    const tvIp = document.getElementById('own-upload-tv-select').value;
    const matte = document.getElementById('own-upload-matte-select').value || 'none';
    const statusEl = document.getElementById('own-upload-status');
    const statusTextEl = document.getElementById('own-upload-status-text');
    const btn = document.getElementById('btn-own-upload');

    const file = fileInput.files && fileInput.files[0] ? fileInput.files[0] : null;
    if (!file) {
      statusEl.className = 'status-bar visible error';
      statusTextEl.textContent = 'Please choose a JPG or PNG image.';
      return;
    }
    if (!tvIp) {
      statusEl.className = 'status-bar visible error';
      statusTextEl.textContent = 'Please select a TV.';
      return;
    }

    const formData = new FormData();
    formData.append('image', file);
    formData.append('tv_ip', tvIp);
    formData.append('matte', matte);

    setButtonBusy(btn, 'Processing...');
    statusEl.className = 'status-bar visible';
    statusTextEl.textContent = 'Processing image, cropping/upscaling, and uploading...';

    try {
      const resp = await apiFetch('/upload-and-apply', {
        method: 'POST',
        body: formData,
      });
      if (!resp.ok) {
        const detail = await readApiError(resp, 'Server returned');
        throw new Error(detail);
      }
      const data = await resp.json();
      statusEl.className = 'status-bar visible done';
      statusTextEl.textContent = 'Displayed on TV. Job ' + data.job_id;
      showToast('Uploaded and displayed your image on TV.', 'done');
      fileInput.value = '';
      loadGallery();
    } catch (e) {
      statusEl.className = 'status-bar visible error';
      statusTextEl.textContent = 'Failed: ' + e.message;
      showToast('Image upload failed: ' + e.message, 'error');
    } finally {
      clearButtonBusy(btn);
    }
  }

  document.getElementById('btn-own-upload').addEventListener('click', uploadOwnImage);

  async function uploadEditImage() {
    const fileInput = document.getElementById('edit-image-file');
    const promptInput = document.getElementById('edit-prompt');
    const providerInput = document.getElementById('edit-provider');
    const modelInput = document.getElementById('edit-model');
    const tvIp = document.getElementById('edit-upload-tv-select').value;
    const matte = document.getElementById('edit-upload-matte-select').value || 'none';
    const statusEl = document.getElementById('edit-upload-status');
    const statusTextEl = document.getElementById('edit-upload-status-text');
    const btn = document.getElementById('btn-edit-upload');

    const file = fileInput.files && fileInput.files[0] ? fileInput.files[0] : null;
    const editPrompt = promptInput.value.trim();
    if (!file) {
      statusEl.className = 'status-bar visible error';
      statusTextEl.textContent = 'Please choose a JPG or PNG image.';
      return;
    }
    if (!editPrompt) {
      statusEl.className = 'status-bar visible error';
      statusTextEl.textContent = 'Please provide an edit prompt.';
      return;
    }
    const noUpload = !tvIp;

    const formData = new FormData();
    formData.append('image', file);
    formData.append('prompt', editPrompt);
    if (tvIp) formData.append('tv_ip', tvIp);
    formData.append('matte', matte);
    if (noUpload) formData.append('no_upload', 'true');
    if (providerInput.value) formData.append('provider', providerInput.value);
    if (modelInput.value) formData.append('model', modelInput.value);

    setButtonBusy(btn, 'Editing...');
    statusEl.className = 'status-bar visible';
    statusTextEl.textContent = noUpload
      ? 'Editing image and preparing gallery output...'
      : 'Editing image and preparing TV output...';

    try {
      const resp = await apiFetch('/edit-and-apply', {
        method: 'POST',
        body: formData,
      });
      if (!resp.ok) {
        const detail = await readApiError(resp, 'Server returned');
        throw new Error(detail);
      }
      const data = await resp.json();
      statusEl.className = 'status-bar visible done';
      statusTextEl.textContent = noUpload
        ? ('Edited image saved. Job ' + data.job_id)
        : ('Edited image displayed on TV. Job ' + data.job_id);
      showToast(
        noUpload ? 'Edited image saved to Gallery.' : 'Edited image displayed on TV.',
        'done'
      );
      fileInput.value = '';
      promptInput.value = '';
      loadGallery();
    } catch (e) {
      statusEl.className = 'status-bar visible error';
      statusTextEl.textContent = 'Failed: ' + e.message;
      showToast('Image edit failed: ' + e.message, 'error');
    } finally {
      clearButtonBusy(btn);
    }
  }

  document.getElementById('btn-edit-upload').addEventListener('click', uploadEditImage);

  // =========================================================================
  // Remix modal (edit / generate from existing library or TV artwork)
  // =========================================================================
  let remixSource = null;

  window.openRemixFromJob = function(jobId, label) {
    remixSource = { kind: 'job', jobId: jobId, label: label || jobId };
    document.getElementById('remix-source-label').textContent = 'Library · ' + (label || jobId);
    document.getElementById('remix-status').classList.remove('visible');
    refreshTVSelects();

    const remixTvSel = document.getElementById('remix-tv-select');
    const rememberedTv = localStorage.getItem(storageKeys.tvRemix);
    if (rememberedTv && [...remixTvSel.options].some((o) => o.value === rememberedTv)) {
      remixTvSel.value = rememberedTv;
    }
    loadMattesForSelect('remix-matte-select', remixTvSel.value || knownTVs[0]?.ip);
    openModal('remix-modal', '#remix-prompt');
  };

  window.openRemixFromTVArt = function(contentId, tvIp) {
    remixSource = { kind: 'tv_art', contentId: contentId, tvIp: tvIp };
    document.getElementById('remix-source-label').textContent = 'TV · ' + contentId;
    document.getElementById('remix-status').classList.remove('visible');
    refreshTVSelects();

    const remixTvSel = document.getElementById('remix-tv-select');
    if (tvIp && [...remixTvSel.options].some((o) => o.value === tvIp)) {
      remixTvSel.value = tvIp;
    } else {
      const rememberedTv = localStorage.getItem(storageKeys.tvRemix);
      if (rememberedTv && [...remixTvSel.options].some((o) => o.value === rememberedTv)) {
        remixTvSel.value = rememberedTv;
      }
    }
    loadMattesForSelect('remix-matte-select', remixTvSel.value || tvIp || knownTVs[0]?.ip);
    openModal('remix-modal', '#remix-prompt');
  };

  async function applyRemixFromExisting() {
    if (!remixSource) return;

    const promptInput = document.getElementById('remix-prompt');
    const providerInput = document.getElementById('remix-provider');
    const modelInput = document.getElementById('remix-model');
    const tvInput = document.getElementById('remix-tv-select');
    const matteInput = document.getElementById('remix-matte-select');
    const statusEl = document.getElementById('remix-status');
    const statusTextEl = document.getElementById('remix-status-text');
    const btn = document.getElementById('btn-remix-apply');

    const prompt = promptInput.value.trim();
    if (!prompt) {
      statusEl.className = 'status-bar visible error';
      statusTextEl.textContent = 'Please provide an edit prompt.';
      return;
    }

    const tvIp = tvInput.value || '';
    const noUpload = !tvIp;
    const body = {
      prompt: prompt,
      matte: matteInput.value || 'none',
      no_upload: noUpload,
    };
    if (providerInput.value) body.provider = providerInput.value;
    if (modelInput.value) body.model = modelInput.value;
    if (tvIp) body.tv_ip = tvIp;

    let endpoint = '';
    if (remixSource.kind === 'job') {
      endpoint = '/jobs/' + encodeURIComponent(remixSource.jobId) + '/edit-and-apply';
    } else if (remixSource.kind === 'tv_art') {
      endpoint = '/tv/art/edit-and-apply';
      body.content_id = remixSource.contentId;
      if (remixSource.tvIp) body.source_tv_ip = remixSource.tvIp;
    } else {
      statusEl.className = 'status-bar visible error';
      statusTextEl.textContent = 'Invalid remix source.';
      return;
    }

    setButtonBusy(btn, 'Creating...');
    statusEl.className = 'status-bar visible';
    statusTextEl.textContent = noUpload
      ? 'Creating new image and saving to Gallery...'
      : 'Creating new image and displaying on TV...';

    try {
      const resp = await apiFetch(endpoint, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const detail = await readApiError(resp, 'Server returned');
        throw new Error(detail);
      }
      const data = await resp.json();
      statusEl.className = 'status-bar visible done';
      statusTextEl.textContent = noUpload
        ? ('Created new image and saved to Gallery. Job ' + data.job_id)
        : ('Created new image and displayed on TV. Job ' + data.job_id);
      showToast(
        noUpload ? 'New image created from existing art.' : 'New image created and displayed on TV.',
        'done'
      );
      loadGallery();
      if (getActivePageName() === 'tvs') loadTVArt();
    } catch (e) {
      statusEl.className = 'status-bar visible error';
      statusTextEl.textContent = 'Failed: ' + e.message;
      showToast('Failed to create image from existing art: ' + e.message, 'error');
    } finally {
      clearButtonBusy(btn);
    }
  }

  document.getElementById('btn-remix-cancel').addEventListener('click', () => {
    closeModal('remix-modal');
  });
  document.getElementById('btn-remix-apply').addEventListener('click', applyRemixFromExisting);

  // =========================================================================
  // Upload-to-TV modal (from gallery)
  // =========================================================================
  let uploadJobId = null;

  window.openUploadModal = function(jobId, promptLabel) {
    uploadJobId = jobId;
    document.getElementById('upload-job-id').textContent = promptLabel;
    document.getElementById('upload-status').classList.remove('visible');

    // Populate TV select with known TVs
    refreshTVSelects();
    const uploadTvSel = document.getElementById('upload-tv-select');
    const rememberedTv = localStorage.getItem(storageKeys.tvUpload);
    if (rememberedTv && [...uploadTvSel.options].some(o => o.value === rememberedTv)) {
      uploadTvSel.value = rememberedTv;
    }

    if (!knownTVs.length) {
      document.getElementById('upload-status').className = 'status-bar visible error';
      document.getElementById('upload-status-text').textContent =
        'No TVs available. Configure one or use Scan Network first.';
    }

    // Try to load mattes for selected TV, fallback to first known TV.
    loadMattesForSelect('upload-matte-select', uploadTvSel.value || knownTVs[0]?.ip);

    openModal('upload-modal', '#upload-tv-select');
  };

  document.getElementById('btn-upload-cancel').addEventListener('click', () => {
    closeModal('upload-modal');
  });

  document.getElementById('upload-tv-select').addEventListener('change', (e) => {
    loadMattesForSelect('upload-matte-select', e.target.value);
  });

  document.getElementById('btn-upload-apply').addEventListener('click', async () => {
    const tvIp = document.getElementById('upload-tv-select').value;
    const matte = document.getElementById('upload-matte-select').value || 'none';
    const statusEl = document.getElementById('upload-status');
    const statusTextEl = document.getElementById('upload-status-text');

    if (!tvIp) {
      statusEl.className = 'status-bar visible error';
      statusTextEl.textContent = 'Please select a TV.';
      return;
    }

    const btn = document.getElementById('btn-upload-apply');
    setButtonBusy(btn, 'Uploading...');
    statusEl.className = 'status-bar visible';
    statusTextEl.textContent = 'Uploading...';

    try {
      const resp = await apiFetch('/jobs/' + uploadJobId + '/apply', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ tv_ip: tvIp, matte: matte }),
      });

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || 'Server returned ' + resp.status);
      }

      statusEl.className = 'status-bar visible done';
      statusTextEl.textContent = 'Uploaded successfully!';
      showToast('Artwork uploaded to TV.', 'done');
    } catch (e) {
      statusEl.className = 'status-bar visible error';
      statusTextEl.textContent = 'Failed: ' + e.message;
      showToast('Upload to TV failed: ' + e.message, 'error');
    } finally {
      clearButtonBusy(btn);
    }
  });

  // =========================================================================
  // TV Discovery
  // =========================================================================
  const tvDiscoveryStatus = document.getElementById('tv-discovery-status');
  const tvDiscoveryStatusText = document.getElementById('tv-discovery-status-text');

  function showTVDiscoveryStatus(message, kind) {
    tvDiscoveryStatus.className = 'status-bar visible' + (kind ? ' ' + kind : '');
    tvDiscoveryStatusText.textContent = message;
  }

  function normalizePrivateIPv4(value) {
    const parts = value.trim().split('.');
    if (parts.length !== 4 || parts.some((part) => !/^\d{1,3}$/.test(part))) return null;
    const octets = parts.map(Number);
    if (octets.some((part) => part < 0 || part > 255)) return null;
    const [first, second] = octets;
    const isPrivate = first === 10 ||
      (first === 172 && second >= 16 && second <= 31) ||
      (first === 192 && second === 168);
    return isPrivate ? octets.join('.') : null;
  }

  function showManualTVError(message) {
    const status = document.getElementById('manual-tv-error');
    document.getElementById('manual-tv-error-text').textContent = message || '';
    status.classList.toggle('visible', Boolean(message));
  }

  document.getElementById('btn-add-tv').addEventListener('click', () => {
    showManualTVError('');
    document.getElementById('manual-tv-name').value = '';
    document.getElementById('manual-tv-ip').value = '';
    openModal('add-tv-modal', '#manual-tv-name');
  });

  document.getElementById('btn-add-tv-cancel').addEventListener('click', () => {
    closeModal('add-tv-modal');
  });

  function addManualTV() {
    const nameInput = document.getElementById('manual-tv-name');
    const ipInput = document.getElementById('manual-tv-ip');
    const ip = normalizePrivateIPv4(ipInput.value);
    if (!ip) {
      showManualTVError('Enter an RFC1918 private IPv4 address, such as 192.168.1.100.');
      ipInput.focus();
      return;
    }
    if (knownTVs.some((tv) => tv.ip === ip)) {
      showManualTVError('That TV is already in the list.');
      ipInput.focus();
      return;
    }

    const name = nameInput.value.trim() || ('Frame TV ' + ip);
    addTVs([{ ip, name, model: null, frame_tv: true }], 'manual');
    closeModal('add-tv-modal');
    showToast('Added ' + name + ' for this session.', 'done');
  }

  document.getElementById('btn-add-tv-apply').addEventListener('click', addManualTV);
  document.getElementById('manual-tv-ip').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      addManualTV();
    }
  });

  const btnDiscover = document.getElementById('btn-discover');
  btnDiscover.addEventListener('click', async () => {
    const empty = document.getElementById('tv-empty');
    setButtonBusy(btnDiscover, 'Scanning...');
    showTVDiscoveryStatus('Scanning the local network for Samsung TVs...', '');
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 45000);

    try {
      const resp = await apiFetch('/tv/discover?timeout=4', { signal: controller.signal });
      const tvs = await parseJSONResponse(resp, 'TV discovery request failed.');
      if (!Array.isArray(tvs)) throw new Error('The server returned an invalid discovery response.');
      if (!tvs.length && !knownTVs.length) {
        empty.textContent = 'No Samsung TVs found on network.';
        empty.style.display = 'block';
        showTVDiscoveryStatus(
          'No TVs responded. If FrameArt runs in Docker, use frameart-api-lan, or add the TV by IP.',
          '',
        );
        return;
      }
      addTVs(tvs, 'discovered');
      showTVDiscoveryStatus(
        tvs.length ? ('Found ' + tvs.length + ' TV(s).') : 'Scan complete; no new TVs found.',
        'done',
      );
      showToast(tvs.length ? ('Found ' + tvs.length + ' TV(s).') : 'Scan complete.', 'done');
    } catch (e) {
      let message = e && e.message ? e.message : 'Unknown discovery error.';
      if (e && e.name === 'AbortError') {
        message = 'Discovery timed out after 45 seconds. Add the TV by IP or check LAN access.';
      } else if (/failed to fetch|networkerror/i.test(message)) {
        message = 'Could not reach the FrameArt API. Confirm the server is running and reload this page.';
      }
      showTVDiscoveryStatus('Discovery failed: ' + message, 'error');
      showToast('TV discovery failed: ' + message, 'error');
    } finally {
      window.clearTimeout(timer);
      clearButtonBusy(btnDiscover);
    }
  });

  // =========================================================================
  // TV Art — list art on TV
  // =========================================================================
  document.getElementById('tv-art-select').addEventListener('change', () => {
    selectedTVArtIds = new Set();
    loadedTVArtById = {};
    updateTVSelectionUI();
    document.getElementById('tv-art-grid').innerHTML = '';
    const empty = document.getElementById('tv-art-empty');
    empty.textContent = 'Select a TV and click "Load Art".';
    empty.style.display = 'block';
  });
  document.getElementById('btn-load-art').addEventListener('click', loadTVArt);
  document.getElementById('btn-delete-selected').addEventListener('click', deleteSelectedTVArt);
  document.getElementById('btn-tv-art-select-all').addEventListener('click', () => setAllTVArtSelections(true));
  document.getElementById('btn-tv-art-clear').addEventListener('click', () => setAllTVArtSelections(false));
  document.getElementById('btn-tv-delete-selected-inline').addEventListener('click', deleteSelectedTVArt);
  document.getElementById('btn-tv-clear-selection-inline').addEventListener('click', () => setAllTVArtSelections(false));
  document.getElementById('btn-tv-display-selected').addEventListener('click', (e) => displaySelectedTVArt(e.currentTarget));
  document.getElementById('btn-tv-matte-selected').addEventListener('click', openMatteForSelectedTVArt);
  document.getElementById('btn-delete-all-art').addEventListener('click', deleteAllTVArt);
  document.getElementById('btn-delete-all-except-fav').addEventListener('click', deleteAllExceptFavoritesTVArt);

  function updateTVSelectionUI() {
    const btn = document.getElementById('btn-delete-selected');
    const inlineDeleteBtn = document.getElementById('btn-tv-delete-selected-inline');
    const inlineDisplayBtn = document.getElementById('btn-tv-display-selected');
    const inlineMatteBtn = document.getElementById('btn-tv-matte-selected');
    const inlineClearBtn = document.getElementById('btn-tv-clear-selection-inline');
    const bar = document.getElementById('tv-selection-bar');
    const text = document.getElementById('tv-selection-text');
    const n = selectedTVArtIds.size;
    const hasSelection = n > 0;
    const isSingle = n === 1;
    const label = n === 1 ? '1 artwork selected on TV' : (n + ' artwork selected on TV');

    btn.disabled = n === 0;
    btn.textContent = n > 0 ? ('Delete Selected (' + n + ')') : 'Delete Selected';
    inlineDeleteBtn.disabled = !hasSelection;
    inlineDisplayBtn.disabled = !isSingle;
    inlineMatteBtn.disabled = !isSingle;
    inlineClearBtn.disabled = !hasSelection;
    text.textContent = label;
    bar.classList.toggle('visible', hasSelection);
  }

  function setAllTVArtSelections(checked) {
    document.querySelectorAll('.tv-art-select-item').forEach((el) => {
      const cid = el.dataset.contentId;
      if (!cid) return;
      el.checked = checked;
      const card = el.closest('.tv-art-item');
      if (card) card.classList.toggle('selected', checked);
      if (checked) selectedTVArtIds.add(cid);
      else selectedTVArtIds.delete(cid);
    });
    updateTVSelectionUI();
  }

  function setTVThumbnailState(wrapper, state, message) {
    const image = wrapper.querySelector('.art-thumb');
    const fallback = wrapper.querySelector('.art-thumb-fallback');
    wrapper.dataset.thumbnailState = state;
    fallback.textContent = message;
    fallback.disabled = state !== 'error';
    fallback.style.display = state === 'loaded' ? 'none' : 'flex';
    image.style.display = state === 'loaded' ? 'block' : 'none';
  }

  async function loadTVThumbnail(wrapper, tvIp, contentId, refresh = false) {
    const image = wrapper.querySelector('.art-thumb');
    setTVThumbnailState(wrapper, 'loading', 'Loading thumbnail…');
    const params = new URLSearchParams({ tv_ip: tvIp, content_id: contentId });
    if (refresh) params.set('refresh', 'true');
    try {
      const response = await apiFetch('/tv/art/thumbnail?' + params.toString());
      if (response.status === 404) {
        setTVThumbnailState(wrapper, 'missing', 'No thumbnail available');
        return;
      }
      if (!response.ok) {
        setTVThumbnailState(wrapper, 'error', 'TV unavailable · Retry');
        return;
      }
      const blob = await response.blob();
      const previousUrl = image.dataset.objectUrl;
      if (previousUrl) URL.revokeObjectURL(previousUrl);
      const objectUrl = URL.createObjectURL(blob);
      image.dataset.objectUrl = objectUrl;
      image.loading = 'eager';
      image.onload = () => setTVThumbnailState(wrapper, 'loaded', '');
      image.onerror = () => setTVThumbnailState(wrapper, 'error', 'Preview unavailable · Retry');
      image.src = objectUrl;
    } catch {
      setTVThumbnailState(wrapper, 'error', 'TV unavailable · Retry');
    }
  }

  function loadTVThumbnailQueue(wrappers, tvIp) {
    const queue = [...wrappers];
    const worker = async () => {
      while (queue.length) {
        const wrapper = queue.shift();
        await loadTVThumbnail(wrapper, tvIp, wrapper.dataset.contentId);
      }
    };
    Promise.all([worker(), worker()]).catch(() => {});
  }

  async function warmTVThumbnailQueue(wrappers, tvIp) {
    const pending = [...wrappers];
    const available = [];

    try {
      for (let offset = 0; offset < pending.length; offset += 100) {
        const chunk = pending.slice(offset, offset + 100);
        const response = await apiFetch('/tv/art/thumbnails/warm', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            tv_ip: tvIp,
            content_ids: chunk.map(wrapper => wrapper.dataset.contentId),
          }),
        });

        // Some newer TVs do not implement the batch request. Preserve the
        // individual path as a compatibility fallback when it fails cleanly.
        if (!response.ok) {
          loadTVThumbnailQueue(pending, tvIp);
          return;
        }

        const result = await response.json();
        const ready = new Set([...(result.cached || []), ...(result.warmed || [])]);
        const missing = new Set(result.missing || []);
        chunk.forEach((wrapper) => {
          const contentId = wrapper.dataset.contentId;
          if (ready.has(contentId)) available.push(wrapper);
          else if (missing.has(contentId)) {
            setTVThumbnailState(wrapper, 'missing', 'No thumbnail available');
          } else {
            setTVThumbnailState(wrapper, 'error', 'TV unavailable · Retry');
          }
        });
      }
      loadTVThumbnailQueue(available, tvIp);
    } catch {
      loadTVThumbnailQueue(pending, tvIp);
    }
  }

  async function loadTVArt() {
    const tvIp = document.getElementById('tv-art-select').value;
    const grid = document.getElementById('tv-art-grid');
    const empty = document.getElementById('tv-art-empty');

    selectedTVArtIds = new Set();
    loadedTVArtById = {};
    updateTVSelectionUI();

    if (!tvIp) {
      empty.textContent = 'Please select a TV.';
      empty.style.display = 'block';
      grid.innerHTML = '';
      return;
    }

    grid.innerHTML = gallerySkeleton(8);
    empty.style.display = 'none';

    try {
      const resp = await apiFetch('/tv/art?tv_ip=' + encodeURIComponent(tvIp));
      if (!resp.ok) throw new Error('Server returned ' + resp.status);
      const artworks = await resp.json();

      if (!artworks.length) {
        grid.innerHTML = '';
        empty.textContent = 'No artwork on this TV.';
        empty.style.display = 'block';
        return;
      }

      loadedTVArtById = Object.fromEntries(artworks.map(a => [a.content_id, a]));

      grid.innerHTML = artworks.map(a => {
        const localPreview = a.local_job_id
          ? '/jobs/' + encodeURIComponent(a.local_job_id) + '/image'
          : '';
        return `
        <div class="tv-art-item">
          <label class="art-select-row">
            <input type="checkbox" class="tv-art-select-item" data-content-id="${esc(a.content_id)}">
            <span class="art-id">${esc(a.content_id)}</span>
            ${a.is_favourite ? '<span class="art-fav">\u2665 favourite</span>' : ''}
          </label>
          <div class="art-thumb-wrap" data-content-id="${esc(a.content_id)}"
               data-thumbnail-state="${localPreview ? 'local' : 'loading'}">
            <img class="art-thumb"
                 ${localPreview ? `src="${esc(localPreview)}"` : ''}
                 alt="Thumbnail ${esc(a.content_id)}"
                 loading="lazy"${localPreview ? '' : ' style="display:none"'}>
            <button type="button" class="art-thumb-fallback"
                    ${localPreview ? 'style="display:none" disabled' : 'disabled'}>
              ${localPreview ? '' : 'Loading thumbnail…'}
            </button>
          </div>
          <div class="art-actions">
            <button class="btn btn-small"
                    onclick="displayTVArt('${esc(a.content_id)}', '${esc(tvIp)}', this)">
              Display on TV</button>
            <button class="btn btn-secondary btn-small"
                    onclick="openMatteModal('${esc(a.content_id)}', '${esc(tvIp)}')">
              Change Matte</button>
            <button class="btn btn-secondary btn-small"
                    onclick="openRemixFromTVArt('${esc(a.content_id)}', '${esc(tvIp)}')">
              Edit / Generate New</button>
            <button class="btn btn-danger btn-small"
                    onclick="deleteTVArt('${esc(a.content_id)}', '${esc(tvIp)}', ${a.is_favourite})">
              Delete</button>
          </div>
        </div>
      `;
      }).join('');
      animateStaggeredChildren(grid, '.tv-art-item');

      const coldThumbnails = [];
      grid.querySelectorAll('.art-thumb-wrap').forEach((wrapper) => {
        const contentId = wrapper.dataset.contentId;
        const image = wrapper.querySelector('.art-thumb');
        const fallback = wrapper.querySelector('.art-thumb-fallback');
        fallback.addEventListener('click', () => {
          loadTVThumbnail(wrapper, tvIp, contentId, true);
        });
        if (wrapper.dataset.thumbnailState === 'local') {
          image.addEventListener('load', () => setTVThumbnailState(wrapper, 'loaded', ''));
          image.addEventListener('error', () => loadTVThumbnail(wrapper, tvIp, contentId));
          if (image.complete && image.naturalWidth > 0) {
            setTVThumbnailState(wrapper, 'loaded', '');
          }
        } else {
          coldThumbnails.push(wrapper);
        }
      });
      warmTVThumbnailQueue(coldThumbnails, tvIp);

      grid.querySelectorAll('.tv-art-select-item').forEach(el => {
        el.addEventListener('change', (e) => {
          const checkbox = e.target;
          const card = checkbox.closest('.tv-art-item');
          const cid = checkbox.dataset.contentId;
          if (!cid) return;
          if (checkbox.checked) {
            selectedTVArtIds.add(cid);
          } else {
            selectedTVArtIds.delete(cid);
          }
          if (card) card.classList.toggle('selected', checkbox.checked);
          updateTVSelectionUI();
        });
      });
    } catch (e) {
      grid.innerHTML = '<div class="empty">Failed to load art: ' + esc(e.message) + '</div>';
      showToast('Failed to load TV art: ' + e.message, 'error');
    } finally {
      updateTVSelectionUI();
    }
  }

  // =========================================================================
  // Display Existing TV Art
  // =========================================================================
  window.displayTVArt = async function(contentId, tvIp, btn) {
    setButtonBusy(btn, 'Displaying...');
    try {
      const resp = await apiFetch('/tv/art/display', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          content_id: contentId,
          tv_ip: tvIp,
        }),
      });
      if (!resp.ok) throw new Error('Server returned ' + resp.status);
      showToast('TV switched to ' + contentId + '.', 'done');
    } catch (e) {
      showToast('Failed to display artwork: ' + e.message, 'error');
    } finally {
      clearButtonBusy(btn);
    }
  };

  function getSingleSelectedTVArtId() {
    const selected = Array.from(selectedTVArtIds);
    return selected.length === 1 ? selected[0] : null;
  }

  async function displaySelectedTVArt(btn) {
    const tvIp = document.getElementById('tv-art-select').value;
    const contentId = getSingleSelectedTVArtId();
    if (!tvIp || !contentId) return;
    await window.displayTVArt(contentId, tvIp, btn);
  }

  function openMatteForSelectedTVArt() {
    const tvIp = document.getElementById('tv-art-select').value;
    const contentId = getSingleSelectedTVArtId();
    if (!tvIp || !contentId) return;
    window.openMatteModal(contentId, tvIp);
  }

  // =========================================================================
  // Delete TV Art
  // =========================================================================
  window.deleteTVArt = async function(contentId, tvIp, isFav) {
    const msg = isFav
      ? 'This artwork is favourited. Delete anyway? (content: ' + contentId + ')'
      : 'Delete artwork ' + contentId + ' from TV?';
    if (!confirm(msg)) return;

    try {
      const resp = await apiFetch('/tv/art/delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          content_ids: [contentId],
          tv_ip: tvIp,
          include_favorites: isFav,
        }),
      });
      if (!resp.ok) throw new Error('Server returned ' + resp.status);
      const data = await resp.json();
      if (data.deleted.length) {
        loadTVArt(); // Refresh the list
        showToast('Artwork deleted from TV.', 'done');
      } else if (data.skipped_favorites.length) {
        showToast('Artwork was skipped because it is a favourite.', 'warn');
      }
    } catch (e) {
      showToast('Failed to delete: ' + e.message, 'error');
    }
  };

  async function deleteSelectedTVArt() {
    const tvIp = document.getElementById('tv-art-select').value;
    const selectedIds = Array.from(selectedTVArtIds);
    if (!tvIp || !selectedIds.length) return;

    const favCount = selectedIds.filter(cid => loadedTVArtById[cid]?.is_favourite).length;
    const msg = favCount
      ? ('Delete ' + selectedIds.length + ' selected artworks? This includes ' + favCount + ' favourite(s).')
      : ('Delete ' + selectedIds.length + ' selected artworks from TV?');
    if (!confirm(msg)) return;

    const btn = document.getElementById('btn-delete-selected');
    setButtonBusy(btn, 'Deleting...');

    try {
      const resp = await apiFetch('/tv/art/delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          content_ids: selectedIds,
          tv_ip: tvIp,
          include_favorites: favCount > 0,
        }),
      });
      if (!resp.ok) throw new Error('Server returned ' + resp.status);
      const data = await resp.json();
      if (data.deleted.length) {
        await loadTVArt();
        showToast('Deleted selected artwork from TV.', 'done');
      }
      if (data.skipped_favorites.length) {
        showToast('Some favourites were skipped: ' + data.skipped_favorites.join(', '), 'warn');
      }
    } catch (e) {
      showToast('Failed to delete selected artwork: ' + e.message, 'error');
    } finally {
      clearButtonBusy(btn);
      updateTVSelectionUI();
    }
  }

  async function deleteAllTVArt() {
    const tvIp = document.getElementById('tv-art-select').value;
    if (!tvIp) return;
    const allIds = Object.keys(loadedTVArtById);
    if (!allIds.length) { showToast('No artwork loaded. Click "Load Art" first.', 'warn'); return; }
    if (!confirm('Delete ALL ' + allIds.length + ' artworks from TV? This includes favourites and cannot be undone.')) return;

    const btn = document.getElementById('btn-delete-all-art');
    setButtonBusy(btn, 'Deleting all...');
    try {
      const resp = await apiFetch('/tv/art/delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ content_ids: allIds, tv_ip: tvIp, include_favorites: true }),
      });
      if (!resp.ok) throw new Error('Server returned ' + resp.status);
      const data = await resp.json();
      await loadTVArt();
      showToast('Deleted ' + data.deleted.length + ' artwork(s) from TV.', 'done');
    } catch (e) {
      showToast('Failed to delete all: ' + e.message, 'error');
    } finally {
      clearButtonBusy(btn);
    }
  }

  async function deleteAllExceptFavoritesTVArt() {
    const tvIp = document.getElementById('tv-art-select').value;
    if (!tvIp) return;
    const nonFavIds = Object.entries(loadedTVArtById)
      .filter(([, a]) => !a.is_favourite)
      .map(([cid]) => cid);
    if (!nonFavIds.length) { showToast('All artwork on this TV is favourited.', 'warn'); return; }
    const favCount = Object.keys(loadedTVArtById).length - nonFavIds.length;
    const msg = 'Delete ' + nonFavIds.length + ' non-favourite artwork(s) from TV?'
      + (favCount > 0 ? ' (' + favCount + ' favourite(s) will be kept.)' : '');
    if (!confirm(msg)) return;

    const btn = document.getElementById('btn-delete-all-except-fav');
    setButtonBusy(btn, 'Deleting...');
    try {
      const resp = await apiFetch('/tv/art/delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ content_ids: nonFavIds, tv_ip: tvIp, include_favorites: false }),
      });
      if (!resp.ok) throw new Error('Server returned ' + resp.status);
      const data = await resp.json();
      await loadTVArt();
      showToast('Deleted ' + data.deleted.length + ' artwork(s), kept ' + favCount + ' favourite(s).', 'done');
    } catch (e) {
      showToast('Failed to delete: ' + e.message, 'error');
    } finally {
      clearButtonBusy(btn);
    }
  }

  // =========================================================================
  // Change Matte modal
  // =========================================================================
  let matteContentId = null;
  let matteTvIp = null;

  window.openMatteModal = async function(contentId, tvIp) {
    matteContentId = contentId;
    matteTvIp = tvIp;
    document.getElementById('matte-content-id').textContent = contentId;
    openModal('matte-modal', '#matte-select');

    loadMattesForSelect('matte-select', tvIp);
  };

  function matteEntryId(entry) {
    if (typeof entry === 'string') return entry.trim();
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) return '';
    for (const key of ['matte_id', 'matte_type', 'matteId', 'matteType']) {
      if (typeof entry[key] === 'string' && entry[key].trim()) return entry[key].trim();
    }
    return '';
  }

  function setMatteFallbackOptions(select) {
    select.innerHTML = '';
    const option = document.createElement('option');
    option.value = 'none';
    option.textContent = 'none';
    select.appendChild(option);
  }

  async function loadMattesForSelect(selectId, tvIp) {
    const sel = document.getElementById(selectId);
    if (!tvIp) return;

    sel.innerHTML = '<option value="">Loading...</option>';
    try {
      const resp = await apiFetch('/tv/mattes?tv_ip=' + encodeURIComponent(tvIp));
      if (!resp.ok) throw new Error('Failed to load');
      const mattes = await resp.json();
      sel.innerHTML = '';
      const matteIds = Array.isArray(mattes)
        ? [...new Set(mattes.map(matteEntryId).filter(Boolean))]
        : [];
      if (!matteIds.length) {
        setMatteFallbackOptions(sel);
        return;
      }
      for (const id of matteIds) {
        const opt = document.createElement('option');
        opt.value = id;
        opt.textContent = id;
        sel.appendChild(opt);
      }
      const remembered = localStorage.getItem(
        selectId === 'public-matte-select' ? storageKeys.mattePublic :
        selectId === 'own-upload-matte-select' ? storageKeys.matteOwnUpload :
        selectId === 'edit-upload-matte-select' ? storageKeys.matteEditUpload :
        selectId === 'remix-matte-select' ? storageKeys.matteRemix :
        storageKeys.matteUpload
      );
      if (remembered && [...sel.options].some(o => o.value === remembered)) sel.value = remembered;
    } catch {
      setMatteFallbackOptions(sel);
      const remembered = localStorage.getItem(
        selectId === 'public-matte-select' ? storageKeys.mattePublic :
        selectId === 'own-upload-matte-select' ? storageKeys.matteOwnUpload :
        selectId === 'edit-upload-matte-select' ? storageKeys.matteEditUpload :
        selectId === 'remix-matte-select' ? storageKeys.matteRemix :
        storageKeys.matteUpload
      );
      if (remembered && [...sel.options].some(o => o.value === remembered)) sel.value = remembered;
    }
  }

  document.getElementById('btn-matte-cancel').addEventListener('click', () => {
    closeModal('matte-modal');
  });

  document.getElementById('btn-matte-apply').addEventListener('click', async () => {
    const matteId = document.getElementById('matte-select').value;
    if (!matteId) return;

    const btn = document.getElementById('btn-matte-apply');
    btn.disabled = true;

    try {
      const resp = await apiFetch('/tv/art/matte', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          content_id: matteContentId,
          matte_id: matteId,
          tv_ip: matteTvIp,
        }),
      });
      if (!resp.ok) throw new Error('Server returned ' + resp.status);
      closeModal('matte-modal');
    } catch (e) {
      alert('Failed to change matte: ' + e.message);
    } finally {
      btn.disabled = false;
    }
  });

  // Close modals on overlay click (but not dialog click)
  modalIds.forEach((modalId) => {
    const modal = document.getElementById(modalId);
    if (!modal) return;
    modal.addEventListener('click', function(e) {
      if (e.target === this) closeModal(modalId);
    });
  });

  function isTypingTarget(el) {
    if (!el) return false;
    const tag = (el.tagName || '').toLowerCase();
    return tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable;
  }

  // =========================================================================
  // Keyboard shortcuts (Phase 2B)
  // =========================================================================
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeAllModals();
      return;
    }

    // Cmd/Ctrl+Enter: generate from prompt
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      const activePage = getActivePageName();
      const createMode = getActiveCreateModeName();
      if (activePage === 'create' && createMode === 'ai') {
        e.preventDefault();
        document.getElementById('btn-generate').click();
      }
      return;
    }

    const typing = isTypingTarget(e.target);

    // Alt+Shift shortcuts for page + create mode navigation.
    if (e.altKey && e.shiftKey) {
      const key = e.key.toLowerCase();
      if (key === 'c') setActivePage('create');
      else if (key === 'l') setActivePage('library');
      else if (key === 't') setActivePage('tvs');
      else if (key === 's') setActivePage('settings');
      else if (key === 'a') { setActivePage('create'); setActiveCreateMode('ai'); }
      else if (key === 'p') { setActivePage('create'); setActiveCreateMode('public'); }
      else if (key === 'u') { setActivePage('create'); setActiveCreateMode('upload'); }
      else if (key === 'e') { setActivePage('create'); setActiveCreateMode('edit'); }
      else return;
      e.preventDefault();
      return;
    }

    if (typing || e.metaKey || e.ctrlKey || e.altKey) return;

    // Slash focuses primary input in current context.
    if (e.key === '/') {
      const activePage = getActivePageName();
      if (activePage === 'create') {
        const mode = getActiveCreateModeName();
        e.preventDefault();
        if (mode === 'public') document.getElementById('public-query').focus();
        else if (mode === 'upload') document.getElementById('own-image-file').focus();
        else if (mode === 'edit') document.getElementById('edit-prompt').focus();
        else document.getElementById('prompt').focus();
      } else if (activePage === 'library') {
        e.preventDefault();
        document.getElementById('btn-refresh-gallery').focus();
      } else if (activePage === 'tvs') {
        e.preventDefault();
        document.getElementById('tv-art-select').focus();
      }
    }
  });

  // =========================================================================
  // Helpers
  // =========================================================================
  function esc(str) {
    const el = document.createElement('span');
    el.textContent = str || '';
    return el.innerHTML;
  }
})();
