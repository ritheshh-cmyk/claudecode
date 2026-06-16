const state = {
  config: null,
  fields: new Map(),
  localStatus: new Map(),
  modelOptions: [],
  activeView: "providers",
  theme: "dark",
  profiles: [],
  activeProfile: "default",
  logSource: null,
};

const MASKED_SECRET = "********";
const VIEW_GROUPS = [
  {
    id: "providers",
    label: "Providers",
    title: "Providers",
    sections: ["providers", "runtime"],
    containerId: "providersSections",
  },
  {
    id: "model_config",
    label: "Model Config",
    title: "Model Config",
    sections: ["models", "thinking", "web_tools"],
    containerId: "modelConfigSections",
  },
  {
    id: "messaging",
    label: "Messaging",
    title: "Messaging",
    sections: ["messaging", "voice"],
    containerId: "messagingSections",
  },
  {
    id: "logs",
    label: "Live Logs",
    title: "Server Logs",
    sections: [],
    containerId: "",
  },
  {
    id: "comparison",
    label: "Comparison & Explorer",
    title: "Comparison & Explorer",
    sections: [],
    containerId: "",
  },
];

const byId = (id) => document.getElementById(id);

function sourceLabel(source) {
  const labels = {
    default: "default",
    template: "template",
    repo_env: "repo .env",
    managed_env: "",
    explicit_env_file: "FCC_ENV_FILE",
    process: "process env",
  };
  return Object.prototype.hasOwnProperty.call(labels, source) ? labels[source] : source;
}

function sourceText(field) {
  const parts = [];
  const label = sourceLabel(field.source);
  if (label) {
    parts.push(label);
  }
  if (field.locked) {
    parts.push("locked");
  }
  return parts.join(" ");
}

function providerName(providerId) {
  const names = {
    nvidia_nim: "NVIDIA NIM",
    open_router: "OpenRouter",
    mistral_codestral: "Mistral Codestral",
    deepseek: "DeepSeek",
    lmstudio: "LM Studio",
    llamacpp: "llama.cpp",
    ollama: "Ollama",
    kimi: "Kimi",
    wafer: "Wafer",
    opencode: "OpenCode Zen",
    opencode_go: "OpenCode Go",
    zai: "Z.ai",
    github_models: "GitHub Models",
    openai: "OpenAI",
    aerolink: "Aerolink",
  };
  if (names[providerId]) return names[providerId];
  return providerId
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function statusClass(status) {
  if (["configured", "reachable", "running"].includes(status)) return "ok";
  if (["missing_key", "missing_url", "unknown"].includes(status)) return "warn";
  if (["offline", "error"].includes(status)) return "error";
  return "neutral";
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json();
}

// Init theme from localStorage
function initTheme() {
  const saved = localStorage.getItem("fcc-admin-theme") || "dark";
  setTheme(saved);
}

function setTheme(theme) {
  state.theme = theme;
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem("fcc-admin-theme", theme);
  const btn = byId("themeToggle");
  if (btn) {
    btn.textContent = theme === "dark" ? "☀ Light Mode" : "🌙 Dark Mode";
  }
}

async function load() {
  showMessage("Loading admin config");
  const config = await api("/admin/api/config");
  state.config = config;
  state.fields = new Map(config.fields.map((field) => [field.key, field]));
  renderNav();
  renderProviders(config.provider_status);
  renderSections(config.sections, config.fields);
  byId("configPath").textContent = config.paths.managed;
  
  await loadProfiles();
  await validate(false);
  await refreshLocalStatus();
  updateDirtyState();
  showMessage("");
}

async function loadProfiles() {
  try {
    const data = await api("/admin/api/profiles");
    state.profiles = data.profiles;
    state.activeProfile = data.active;
    
    const select = byId("profileSelect");
    select.innerHTML = "";
    data.profiles.forEach(profile => {
      const opt = document.createElement("option");
      opt.value = profile;
      opt.textContent = profile === "default" ? "Default (.env)" : profile;
      opt.selected = profile === data.active;
      select.appendChild(opt);
    });
  } catch (err) {
    console.error("Failed to load profiles", err);
  }
}

function renderNav() {
  const nav = byId("sectionNav");
  nav.innerHTML = "";
  VIEW_GROUPS.forEach((view, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `nav-link${state.activeView === view.id ? " active" : ""}`;
    button.dataset.view = view.id;
    button.textContent = view.label;
    if (state.activeView === view.id) {
      button.setAttribute("aria-current", "page");
    }
    button.addEventListener("click", () => {
      setActiveView(view.id, { scroll: true });
    });
    nav.appendChild(button);
  });
}

function setActiveView(viewId, { scroll = false } = {}) {
  // If leaving logs view, close SSE
  if (state.activeView === "logs" && viewId !== "logs") {
    closeLogsStream();
  }

  state.activeView = viewId;
  const activeViewObj = VIEW_GROUPS.find((view) => view.id === viewId) || VIEW_GROUPS[0];
  byId("pageTitle").textContent = activeViewObj.title;

  document.querySelectorAll(".nav-link").forEach((link) => {
    const selected = link.dataset.view === activeViewObj.id;
    link.classList.toggle("active", selected);
    if (selected) {
      link.setAttribute("aria-current", "page");
    } else {
      link.removeAttribute("aria-current");
    }
  });

  document.querySelectorAll(".admin-view").forEach((view) => {
    const selected = view.dataset.view === activeViewObj.id;
    view.classList.toggle("active", selected);
    view.hidden = !selected;
  });

  if (viewId === "logs") {
    openLogsStream();
  } else if (viewId === "comparison") {
    renderComparisonTable();
  }

  if (scroll) {
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
}

function renderProviders(providerStatus) {
  const grid = byId("providerGrid");
  grid.innerHTML = "";
  providerStatus.forEach((provider) => {
    const card = document.createElement("article");
    card.className = "provider-card";
    card.dataset.provider = provider.provider_id;

    const title = document.createElement("div");
    title.className = "provider-title";
    title.innerHTML = `<strong>${providerName(provider.provider_id)}</strong>`;

    const pill = document.createElement("span");
    pill.className = `status-pill ${statusClass(provider.status)}`;
    pill.textContent = provider.label;
    title.appendChild(pill);

    const meta = document.createElement("div");
    meta.className = "provider-meta";
    meta.textContent =
      provider.kind === "local"
        ? provider.base_url || "No local URL configured"
        : provider.credential_env;

    const actions = document.createElement("div");
    actions.className = "card-actions";

    const testBtn = document.createElement("button");
    testBtn.type = "button";
    testBtn.className = "test-button";
    testBtn.textContent = provider.kind === "local" ? "Test" : "Refresh models";
    testBtn.addEventListener("click", () => testProvider(provider.provider_id, testBtn));
    actions.appendChild(testBtn);

    const exploreBtn = document.createElement("button");
    exploreBtn.type = "button";
    exploreBtn.className = "ghost-button";
    exploreBtn.textContent = "Explore";
    exploreBtn.addEventListener("click", () => exploreProviderModels(provider.provider_id));
    actions.appendChild(exploreBtn);

    card.append(title, meta, actions);
    grid.appendChild(card);
  });
}

function updateProviderCard(providerId, status, label, metaText) {
  const card = document.querySelector(`[data-provider="${providerId}"]`);
  if (!card) return;
  const pill = card.querySelector(".status-pill");
  pill.className = `status-pill ${statusClass(status)}`;
  pill.textContent = label;
  if (metaText) {
    card.querySelector(".provider-meta").textContent = metaText;
  }
}

function renderSections(sections, fields) {
  VIEW_GROUPS.forEach((view) => {
    if (view.containerId) {
      byId(view.containerId).innerHTML = "";
    }
  });

  const sectionById = new Map(sections.map((section) => [section.id, section]));
  const bySection = new Map();
  sections.forEach((section) => bySection.set(section.id, []));
  fields.forEach((field) => {
    if (!bySection.has(field.section)) bySection.set(field.section, []);
    bySection.get(field.section).push(field);
  });

  VIEW_GROUPS.forEach((view) => {
    if (!view.containerId) return;
    const container = byId(view.containerId);
    view.sections.forEach((sectionId) => {
      const section = sectionById.get(sectionId);
      const sectionFields = bySection.get(sectionId) || [];
      if (!section || sectionFields.length === 0) return;

      const sectionEl = document.createElement("section");
      sectionEl.className = "settings-section";
      sectionEl.id = `section-${section.id}`;

      const heading = document.createElement("div");
      heading.className = "section-heading";
      heading.innerHTML = `<div><h3>${section.label}</h3><p>${section.description}</p></div>`;
      sectionEl.appendChild(heading);

      const grid = document.createElement("div");
      grid.className = "field-grid";
      sectionFields.forEach((field) => {
        grid.appendChild(renderField(field));
      });
      sectionEl.appendChild(grid);

      if (sectionFields.some((field) => field.advanced)) {
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "ghost-button advanced-toggle";
        toggle.textContent = "Show advanced";
        toggle.addEventListener("click", () => {
          const showing = sectionEl.classList.toggle("show-advanced");
          toggle.textContent = showing ? "Hide advanced" : "Show advanced";
        });
        sectionEl.appendChild(toggle);
      }

      container.appendChild(sectionEl);
    });
  });
}

function renderField(field) {
  const wrapper = document.createElement("div");
  wrapper.className = `field${field.advanced ? " advanced-field" : ""}`;
  wrapper.dataset.key = field.key;

  const label = document.createElement("label");
  label.htmlFor = `field-${field.key}`;
  const labelText = document.createElement("span");
  labelText.textContent = field.label;
  label.appendChild(labelText);

  const source = sourceText(field);
  if (source) {
    const sourceEl = document.createElement("span");
    sourceEl.className = "field-source";
    sourceEl.textContent = source;
    label.appendChild(sourceEl);
  }

  const input = inputForField(field);
  input.id = `field-${field.key}`;
  input.dataset.key = field.key;
  input.dataset.original = field.value || "";
  input.dataset.secret = field.secret ? "true" : "false";
  input.dataset.configured = field.configured ? "true" : "false";
  input.disabled = field.locked;
  input.addEventListener("input", updateDirtyState);
  input.addEventListener("change", updateDirtyState);

  wrapper.append(label, input);
  if (field.description) {
    const description = document.createElement("div");
    description.className = "field-description";
    description.innerHTML = field.description; // allow formatted descriptions
    wrapper.appendChild(description);
  }
  return wrapper;
}

function inputForField(field) {
  if (field.key === "FALLBACK_CHAIN") {
    // Return a custom widget containing input and a reorder builder
    const container = document.createElement("div");
    container.className = "fallback-chain-widget";
    
    const input = document.createElement("input");
    input.type = "text";
    input.value = field.value || "";
    input.style.marginBottom = "8px";
    
    const builder = document.createElement("div");
    builder.style.display = "flex";
    builder.style.flexWrap = "wrap";
    builder.style.gap = "6px";
    
    const updatePills = () => {
      builder.innerHTML = "";
      const currentOrder = input.value.split(",").map(p => p.trim()).filter(Boolean);
      
      currentOrder.forEach((provider, index) => {
        const pill = document.createElement("span");
        pill.className = "status-pill neutral";
        pill.style.display = "inline-flex";
        pill.style.alignItems = "center";
        pill.style.gap = "6px";
        pill.textContent = providerName(provider);
        
        if (index > 0) {
          const up = document.createElement("span");
          up.textContent = "▲";
          up.style.cursor = "pointer";
          up.addEventListener("click", () => {
            const arr = [...currentOrder];
            [arr[index - 1], arr[index]] = [arr[index], arr[index - 1]];
            input.value = arr.join(",");
            updatePills();
            updateDirtyState();
          });
          pill.appendChild(up);
        }
        
        if (index < currentOrder.length - 1) {
          const down = document.createElement("span");
          down.textContent = "▼";
          down.style.cursor = "pointer";
          down.addEventListener("click", () => {
            const arr = [...currentOrder];
            [arr[index], arr[index + 1]] = [arr[index + 1], arr[index]];
            input.value = arr.join(",");
            updatePills();
            updateDirtyState();
          });
          pill.appendChild(down);
        }
        
        builder.appendChild(pill);
      });
    };
    
    setTimeout(updatePills, 100);
    input.addEventListener("input", updatePills);
    
    // We proxy events from the input to the wrapper container
    container.appendChild(input);
    container.appendChild(builder);
    
    // override element properties so readFieldValue and events treat the widget container like the input
    Object.defineProperty(container, "value", {
      get: () => input.value,
      set: (val) => { input.value = val; updatePills(); },
      configurable: true
    });
    container.matches = (sel) => {
      if (sel === "input, select, textarea") return true;
      return HTMLElement.prototype.matches.call(container, sel);
    };
    
    return container;
  }

  if (field.type === "boolean") {
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = String(field.value).toLowerCase() === "true";
    input.dataset.original = input.checked ? "true" : "false";
    return input;
  }

  if (field.type === "tri_boolean") {
    const select = document.createElement("select");
    [
      ["", "Inherit"],
      ["true", "Enabled"],
      ["false", "Disabled"],
    ].forEach(([value, label]) => select.appendChild(option(value, label)));
    select.value = field.value || "";
    return select;
  }

  if (field.type === "select") {
    const select = document.createElement("select");
    field.options.forEach((value) => select.appendChild(option(value, value)));
    select.value = field.value || field.options[0] || "";
    return select;
  }

  if (field.type === "textarea") {
    const textarea = document.createElement("textarea");
    textarea.value = field.value || "";
    return textarea;
  }

  const input = document.createElement("input");
  input.type = field.type === "number" ? "number" : "text";
  if (field.type === "secret") {
    input.type = "password";
    input.placeholder = field.configured
      ? "Configured - enter a new value to replace"
      : "Not configured";
    input.value = "";
    input.autocomplete = "off";
  } else {
    input.value = field.value || "";
  }
  if (field.key.startsWith("MODEL")) {
    input.setAttribute("list", "model-options");
  }
  return input;
}

function option(value, label) {
  const optionEl = document.createElement("option");
  optionEl.value = value;
  optionEl.textContent = label;
  return optionEl;
}

function readFieldValue(input) {
  if (input.type === "checkbox") return input.checked ? "true" : "false";
  if (input.dataset.secret === "true" && input.dataset.configured === "true") {
    return input.value ? input.value : MASKED_SECRET;
  }
  return input.value;
}

function changedValues() {
  const values = {};
  document.querySelectorAll("[data-key]").forEach((input) => {
    if (input.disabled || !input.matches("input, select, textarea")) return;
    const value = readFieldValue(input);
    if (value !== input.dataset.original) {
      values[input.dataset.key] = value;
    }
  });
  return values;
}

function updateDirtyState() {
  const count = Object.keys(changedValues()).length;
  byId("dirtyState").textContent =
    count === 0 ? "No changes" : `${count} unsaved change${count === 1 ? "" : "s"}`;
  byId("applyButton").disabled = count === 0;
  byId("showDiffButton").disabled = count === 0;
}

async function validate(showResult = true) {
  const result = await api("/admin/api/config/validate", {
    method: "POST",
    body: JSON.stringify({ values: changedValues() }),
  });
  if (showResult) {
    showValidationResult(result);
  }
  return result;
}

function showValidationResult(result) {
  if (result.valid) {
    showMessage("Config shape is valid", "ok");
  } else {
    showMessage(result.errors.join("; "), "error");
  }
}

async function apply() {
  const result = await api("/admin/api/config/apply", {
    method: "POST",
    body: JSON.stringify({ values: changedValues() }),
  });
  if (!result.applied) {
    showValidationResult(result);
    return;
  }
  const restart = result.restart || {};
  if (restart.required && restart.automatic) {
    showMessage("Applied. Restarting server...", "ok");
    byId("applyButton").disabled = true;
    setTimeout(() => {
      window.location.href = restart.admin_url || "/admin";
    }, 1600);
    return;
  }
  const pending = restart.required ? restart.fields || [] : result.pending_fields || [];
  await load();
  showMessage(
    pending.length
      ? `Applied. Restart fcc-server to use: ${pending.join(", ")}`
      : "Applied",
    "ok",
  );
}

async function refreshLocalStatus() {
  const result = await api("/admin/api/providers/local-status");
  result.providers.forEach((provider) => {
    state.localStatus.set(provider.provider_id, provider);
    const meta = provider.status_code
      ? `${provider.base_url} returned HTTP ${provider.status_code}`
      : provider.base_url;
    updateProviderCard(provider.provider_id, provider.status, provider.label, meta);
  });
}

async function testProvider(providerId, button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Testing";
  try {
    const result = await api(`/admin/api/providers/${providerId}/test`, {
      method: "POST",
      body: "{}",
    });
    if (result.ok) {
      updateProviderCard(
        providerId,
        "reachable",
        `${result.models.length} models`,
        `Ping: ${result.latency_ms}ms | Models: ${result.models.slice(0, 3).join(", ")}`,
      );
      state.modelOptions = Array.from(
        new Set([
          ...state.modelOptions,
          ...result.models.map((model) => `${providerId}/${model}`),
        ]),
      ).sort();
      syncModelDatalist();
    } else {
      updateProviderCard(providerId, "offline", result.error_type, `Ping error: ${result.error_message || result.error_type}`);
    }
  } finally {
    button.disabled = false;
    button.textContent = original;
    renderComparisonTable();
  }
}

function syncModelDatalist() {
  let datalist = byId("model-options");
  if (!datalist) {
    datalist = document.createElement("datalist");
    datalist.id = "model-options";
    document.body.appendChild(datalist);
  }
  datalist.innerHTML = "";
  state.modelOptions.forEach((model) => datalist.appendChild(option(model, model)));
}

function showMessage(message, kind = "") {
  const area = byId("messageArea");
  if (area) {
    area.textContent = message;
    area.className = `message-area ${kind}`.trim();
  }
}

// Live log SSE Stream
function openLogsStream() {
  closeLogsStream();
  const terminal = byId("logTerminal");
  terminal.textContent = "Opening SSE stream to fcc-server logs...\n";
  
  state.logSource = new EventSource("/admin/api/logs/stream");
  state.logSource.onmessage = (event) => {
    let text = event.data;
    try {
      // Try to pretty print if log is JSON
      const parsed = JSON.parse(event.data);
      text = `[${parsed.time}] ${parsed.level}: ${parsed.message} (${parsed.module}.${parsed.function}:${parsed.line})`;
    } catch (_) {}
    
    terminal.textContent += text + "\n";
    
    // Scroll to bottom
    terminal.scrollTop = terminal.scrollHeight;
    
    // Cap at 1000 lines
    const lines = terminal.textContent.split("\n");
    if (lines.length > 1000) {
      terminal.textContent = lines.slice(lines.length - 1000).join("\n");
    }
  };
  state.logSource.onerror = () => {
    terminal.textContent += "[SSE stream disconnected]\n";
  };
}

function closeLogsStream() {
  if (state.logSource) {
    state.logSource.close();
    state.logSource = null;
  }
}

// Comparison Benchmark Table
function renderComparisonTable() {
  const body = byId("comparisonBody");
  if (!body) return;
  body.innerHTML = "";
  
  if (!state.config || !state.config.provider_status) return;
  
  state.config.provider_status.forEach(p => {
    const row = document.createElement("tr");
    
    const nameTd = document.createElement("td");
    nameTd.innerHTML = `<strong>${providerName(p.provider_id)}</strong>`;
    
    const statusTd = document.createElement("td");
    const pill = document.createElement("span");
    pill.className = `status-pill ${statusClass(p.status)}`;
    pill.textContent = p.label;
    statusTd.appendChild(pill);
    
    const card = document.querySelector(`[data-provider="${p.provider_id}"]`);
    const cardMeta = card ? card.querySelector(".provider-meta").textContent : "";
    const pingMatch = cardMeta.match(/Ping:\s*(\d+)ms/);
    const latencyVal = pingMatch ? `${pingMatch[1]}ms` : "-";
    const latencyTd = document.createElement("td");
    latencyTd.textContent = latencyVal;
    
    const countTd = document.createElement("td");
    const countMatch = cardMeta.match(/(\d+)\s*models/);
    countTd.textContent = countMatch ? countMatch[1] : "-";
    
    const actionTd = document.createElement("td");
    const testBtn = document.createElement("button");
    testBtn.type = "button";
    testBtn.className = "ghost-button mini-button";
    testBtn.textContent = "Ping";
    testBtn.addEventListener("click", () => testProvider(p.provider_id, testBtn));
    actionTd.appendChild(testBtn);
    
    row.append(nameTd, statusTd, latencyTd, countTd, actionTd);
    body.appendChild(row);
  });
}

async function runBenchmark() {
  const btn = byId("refreshComparisonButton");
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Benchmarking...";
  
  try {
    if (!state.config || !state.config.provider_status) return;
    for (const p of state.config.provider_status) {
      await testProvider(p.provider_id, btn);
    }
  } finally {
    btn.disabled = false;
    btn.textContent = original;
    renderComparisonTable();
  }
}

// Model Explorer
async function exploreProviderModels(providerId) {
  const section = byId("modelExplorerSection");
  const list = byId("explorerModelsList");
  const title = byId("explorerTitle");
  
  section.hidden = false;
  title.textContent = `Supported Models for ${providerName(providerId)}`;
  list.innerHTML = "<p>Loading models...</p>";
  
  try {
    const data = await api(`/admin/api/providers/${providerId}/models`);
    list.innerHTML = "";
    if (data.models && data.models.length > 0) {
      data.models.forEach(model => {
        const item = document.createElement("div");
        item.className = "model-item";
        item.textContent = model;
        list.appendChild(item);
      });
    } else {
      list.innerHTML = `<p style="grid-column: 1/-1;">No models found in cache. Click <strong>Test</strong> or <strong>Ping</strong> above to query this provider.</p>`;
    }
  } catch (err) {
    list.innerHTML = `<p style="grid-column: 1/-1; color: var(--error);">Error loading models: ${err.message}</p>`;
  }
}

// Diff Generation
function showDiff() {
  const container = byId("diffContainer");
  container.innerHTML = "";
  const changes = changedValues();
  
  Object.keys(changes).forEach(key => {
    const row = document.createElement("div");
    const spec = state.fields.get(key);
    const original = spec ? spec.value : "";
    const current = changes[key];
    
    row.className = "diff-row";
    if (!original && current) {
      row.className += " diff-added";
      row.innerHTML = `<strong>${key}</strong> (Added):<br><span style="color: var(--ok);">+ ${current}</span>`;
    } else if (original && !current) {
      row.className += " diff-removed";
      row.innerHTML = `<strong>${key}</strong> (Removed):<br><span style="color: var(--error);">- ${original}</span>`;
    } else {
      row.className += " diff-changed";
      row.innerHTML = `<strong>${key}</strong> (Modified):<br><span style="color: var(--error);">- ${original}</span><br><span style="color: var(--ok);">+ ${current}</span>`;
    }
    container.appendChild(row);
  });
  
  byId("diffModal").hidden = false;
}

// Config Import / Export
function exportConfig() {
  window.location.href = "/admin/api/config/export";
}

function triggerImport() {
  byId("importFileInput").click();
}

async function handleImportFile(event) {
  const file = event.target.files[0];
  if (!file) return;
  
  const reader = new FileReader();
  reader.onload = async (e) => {
    try {
      const imported = JSON.parse(e.target.result);
      let count = 0;
      
      // Update form inputs with imported values
      document.querySelectorAll("[data-key]").forEach(input => {
        const key = input.dataset.key;
        if (Object.prototype.hasOwnProperty.call(imported, key)) {
          const val = imported[key];
          if (input.type === "checkbox") {
            input.checked = String(val).toLowerCase() === "true";
          } else {
            input.value = val || "";
          }
          // trigger input event
          input.dispatchEvent(new Event("input"));
          count++;
        }
      });
      
      showMessage(`Imported ${count} values from config file. Save to apply.`, "ok");
    } catch (err) {
      showMessage(`Failed to parse config file: ${err.message}`, "error");
    }
  };
  reader.readAsText(file);
}

// Profile management
async function switchProfile() {
  const select = byId("profileSelect");
  const profile = select.value;
  showMessage(`Switching profile to ${profile}...`, "ok");
  try {
    const res = await api("/admin/api/profiles/switch", {
      method: "POST",
      body: JSON.stringify({ profile }),
    });
    if (res.success) {
      setTimeout(() => {
        window.location.reload();
      }, 1500);
    }
  } catch (err) {
    showMessage(`Error switching profile: ${err.message}`, "error");
  }
}

async function createProfile() {
  const name = prompt("Enter new profile name (alphanumeric only):");
  if (!name) return;
  
  const cleanName = name.replace(/[^a-zA-Z0-9_-]/g, "").trim();
  if (!cleanName) {
    alert("Invalid profile name.");
    return;
  }
  
  showMessage(`Creating profile ${cleanName}...`, "ok");
  try {
    const res = await api("/admin/api/profiles/switch", {
      method: "POST",
      body: JSON.stringify({ profile: cleanName }),
    });
    if (res.success) {
      setTimeout(() => {
        window.location.reload();
      }, 1500);
    }
  } catch (err) {
    showMessage(`Error creating profile: ${err.message}`, "error");
  }
}

// Register Handlers
byId("validateButton").addEventListener("click", () => validate(true));
byId("applyButton").addEventListener("click", apply);
byId("showDiffButton").addEventListener("click", showDiff);

byId("themeToggle").addEventListener("click", () => {
  setTheme(state.theme === "dark" ? "light" : "dark");
});

byId("exportButton").addEventListener("click", exportConfig);
byId("importButton").addEventListener("click", triggerImport);
byId("importFileInput").addEventListener("change", handleImportFile);

byId("profileSelect").addEventListener("change", switchProfile);
byId("newProfileButton").addEventListener("click", createProfile);

byId("clearLogsButton").addEventListener("click", () => {
  byId("logTerminal").textContent = "";
});

byId("refreshComparisonButton").addEventListener("click", runBenchmark);

byId("closeDiffModal").addEventListener("click", () => { byId("diffModal").hidden = true; });
byId("modalCloseBtn").addEventListener("click", () => { byId("diffModal").hidden = true; });
byId("modalApplyButton").addEventListener("click", () => {
  byId("diffModal").hidden = true;
  apply();
});

// Start load
initTheme();
load().catch((error) => {
  showMessage(error.message, "error");
});
