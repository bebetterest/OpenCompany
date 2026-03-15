const STREAM_SKIP_ACTIVITY = new Set([
  "llm_token",
  "llm_reasoning",
  "shell_stream",
  "agent_response",
  "tool_call_started",
  "tool_call",
  "tool_run_submitted",
  "tool_run_updated",
  "steer_run_submitted",
  "steer_run_updated",
]);
const AGENT_META_SKIP_EVENTS = new Set([
  "llm_reasoning",
  "llm_token",
  "tool_call_started",
  "tool_call",
  "tool_run_submitted",
  "tool_run_updated",
  "steer_run_submitted",
  "steer_run_updated",
]);
const AGENT_PANEL_MIN_RENDER_INTERVAL_MS = 140;
const TOOL_RUN_FILTER_KEYS = ["all", "pending", "running", "completed", "failed", "cancelled"];
const TOOL_RUN_GROUP_KEYS = ["agent", "tool", "status"];
const STEER_RUN_FILTER_KEYS = ["all", "waiting", "completed", "cancelled"];
const STEER_RUN_GROUP_KEYS = ["agent", "status", "source"];
const MESSAGE_SYNC_EVENT_TYPES = new Set([
  "session_started",
  "session_resumed",
  "session_finalized",
  "session_failed",
  "session_interrupted",
  "agent_prompt",
  "agent_response",
  "tool_call",
  "control_message",
  "child_summaries_received",
  "agent_completed",
]);
const TASK_INPUT_MIN_ROWS = 1;
const TASK_INPUT_MAX_ROWS = 8;
const AGENT_MASONRY_MIN_COLUMN_WIDTH_PX = 360;
const AGENT_MASONRY_COLUMN_GAP_PX = 10;
const WORKFLOW_ACTIVITY_MAX_ENTRIES = 600;

const state = {
  locale: "en",
  translations: {},
  launchConfig: {
    project_dir: null,
    session_id: null,
    session_mode: "direct",
    session_mode_locked: false,
    sandbox_backend: "anthropic",
    sandbox_backend_default: "anthropic",
    sandbox_backends: ["anthropic", "none"],
    remote: null,
    can_run: false,
    can_resume: false,
  },
  runtime: {
    current_session_id: null,
    configured_resume_session_id: null,
    task: "",
    model: "",
    keep_pinned_messages: 1,
    root_agent_name: "",
    session_status: "idle",
    summary: "",
    status_message: "",
    running: false,
    runSubmitting: false,
    project_sync_action_in_progress: false,
  },
  sessionsDir: "",
  appDir: "",
  activeTab: "overview",
  workflow: {
    scale: 1,
    minScale: 0.45,
    maxScale: 2.8,
    tx: 0,
    ty: 0,
    dragging: false,
    lastX: 0,
    lastY: 0,
    expanded: false,
    nativeFullscreen: false,
    summaryTransfers: new Map(),
    nodeDetails: new Map(),
    graphDirty: true,
    activityDirty: true,
    activityNeedsFullRender: true,
    activityRenderedCount: 0,
  },
  agentFocus: {
    open: false,
    agentId: null,
  },
  agentsView: {
    roleFilter: "all",
    searchQuery: "",
  },
  steerCompose: {
    open: false,
    agentId: null,
    content: "",
    error: "",
    submitting: false,
    focusRequested: false,
  },
  setup: {
    open: false,
    mode: "project",
    sessionEntries: [],
    error: "",
    busy: false,
    loadingSessionId: null,
    workspaceSource: "local",
    sandboxBackendDraft: "anthropic",
    remoteValidateBusy: false,
    remoteValidateOk: null,
    remoteValidateStatus: "",
    remoteDraft: {
      kind: "remote_ssh",
      ssh_target: "",
      remote_dir: "",
      auth_mode: "key",
      identity_file: "",
      known_hosts_policy: "accept_new",
      remote_os: "linux",
      password_saved: false,
      remote_password: "",
    },
  },
  activityEntries: [],
  agents: new Map(),
  agentOrder: [],
  diff: {
    dirty: true,
    status: "",
    preview: null,
  },
  toolRuns: {
    dirty: true,
    loading: false,
    filter: "all",
    groupBy: "agent",
    page: { tool_runs: [], next_cursor: null },
    metrics: null,
    error: "",
    limit: 500,
    callIdToRunId: new Map(),
    eventTimelineByRunId: new Map(),
    detail: {
      open: false,
      runId: null,
      runSnapshot: null,
      loading: false,
      error: "",
    },
  },
  steerRuns: {
    dirty: true,
    loading: false,
    filter: "all",
    groupBy: "agent",
    searchQuery: "",
    page: { steer_runs: [], next_cursor: null },
    metrics: null,
    error: "",
    limit: 500,
  },
  config: {
    path: "",
    text: "",
    mtimeNs: null,
    dirty: false,
    status: "",
  },
  websocket: {
    socket: null,
    reconnectTimer: null,
    connected: false,
  },
  messages: {
    cursor: null,
    syncing: false,
    timer: null,
  },
  agentPanel: {
    dirty: true,
    lastRenderedAt: 0,
    deferredTimer: null,
    expandedSteps: new Map(),
    preserveScrollNextRender: false,
    preservedScrollTop: null,
    suppressAutoStickToBottomUntil: 0,
  },
  renderScheduled: false,
  configLoaded: false,
  taskInputSeededValue: null,
};

const dom = {
  appTitle: document.getElementById("app-title"),
  appSubtitle: document.getElementById("app-subtitle"),
  taskInput: document.getElementById("task-input"),
  modelLabel: document.getElementById("model-label"),
  modelInput: document.getElementById("model-input"),
  rootAgentNameLabel: document.getElementById("root-agent-name-label"),
  rootAgentNameInput: document.getElementById("root-agent-name-input"),
  runButton: document.getElementById("run-button"),
  terminalButton: document.getElementById("terminal-button"),
  setupButton: document.getElementById("setup-button"),
  interruptButton: document.getElementById("interrupt-button"),
  controlSummary: document.getElementById("control-summary"),
  localeEnButton: document.getElementById("locale-en-button"),
  localeZhButton: document.getElementById("locale-zh-button"),
  tabButtons: [...document.querySelectorAll(".tab-button")],
  tabOverview: document.getElementById("tab-overview"),
  tabWorkflow: document.getElementById("tab-workflow"),
  tabAgents: document.getElementById("tab-agents"),
  tabToolRuns: document.getElementById("tab-tool-runs"),
  tabSteerRuns: document.getElementById("tab-steer-runs"),
  tabDiff: document.getElementById("tab-diff"),
  tabConfig: document.getElementById("tab-config"),
  overviewLaunchTitle: document.getElementById("overview-launch-title"),
  overviewFeedTitle: document.getElementById("overview-feed-title"),
  overviewLaunch: document.getElementById("overview-launch"),
  overviewFeed: document.getElementById("overview-feed"),
  workflowPanel: document.getElementById("workflow-panel"),
  workflowTitle: document.getElementById("workflow-title"),
  workflowGraph: document.getElementById("workflow-graph"),
  workflowZoomInButton: document.getElementById("workflow-zoom-in-button"),
  workflowZoomOutButton: document.getElementById("workflow-zoom-out-button"),
  workflowZoomResetButton: document.getElementById("workflow-zoom-reset-button"),
  workflowOriginButton: document.getElementById("workflow-origin-button"),
  workflowExpandButton: document.getElementById("workflow-expand-button"),
  activityTitle: document.getElementById("activity-title"),
  activityLog: document.getElementById("activity-log"),
  agentsTitle: document.getElementById("agents-title"),
  agentsRoleFilterLabel: document.getElementById("agents-role-filter-label"),
  agentsRoleFilter: document.getElementById("agents-role-filter"),
  agentsSearchInput: document.getElementById("agents-search-input"),
  agentsLive: document.getElementById("agents-live"),
  toolRunsRefreshButton: document.getElementById("tool-runs-refresh-button"),
  toolRunsFilterButton: document.getElementById("tool-runs-filter-button"),
  toolRunsGroupButton: document.getElementById("tool-runs-group-button"),
  toolRunsStatus: document.getElementById("tool-runs-status"),
  toolRunsSummary: document.getElementById("tool-runs-summary"),
  toolRunsContent: document.getElementById("tool-runs-content"),
  steerRunsRefreshButton: document.getElementById("steer-runs-refresh-button"),
  steerRunsFilterButton: document.getElementById("steer-runs-filter-button"),
  steerRunsGroupButton: document.getElementById("steer-runs-group-button"),
  steerRunsSearchInput: document.getElementById("steer-runs-search-input"),
  steerRunsStatus: document.getElementById("steer-runs-status"),
  steerRunsSummary: document.getElementById("steer-runs-summary"),
  steerRunsContent: document.getElementById("steer-runs-content"),
  diffStatus: document.getElementById("diff-status"),
  diffContent: document.getElementById("diff-content"),
  applyButton: document.getElementById("apply-button"),
  undoButton: document.getElementById("undo-button"),
  configEditor: document.getElementById("config-editor"),
  configMeta: document.getElementById("config-meta"),
  configSyncStatus: document.getElementById("config-sync-status"),
  configSaveButton: document.getElementById("config-save-button"),
  configReloadButton: document.getElementById("config-reload-button"),
  setupOverlay: document.getElementById("setup-overlay"),
  setupTitle: document.getElementById("setup-title"),
  setupHelp: document.getElementById("setup-help"),
  setupModeProjectButton: document.getElementById("setup-mode-project"),
  setupModeSessionButton: document.getElementById("setup-mode-session"),
  setupProjectSection: document.getElementById("setup-project-section"),
  setupSessionSection: document.getElementById("setup-session-section"),
  setupProjectHelp: document.getElementById("setup-project-help"),
  setupSessionHelp: document.getElementById("setup-session-help"),
  sandboxBackendLabel: document.getElementById("sandbox-backend-label"),
  sandboxBackendSelect: document.getElementById("sandbox-backend-select"),
  sandboxBackendStatus: document.getElementById("sandbox-backend-status"),
  workspaceModeDirectButton: document.getElementById("workspace-mode-direct-button"),
  workspaceModeStagedButton: document.getElementById("workspace-mode-staged-button"),
  workspaceModeStatus: document.getElementById("workspace-mode-status"),
  workspaceSourceLocalButton: document.getElementById("workspace-source-local-button"),
  workspaceSourceRemoteButton: document.getElementById("workspace-source-remote-button"),
  setupLocalSourceSection: document.getElementById("setup-local-source-section"),
  setupRemoteSourceSection: document.getElementById("setup-remote-source-section"),
  projectPickerButton: document.getElementById("project-picker-button"),
  remoteTargetLabel: document.getElementById("remote-target-label"),
  remoteTargetInput: document.getElementById("remote-target-input"),
  remoteDirLabel: document.getElementById("remote-dir-label"),
  remoteDirInput: document.getElementById("remote-dir-input"),
  remoteAuthLabel: document.getElementById("remote-auth-label"),
  remoteAuthSelect: document.getElementById("remote-auth-select"),
  remoteKeyRow: document.getElementById("remote-key-row"),
  remoteKeyLabel: document.getElementById("remote-key-label"),
  remoteKeyInput: document.getElementById("remote-key-input"),
  remotePasswordRow: document.getElementById("remote-password-row"),
  remotePasswordLabel: document.getElementById("remote-password-label"),
  remotePasswordInput: document.getElementById("remote-password-input"),
  remoteKnownHostsLabel: document.getElementById("remote-known-hosts-label"),
  remoteKnownHostsSelect: document.getElementById("remote-known-hosts-select"),
  remoteValidateButton: document.getElementById("remote-validate-button"),
  remoteValidateStatus: document.getElementById("remote-validate-status"),
  sessionWorkspaceMode: document.getElementById("session-workspace-mode"),
  sessionPickerButton: document.getElementById("session-picker-button"),
  sessionValidateStatus: document.getElementById("session-validate-status"),
  projectCurrentPath: document.getElementById("project-current-path"),
  sessionsRootPath: document.getElementById("sessions-root-path"),
  sessionDirectoryList: document.getElementById("session-directory-list"),
  sessionRefreshButton: document.getElementById("session-refresh-button"),
  setupError: document.getElementById("setup-error"),
  setupCloseButton: document.getElementById("setup-close-button"),
  agentFocusOverlay: document.getElementById("agent-focus-overlay"),
  agentFocusTitle: document.getElementById("agent-focus-title"),
  agentFocusBody: document.getElementById("agent-focus-body"),
  agentFocusCloseButton: document.getElementById("agent-focus-close-button"),
  steerComposeOverlay: document.getElementById("steer-compose-overlay"),
  steerComposeTitle: document.getElementById("steer-compose-title"),
  steerComposePrompt: document.getElementById("steer-compose-prompt"),
  steerComposeInput: document.getElementById("steer-compose-input"),
  steerComposeError: document.getElementById("steer-compose-error"),
  steerComposeCancelButton: document.getElementById("steer-compose-cancel-button"),
  steerComposeSubmitButton: document.getElementById("steer-compose-submit-button"),
  toolRunDetailOverlay: document.getElementById("tool-run-detail-overlay"),
  toolRunDetailTitle: document.getElementById("tool-run-detail-title"),
  toolRunDetailBody: document.getElementById("tool-run-detail-body"),
  toolRunDetailCloseButton: document.getElementById("tool-run-detail-close-button"),
};

function t(key) {
  return state.translations[key] || key;
}

function scheduleRender() {
  if (state.renderScheduled) {
    return;
  }
  state.renderScheduled = true;
  requestAnimationFrame(() => {
    state.renderScheduled = false;
    render();
  });
}

function markAgentPanelDirty() {
  state.agentPanel.dirty = true;
}

function markWorkflowGraphDirty() {
  state.workflow.graphDirty = true;
}

function markWorkflowActivityDirty({ full = false } = {}) {
  state.workflow.activityDirty = true;
  if (full) {
    state.workflow.activityNeedsFullRender = true;
    state.workflow.activityRenderedCount = 0;
  }
}

function scheduleAgentPanelDeferredRender(delayMs) {
  if (state.agentPanel.deferredTimer !== null) {
    return;
  }
  const boundedDelay = Math.max(0, Math.floor(delayMs));
  state.agentPanel.deferredTimer = window.setTimeout(() => {
    state.agentPanel.deferredTimer = null;
    scheduleRender();
  }, boundedDelay);
}

async function fetchJson(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch (_error) {
    payload = null;
  }
  if (!response.ok) {
    const message =
      (payload && typeof payload.detail === "string" && payload.detail) ||
      `${response.status} ${response.statusText}`;
    throw new Error(message);
  }
  return payload;
}

async function bootstrap() {
  const payload = await fetchJson("/api/bootstrap");
  applySnapshot(payload);
  if (!state.launchConfig.can_run) {
    resetSetupSandboxBackendDraft();
    state.setup.open = true;
    state.setup.mode = "project";
  }
  await refreshSessions();
  if (activeSessionId()) {
    await loadSessionEvents(activeSessionId());
  }
  scheduleRender();
}

function applySnapshot(payload) {
  if (!payload || typeof payload !== "object") {
    return;
  }
  state.locale = payload.locale || state.locale;
  state.translations = payload.translations || state.translations;
  if (payload.launch_config) {
    state.launchConfig = {
      ...state.launchConfig,
      ...payload.launch_config,
    };
    state.launchConfig.session_mode = activeWorkspaceMode();
    state.launchConfig.session_mode_locked = Boolean(state.launchConfig.session_mode_locked);
    state.launchConfig.sandbox_backends = sanitizeSandboxBackends(state.launchConfig.sandbox_backends);
    state.launchConfig.sandbox_backend_default = normalizeSandboxBackend(
      state.launchConfig.sandbox_backend_default,
      "anthropic"
    );
    state.launchConfig.sandbox_backend = normalizeSandboxBackend(
      state.launchConfig.sandbox_backend,
      state.launchConfig.sandbox_backend_default
    );
    if (state.setup.open) {
      state.setup.sandboxBackendDraft = setupDraftSandboxBackend();
    } else {
      resetSetupSandboxBackendDraft();
    }
    state.launchConfig.remote = sanitizeRemoteConfig(state.launchConfig.remote);
    if (state.launchConfig.remote) {
      syncRemoteDraftFromLaunch();
    } else if (!isSetupRemoteSource()) {
      state.setup.workspaceSource = "local";
    }
  }
  if (payload.runtime) {
    state.runtime = {
      ...state.runtime,
      ...payload.runtime,
    };
  }
  state.appDir = payload.app_dir || state.appDir;
  state.sessionsDir = payload.sessions_dir || state.sessionsDir;
  if (Array.isArray(payload.sessions)) {
    state.setup.sessionEntries = payload.sessions;
  }
  if (payload.config_meta && typeof payload.config_meta === "object") {
    state.config.path = payload.config_meta.path || state.config.path;
    state.config.mtimeNs = payload.config_meta.mtime_ns ?? state.config.mtimeNs;
  }
  if (isDirectWorkspaceMode() && state.activeTab === "diff") {
    state.activeTab = "overview";
  }
}

function resetRuntimeViews() {
  state.activityEntries = [];
  state.agents.clear();
  state.agentOrder = [];
  state.messages.cursor = null;
  state.messages.syncing = false;
  if (state.messages.timer !== null) {
    window.clearTimeout(state.messages.timer);
    state.messages.timer = null;
  }
  markAgentPanelDirty();
  state.agentPanel.lastRenderedAt = 0;
  state.agentPanel.expandedSteps.clear();
  state.agentPanel.preserveScrollNextRender = false;
  state.agentPanel.preservedScrollTop = null;
  state.agentPanel.suppressAutoStickToBottomUntil = 0;
  if (state.agentPanel.deferredTimer !== null) {
    window.clearTimeout(state.agentPanel.deferredTimer);
    state.agentPanel.deferredTimer = null;
  }
  state.workflow.scale = 1;
  state.workflow.tx = 0;
  state.workflow.ty = 0;
  state.workflow.dragging = false;
  state.workflow.expanded = false;
  state.workflow.nativeFullscreen = false;
  state.workflow.summaryTransfers.clear();
  state.workflow.nodeDetails.clear();
  state.workflow.graphDirty = true;
  state.workflow.activityDirty = true;
  state.workflow.activityNeedsFullRender = true;
  state.workflow.activityRenderedCount = 0;
  document.body.classList.remove("workflow-immersive");
  state.agentFocus.open = false;
  state.agentFocus.agentId = null;
  resetSteerComposeState();
  state.diff.dirty = true;
  state.toolRuns.dirty = true;
  state.toolRuns.error = "";
  state.toolRuns.page = { tool_runs: [], next_cursor: null };
  state.toolRuns.metrics = null;
  state.toolRuns.callIdToRunId.clear();
  state.toolRuns.eventTimelineByRunId.clear();
  state.toolRuns.detail.open = false;
  state.toolRuns.detail.runId = null;
  state.toolRuns.detail.runSnapshot = null;
  state.toolRuns.detail.loading = false;
  state.toolRuns.detail.error = "";
  state.steerRuns.dirty = true;
  state.steerRuns.error = "";
  state.steerRuns.page = { steer_runs: [], next_cursor: null };
  state.steerRuns.metrics = null;
  if (dom.activityLog) {
    dom.activityLog.innerHTML = "";
  }
}

function activeSessionId() {
  return (
    state.runtime.current_session_id ||
    state.runtime.configured_resume_session_id ||
    state.launchConfig.session_id
  );
}

function activeWorkspaceMode() {
  const value = String(state.launchConfig.session_mode || "direct").trim().toLowerCase();
  return value === "staged" ? "staged" : "direct";
}

function sanitizeSandboxBackends(rawBackends) {
  const defaults = ["anthropic", "none"];
  const seen = new Set();
  const normalized = [];
  if (Array.isArray(rawBackends)) {
    for (const item of rawBackends) {
      const candidate = String(item || "").trim().toLowerCase();
      if (!candidate || seen.has(candidate)) {
        continue;
      }
      seen.add(candidate);
      normalized.push(candidate);
    }
  }
  for (const fallback of defaults) {
    if (!seen.has(fallback)) {
      seen.add(fallback);
      normalized.push(fallback);
    }
  }
  return normalized;
}

function availableSandboxBackends() {
  return sanitizeSandboxBackends(state.launchConfig.sandbox_backends);
}

function normalizeSandboxBackend(backend, fallback = null) {
  const candidate = String(backend || "").trim().toLowerCase();
  const backends = availableSandboxBackends();
  if (backends.includes(candidate)) {
    return candidate;
  }
  const fallbackCandidate = String(fallback || "").trim().toLowerCase();
  if (backends.includes(fallbackCandidate)) {
    return fallbackCandidate;
  }
  return backends[0] || "anthropic";
}

function defaultSandboxBackend() {
  return normalizeSandboxBackend(state.launchConfig.sandbox_backend_default, "anthropic");
}

function activeSandboxBackend() {
  return normalizeSandboxBackend(state.launchConfig.sandbox_backend, defaultSandboxBackend());
}

function setupDraftSandboxBackend() {
  return normalizeSandboxBackend(state.setup.sandboxBackendDraft, defaultSandboxBackend());
}

function resetSetupSandboxBackendDraft() {
  state.setup.sandboxBackendDraft = defaultSandboxBackend();
}

function isSetupWorkspaceModeLocked() {
  return state.setup.mode === "session" && Boolean(state.launchConfig.session_mode_locked);
}

function isDirectWorkspaceMode() {
  return activeWorkspaceMode() === "direct";
}

function sanitizeRemoteConfig(remote) {
  if (!remote || typeof remote !== "object") {
    return null;
  }
  const sshTarget = String(remote.ssh_target || "").trim();
  const remoteDir = String(remote.remote_dir || "").trim();
  if (!sshTarget || !remoteDir) {
    return null;
  }
  const authMode = String(remote.auth_mode || "key").trim().toLowerCase();
  const knownHostsPolicy = String(remote.known_hosts_policy || "accept_new")
    .trim()
    .toLowerCase();
  const identityFile = String(remote.identity_file || "").trim();
  const passwordSaved = Boolean(remote.password_saved);
  return {
    kind: "remote_ssh",
    ssh_target: sshTarget,
    remote_dir: remoteDir,
    auth_mode: authMode === "password" ? "password" : "key",
    identity_file: authMode === "password" ? "" : identityFile,
    known_hosts_policy: knownHostsPolicy === "strict" ? "strict" : "accept_new",
    remote_os: "linux",
    password_saved: authMode === "password" ? passwordSaved : false,
  };
}

function activeRemoteConfig() {
  return sanitizeRemoteConfig(state.launchConfig.remote);
}

function isRemoteWorkspaceConfigured() {
  return Boolean(activeRemoteConfig());
}

function remoteWorkspaceLabel() {
  const remote = activeRemoteConfig();
  if (!remote) {
    return t("unset_value");
  }
  return `${remote.ssh_target}:${remote.remote_dir}`;
}

function syncRemoteDraftFromLaunch() {
  const remote = activeRemoteConfig();
  if (!remote) {
    return;
  }
  state.setup.workspaceSource = "remote";
  state.setup.remoteDraft = {
    ...state.setup.remoteDraft,
    ...remote,
    remote_password: String(state.setup.remoteDraft.remote_password || ""),
  };
}

function isSetupRemoteSource() {
  return String(state.setup.workspaceSource || "local") === "remote";
}

function remotePayloadFromDraft() {
  return sanitizeRemoteConfig(state.setup.remoteDraft);
}

function ensureAgent(record, details) {
  const agentId = String(record.agent_id || "");
  if (!agentId) {
    return null;
  }
  let agent = state.agents.get(agentId);
  let graphChanged = false;
  if (!agent) {
    const configuredKeepPinned = coerceNonNegativeInt(state.runtime.keep_pinned_messages);
    agent = {
      id: agentId,
      name:
        String(details.agent_name || details.root_agent_name || details.name || agentId).trim() ||
        agentId,
      status: "pending",
      role: String(details.agent_role || details.root_agent_role || "worker"),
      model: String(details.agent_model || details.model || ""),
      instruction: String(details.instruction || details.task || ""),
      parentAgentId: record.parent_agent_id ? String(record.parent_agent_id) : null,
      stepCount: 0,
      lastEvent: "idle",
      lastPhase: "runtime",
      lastDetail: "",
      summary: "",
      outputTokensTotal: 0,
      currentContextTokens: 0,
      contextLimitTokens: 0,
      usageRatio: 0,
      compressionCount: 0,
      keepPinnedMessages: configuredKeepPinned === null ? 1 : configuredKeepPinned,
      summaryVersion: 0,
      contextLatestSummary: "",
      summarizedUntilMessageIndex: null,
      lastUsageInputTokens: null,
      lastUsageOutputTokens: null,
      lastUsageCacheReadTokens: null,
      lastUsageCacheWriteTokens: null,
      lastUsageTotalTokens: null,
      lastCompactedMessageRange: null,
      lastCompactedStepRange: null,
      compactedStepRanges: [],
      contextWarningCount: 0,
      lastContextWarningRatio: null,
      lastContextWarningTokens: null,
      lastContextWarningLimit: null,
      isGenerating: false,
      stepOrder: [],
      stepEntries: new Map(),
      nextMessageStep: 1,
      lastMessageIndex: -1,
    };
    state.agents.set(agentId, agent);
    state.agentOrder.push(agentId);
    graphChanged = true;
  }
  if (record.parent_agent_id) {
    const parentAgentId = String(record.parent_agent_id);
    if (agent.parentAgentId !== parentAgentId) {
      agent.parentAgentId = parentAgentId;
      graphChanged = true;
    }
  }
  if (details.agent_name || details.root_agent_name || details.name) {
    const nextName = String(details.agent_name || details.root_agent_name || details.name);
    if (agent.name !== nextName) {
      agent.name = nextName;
      graphChanged = true;
    }
  }
  if (details.agent_role || details.root_agent_role) {
    const nextRole = String(details.agent_role || details.root_agent_role);
    if (agent.role !== nextRole) {
      agent.role = nextRole;
      graphChanged = true;
    }
  }
  if (details.agent_model || details.model) {
    const nextModel = String(details.agent_model || details.model);
    if (agent.model !== nextModel) {
      agent.model = nextModel;
      graphChanged = true;
    }
  }
  if (details.instruction || details.task) {
    const nextInstruction = String(details.instruction || details.task);
    if (agent.instruction !== nextInstruction) {
      agent.instruction = nextInstruction;
      graphChanged = true;
    }
  }
  if (graphChanged) {
    markWorkflowGraphDirty();
  }
  return agent;
}

function applyAgentSnapshot(records) {
  if (!Array.isArray(records)) {
    return;
  }
  for (const row of records) {
    if (!row || typeof row !== "object") {
      continue;
    }
    const agentId = String(row.id || row.agent_id || "").trim();
    if (!agentId) {
      continue;
    }
    const rawParentAgentId = row.parent_agent_id;
    const parentAgentId =
      rawParentAgentId === null || rawParentAgentId === undefined
        ? null
        : String(rawParentAgentId).trim() || null;
    const details = {
      agent_name: row.name,
      agent_role: row.role,
      instruction: row.instruction,
      step_count: row.step_count,
      agent_status: row.status,
      agent_model: row.model,
    };
    const agent = ensureAgent(
      {
        agent_id: agentId,
        parent_agent_id: parentAgentId,
      },
      details
    );
    if (!agent) {
      continue;
    }
    agent.parentAgentId = parentAgentId;
    if (row.status) {
      agent.status = String(row.status);
    }
    const stepCount = Number(row.step_count);
    if (Number.isFinite(stepCount) && stepCount >= 0) {
      const normalized = Math.floor(stepCount);
      agent.stepCount = Math.max(Number(agent.stepCount || 0), normalized);
      agent.nextMessageStep = Math.max(Number(agent.nextMessageStep || 1), normalized + 1);
    }
    if (row.summary !== undefined && row.summary !== null) {
      agent.summary = String(row.summary);
    }
    if (row.model !== undefined && row.model !== null) {
      agent.model = String(row.model);
    }
    const contextTokens = coerceNonNegativeInt(row.current_context_tokens);
    if (contextTokens !== null) {
      agent.currentContextTokens = contextTokens;
    }
    const contextLimit = coerceNonNegativeInt(row.context_limit_tokens);
    if (contextLimit !== null) {
      agent.contextLimitTokens = contextLimit;
    }
    const usageRatio = Number(row.usage_ratio);
    if (Number.isFinite(usageRatio) && usageRatio >= 0) {
      agent.usageRatio = usageRatio;
    }
    const compressionCount = coerceNonNegativeInt(row.compression_count);
    if (compressionCount !== null) {
      agent.compressionCount = compressionCount;
    }
    applyContextSummaryMetrics(agent, row);
    applyUsageMetrics(agent, row);
    if (row.last_compacted_message_range && typeof row.last_compacted_message_range === "object") {
      agent.lastCompactedMessageRange = normalizeRange(row.last_compacted_message_range);
    }
    if (row.last_compacted_step_range && typeof row.last_compacted_step_range === "object") {
      const normalized = normalizeRange(row.last_compacted_step_range);
      agent.lastCompactedStepRange = normalized;
      recordCompactedStepRange(agent, normalized);
    }
  }
  markAgentPanelDirty();
  markWorkflowGraphDirty();
}

function stepNumberFor(agent, details) {
  const candidate = Number(details.step_count ?? agent.stepCount);
  if (Number.isFinite(candidate) && candidate > 0) {
    return Math.floor(candidate);
  }
  if (agent.stepOrder.length > 0) {
    return agent.stepOrder[agent.stepOrder.length - 1];
  }
  return 1;
}

function ensureStepEntries(agent, stepNumber) {
  if (!agent.stepEntries.has(stepNumber)) {
    agent.stepEntries.set(stepNumber, []);
    if (!agent.stepOrder.includes(stepNumber)) {
      agent.stepOrder.push(stepNumber);
      agent.stepOrder.sort((a, b) => a - b);
    }
  }
  return agent.stepEntries.get(stepNumber);
}

function appendStepEntry(agent, stepNumber, kind, text, options = {}) {
  if (!text) {
    return;
  }
  const entries = ensureStepEntries(agent, stepNumber);
  const merge = Boolean(options.merge);
  if (!merge && entries.length > 0) {
    const last = entries[entries.length - 1];
    if (last.kind === kind && String(last.text || "") === String(text)) {
      return;
    }
  }
  if (merge && entries.length > 0 && entries[entries.length - 1].kind === kind) {
    entries[entries.length - 1].text += text;
  } else {
    entries.push({ kind, text });
  }
}

function stepHasEntry(agent, stepNumber, kind, text) {
  const entries = ensureStepEntries(agent, stepNumber);
  const targetKind = String(kind || "");
  const targetText = String(text || "");
  return entries.some(
    (entry) =>
      String(entry.kind || "") === targetKind &&
      String(entry.text || "") === targetText
  );
}

function isPreviewKind(kind) {
  return String(kind || "").endsWith("_preview");
}

function isNonMessageKind(kind) {
  const text = String(kind || "");
  return text.endsWith("_extra") || isPreviewKind(text);
}

function baseStreamKind(kind) {
  const text = String(kind || "");
  if (text.endsWith("_extra")) {
    return text.slice(0, -6);
  }
  if (text.endsWith("_preview")) {
    return text.slice(0, -8);
  }
  return text;
}

function extraKind(kind) {
  return `${String(kind || "")}_extra`;
}

function clearPreviewEntries(agent, stepNumber) {
  if (!agent.stepEntries.has(stepNumber)) {
    return;
  }
  const entries = agent.stepEntries.get(stepNumber) || [];
  const filtered = entries.filter((entry) => !isPreviewKind(entry.kind));
  if (filtered.length === entries.length) {
    return;
  }
  agent.stepEntries.set(stepNumber, filtered);
}

function normalizeStreamText(value) {
  return String(value || "").replace(/\r\n/g, "\n").trim();
}

function extractJsonObject(text) {
  const source = String(text || "").trim();
  if (!source) {
    return null;
  }
  const fenced = source.match(/```json\s*([\s\S]*?)\s*```/i);
  const candidate = fenced ? String(fenced[1] || "").trim() : source;
  if (!candidate) {
    return null;
  }
  try {
    const parsed = JSON.parse(candidate);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
  } catch (_error) {
    // Fall through to best-effort object extraction from surrounding text.
  }
  const start = candidate.indexOf("{");
  const end = candidate.lastIndexOf("}");
  if (start < 0 || end <= start) {
    return null;
  }
  try {
    const parsed = JSON.parse(candidate.slice(start, end + 1));
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
  } catch (_error) {
    return null;
  }
}

function appendAgentResponseEntry(agent, stepNumber, content) {
  const normalizedContent = normalizeStreamText(content);
  if (!normalizedContent) {
    return;
  }
  const entries = ensureStepEntries(agent, stepNumber);
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    const kind = String(entry.kind || "");
    if (kind !== "reply" && kind !== "response") {
      continue;
    }
    const normalizedExisting = normalizeStreamText(entry.text);
    if (!normalizedExisting) {
      continue;
    }
    const overlaps =
      normalizedExisting === normalizedContent ||
      normalizedExisting.endsWith(normalizedContent) ||
      normalizedContent.endsWith(normalizedExisting);
    if (overlaps) {
      if (normalizedContent.length > normalizedExisting.length) {
        entry.text = content;
      }
      return;
    }
    break;
  }
  appendStepEntry(agent, stepNumber, "response", content);
}

function safeInt(value, fallback = -1) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  return Math.floor(parsed);
}

function coerceNonNegativeInt(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return null;
  }
  const normalized = Math.floor(parsed);
  if (normalized < 0) {
    return null;
  }
  return normalized;
}

function normalizeRange(value) {
  if (!value || typeof value !== "object") {
    return null;
  }
  const start = coerceNonNegativeInt(value.start);
  const end = coerceNonNegativeInt(value.end);
  if (start === null || end === null || start <= 0 || end < start) {
    return null;
  }
  return { start, end };
}

function compactedRangeText(range) {
  const normalized = normalizeRange(range);
  if (!normalized) {
    return t("none_value");
  }
  if (normalized.start === normalized.end) {
    return `${t("step_label")} ${normalized.start}`;
  }
  return `${t("step_label")} ${normalized.start}-${normalized.end}`;
}

function applyUsageMetrics(agent, source) {
  if (!agent || !source || typeof source !== "object") {
    return;
  }
  const fields = [
    ["last_usage_input_tokens", "lastUsageInputTokens"],
    ["last_usage_output_tokens", "lastUsageOutputTokens"],
    ["last_usage_cache_read_tokens", "lastUsageCacheReadTokens"],
    ["last_usage_cache_write_tokens", "lastUsageCacheWriteTokens"],
    ["last_usage_total_tokens", "lastUsageTotalTokens"],
  ];
  for (const [sourceKey, agentKey] of fields) {
    if (!Object.prototype.hasOwnProperty.call(source, sourceKey)) {
      continue;
    }
    const normalized = coerceNonNegativeInt(source[sourceKey]);
    agent[agentKey] = normalized === null ? null : normalized;
  }
}

function applyContextSummaryMetrics(agent, source) {
  if (!agent || !source || typeof source !== "object") {
    return;
  }
  if (Object.prototype.hasOwnProperty.call(source, "keep_pinned_messages")) {
    const keepPinnedMessages = coerceNonNegativeInt(source.keep_pinned_messages);
    if (keepPinnedMessages !== null) {
      agent.keepPinnedMessages = keepPinnedMessages;
    }
  }
  if (Object.prototype.hasOwnProperty.call(source, "summary_version")) {
    const summaryVersion = coerceNonNegativeInt(source.summary_version);
    agent.summaryVersion = summaryVersion === null ? 0 : summaryVersion;
  }
  if (Object.prototype.hasOwnProperty.call(source, "context_latest_summary")) {
    const rawSummary = source.context_latest_summary;
    agent.contextLatestSummary = rawSummary === null || rawSummary === undefined ? "" : String(rawSummary);
  }
  if (Object.prototype.hasOwnProperty.call(source, "summarized_until_message_index")) {
    const parsed = Number(source.summarized_until_message_index);
    if (!Number.isFinite(parsed)) {
      agent.summarizedUntilMessageIndex = null;
    } else {
      const normalized = Math.floor(parsed);
      agent.summarizedUntilMessageIndex = normalized < -1 ? -1 : normalized;
    }
  }
}

function tokenMetricText(value) {
  const normalized = coerceNonNegativeInt(value);
  if (normalized === null) {
    return "-";
  }
  return String(normalized);
}

function summaryVersionText(agent) {
  const summaryVersion = coerceNonNegativeInt(agent && agent.summaryVersion);
  if (summaryVersion === null || summaryVersion <= 0) {
    return t("none_value");
  }
  return `v${summaryVersion}`;
}

function contextSummaryText(agent) {
  return String((agent && agent.contextLatestSummary) || "").trim();
}

function hasContextSummary(agent) {
  if (!agent || typeof agent !== "object") {
    return false;
  }
  if (contextSummaryText(agent)) {
    return true;
  }
  const summaryVersion = coerceNonNegativeInt(agent.summaryVersion);
  return summaryVersion !== null && summaryVersion > 0;
}

function summarizedStepNumbers(agent) {
  const summarized = new Set();
  if (!agent || !Array.isArray(agent.compactedStepRanges)) {
    return summarized;
  }
  for (const rangeValue of agent.compactedStepRanges) {
    const normalized = normalizeRange(rangeValue);
    if (!normalized) {
      continue;
    }
    for (let step = normalized.start; step <= normalized.end; step += 1) {
      summarized.add(step);
    }
  }
  return summarized;
}

function pinnedStepNumbers(agent, orderedSteps) {
  const keepCount = coerceNonNegativeInt(agent && agent.keepPinnedMessages);
  if (keepCount === null || keepCount <= 0 || !Array.isArray(orderedSteps) || orderedSteps.length === 0) {
    return [];
  }
  return orderedSteps.slice(0, Math.min(keepCount, orderedSteps.length));
}

function usageCacheText(agent) {
  const readTokens = coerceNonNegativeInt(agent && agent.lastUsageCacheReadTokens);
  const writeTokens = coerceNonNegativeInt(agent && agent.lastUsageCacheWriteTokens);
  if (readTokens === null && writeTokens === null) {
    return t("none_value");
  }
  return `${t("token_cache_read_short")} ${tokenMetricText(readTokens)} / ${t(
    "token_cache_write_short"
  )} ${tokenMetricText(writeTokens)}`;
}

function usageTotalText(agent) {
  const totalTokens = coerceNonNegativeInt(agent && agent.lastUsageTotalTokens);
  const inputTokens = coerceNonNegativeInt(agent && agent.lastUsageInputTokens);
  const outputTokens = coerceNonNegativeInt(agent && agent.lastUsageOutputTokens);
  if (totalTokens === null && inputTokens === null && outputTokens === null) {
    return t("none_value");
  }
  const resolvedTotal =
    totalTokens !== null ? totalTokens : Math.max(0, Number(inputTokens || 0) + Number(outputTokens || 0));
  if (inputTokens === null && outputTokens === null) {
    return tokenMetricText(resolvedTotal);
  }
  return `${tokenMetricText(resolvedTotal)} (${t("token_input_short")} ${tokenMetricText(
    inputTokens
  )} + ${t("token_output_short")} ${tokenMetricText(outputTokens)})`;
}

function contextWarningSummaryText(agent) {
  const count = Math.max(0, Number((agent && agent.contextWarningCount) || 0));
  if (count <= 0) {
    return t("none_value");
  }
  const ratioValue = Number(agent && agent.lastContextWarningRatio);
  const tokens = coerceNonNegativeInt(agent && agent.lastContextWarningTokens);
  const limit = coerceNonNegativeInt(agent && agent.lastContextWarningLimit);
  const ratioText = Number.isFinite(ratioValue) && ratioValue >= 0 ? ratioValue.toFixed(4) : "-";
  if (tokens === null || limit === null || limit <= 0) {
    return `${count} · ${ratioText}`;
  }
  return `${count} · ${ratioText} (${tokens}/${limit})`;
}

function recordCompactedStepRange(agent, range) {
  if (!agent || !Array.isArray(agent.compactedStepRanges)) {
    return;
  }
  const normalized = normalizeRange(range);
  if (!normalized) {
    return;
  }
  const exists = agent.compactedStepRanges.some((item) => {
    const current = normalizeRange(item);
    return current && current.start === normalized.start && current.end === normalized.end;
  });
  if (!exists) {
    agent.compactedStepRanges.push(normalized);
    agent.compactedStepRanges.sort((a, b) => a.start - b.start || a.end - b.end);
  }
  agent.lastCompactedStepRange = normalized;
}

function outputTokensFromUsage(usage) {
  if (!usage || typeof usage !== "object") {
    return 0;
  }
  for (const key of [
    "output_tokens",
    "completion_tokens",
    "assistant_tokens",
    "generated_tokens",
    "response_tokens",
  ]) {
    const value = coerceNonNegativeInt(usage[key]);
    if (value !== null) {
      return value;
    }
  }
  const totalTokens = coerceNonNegativeInt(usage.total_tokens);
  const promptTokens = coerceNonNegativeInt(usage.prompt_tokens);
  const inputTokens = coerceNonNegativeInt(usage.input_tokens);
  const baseline = promptTokens !== null ? promptTokens : inputTokens;
  if (totalTokens !== null && baseline !== null) {
    return Math.max(0, totalTokens - baseline);
  }
  return 0;
}

function outputTokensFromMessageRecord(record) {
  const message = record && typeof record.message === "object" ? record.message : null;
  const response = record && typeof record.response === "object" ? record.response : null;
  const usageCandidates = [];
  if (response && typeof response.usage === "object") {
    usageCandidates.push(response.usage);
  }
  if (record && typeof record.usage === "object") {
    usageCandidates.push(record.usage);
  }
  if (message && typeof message.usage === "object") {
    usageCandidates.push(message.usage);
  }
  for (const usage of usageCandidates) {
    const tokens = outputTokensFromUsage(usage);
    if (tokens > 0) {
      return tokens;
    }
  }
  return 0;
}

function normalizeMessageToolCalls(rawToolCalls) {
  if (!Array.isArray(rawToolCalls)) {
    return [];
  }
  const normalized = [];
  for (const toolCall of rawToolCalls) {
    if (!toolCall || typeof toolCall !== "object") {
      continue;
    }
    const functionPayload = toolCall.function;
    if (functionPayload && typeof functionPayload === "object") {
      const argumentsValue = functionPayload.arguments;
      let parsedArguments = argumentsValue;
      if (typeof argumentsValue === "string") {
        try {
          const parsed = extractJsonObject(argumentsValue);
          parsedArguments = parsed !== null ? parsed : argumentsValue;
        } catch (_error) {
          parsedArguments = argumentsValue;
        }
      }
      normalized.push({
        id: toolCall.id,
        name: functionPayload.name,
        arguments: parsedArguments,
      });
      continue;
    }
    normalized.push(toolCall);
  }
  return normalized;
}

function formatStructuredValue(value, { fallback = "" } = {}) {
  if (value === undefined || value === null) {
    return fallback;
  }
  if (typeof value === "string") {
    const normalized = value.trim();
    if (!normalized) {
      return fallback;
    }
    const parsed = tryParseJson(normalized);
    if (parsed !== null) {
      try {
        return JSON.stringify(parsed, null, 2);
      } catch (_error) {
        return normalized;
      }
    }
    return normalized;
  }
  if (typeof value === "object") {
    try {
      return JSON.stringify(value, null, 2);
    } catch (_error) {
      return String(value);
    }
  }
  return String(value);
}

function formatLabeledStructuredValue(label, value, { fallback = "" } = {}) {
  const rendered = formatStructuredValue(value, { fallback });
  if (!rendered) {
    return `${label}:`;
  }
  if (rendered.includes("\n")) {
    return `${label}:\n${rendered}`;
  }
  return `${label}: ${rendered}`;
}

function messageStepNumber(agent, record, role) {
  const nextStep = Math.max(1, Number(agent.nextMessageStep || 1));
  const derivedStep = role === "assistant" ? nextStep : Math.max(1, nextStep - 1);
  const explicitStep = safeInt(record && record.step_count, 0);
  if (explicitStep > 0) {
    return Math.max(derivedStep, explicitStep);
  }
  return derivedStep;
}

function applyMessageRecord(record) {
  if (!record || typeof record !== "object") {
    return;
  }
  const agentId = String(record.agent_id || "").trim();
  if (!agentId) {
    return;
  }
  const details = {
    agent_name: record.agent_name,
    agent_role: record.agent_role,
  };
  const agent = ensureAgent({ agent_id: agentId }, details);
  if (!agent) {
    return;
  }
  const messageIndex = safeInt(record.message_index, -1);
  if (messageIndex <= Number(agent.lastMessageIndex || -1)) {
    return;
  }
  const hiddenFromStepView =
    Boolean(record.internal) || Boolean(record.exclude_from_context_compression);
  if (hiddenFromStepView) {
    agent.lastMessageIndex = Math.max(Number(agent.lastMessageIndex || -1), messageIndex);
    return;
  }
  const role = String(record.role || "").trim();
  const message = record.message && typeof record.message === "object" ? record.message : {};
  const explicitStep = safeInt(record.step_count, 0);
  const stepNumber = messageStepNumber(agent, record, role);
  clearPreviewEntries(agent, stepNumber);

  if (role === "assistant") {
    const content = String(message.content || "");
    const reasoning = String(message.reasoning || "").trim();
    const toolCalls = normalizeMessageToolCalls(message.tool_calls);
    const outputTokens = outputTokensFromMessageRecord(record);
    if (outputTokens > 0) {
      agent.outputTokensTotal = Math.max(0, safeInt(agent.outputTokensTotal, 0)) + outputTokens;
    }
    const entries = responseStreamEntriesForMessage(content, {
      reasoning,
      toolCalls,
    });
    for (const entry of entries) {
      const kind = String(entry.kind || "");
      const text = String(entry.text || "");
      if (!kind || !text) {
        continue;
      }
      if (!stepHasEntry(agent, stepNumber, kind, text)) {
        appendStepEntry(agent, stepNumber, kind, text);
      }
    }
    agent.stepCount = Math.max(Number(agent.stepCount || 0), stepNumber);
    agent.nextMessageStep = Math.max(Number(agent.nextMessageStep || 1), stepNumber + 1);
    agent.isGenerating = false;
  } else if (role === "tool") {
    const contentText = String(message.content || "").trim();
    const toolCallId = String(message.tool_call_id || "").trim();
    const toolLines = [];
    if (toolCallId) {
      toolLines.push(`tool_call_id=${toolCallId}`);
    }
    if (contentText) {
      const parsedContent = tryParseJson(contentText);
      if (parsedContent !== null) {
        toolLines.push(formatLabeledStructuredValue("content", parsedContent));
      } else {
        toolLines.push(contentText);
      }
    }
    const toolBody = toolLines.join("\n");
    const normalizedToolBody = toolBody.trim();
    if (normalizedToolBody) {
      appendStepEntry(agent, stepNumber, "tool_message", normalizedToolBody);
      agent.stepCount = Math.max(Number(agent.stepCount || 0), stepNumber);
    }
  } else {
    const userBody = String(message.content || "").trim();
    if (userBody) {
      appendStepEntry(agent, stepNumber, "user_message", userBody);
      agent.stepCount = Math.max(Number(agent.stepCount || 0), stepNumber);
    }
  }
  agent.lastMessageIndex = messageIndex;
}

function applyMessageRecords(records) {
  if (!Array.isArray(records)) {
    return;
  }
  const ordered = [...records].sort((left, right) => {
    const leftTimestamp = String((left && left.timestamp) || "");
    const rightTimestamp = String((right && right.timestamp) || "");
    if (leftTimestamp !== rightTimestamp) {
      return leftTimestamp.localeCompare(rightTimestamp);
    }
    const leftAgent = String((left && left.agent_id) || "");
    const rightAgent = String((right && right.agent_id) || "");
    if (leftAgent !== rightAgent) {
      return leftAgent.localeCompare(rightAgent);
    }
    return safeInt(left && left.message_index, -1) - safeInt(right && right.message_index, -1);
  });
  for (const record of ordered) {
    applyMessageRecord(record);
  }
  markAgentPanelDirty();
}

function nonMessageEntries(agent) {
  const rows = [];
  for (const step of effectiveStepOrder(agent)) {
    for (const entry of entriesForStep(agent, step)) {
      if (!isNonMessageKind(entry.kind)) {
        continue;
      }
      rows.push({
        step,
        kind: String(entry.kind || ""),
        text: String(entry.text || ""),
      });
    }
  }
  return rows;
}

function resetAgentMessageEntries({ preserveNonMessage = true } = {}) {
  const preserved = new Map();
  if (preserveNonMessage) {
    for (const [agentId, agent] of state.agents.entries()) {
      preserved.set(agentId, nonMessageEntries(agent));
    }
  }
  for (const agent of state.agents.values()) {
    agent.stepEntries = new Map();
    agent.stepOrder = [];
    agent.nextMessageStep = 1;
    agent.lastMessageIndex = -1;
    agent.outputTokensTotal = 0;
    agent.isGenerating = false;
  }
  if (preserveNonMessage) {
    for (const [agentId, entries] of preserved.entries()) {
      const agent = state.agents.get(agentId);
      if (!agent) {
        continue;
      }
      for (const row of entries) {
        appendStepEntry(agent, row.step, row.kind, row.text);
      }
    }
  }
  markAgentPanelDirty();
}

function responseStreamEntriesForMessage(rawText, { reasoning = "", toolCalls = null } = {}) {
  const entries = [];
  const normalizedReasoning = String(reasoning || "").trim();
  if (normalizedReasoning) {
    entries.push({ kind: "thinking", text: normalizedReasoning });
  }
  for (const entry of actionEntriesFromMessage(rawText)) {
    if (!entries.some((candidate) => candidate.kind === entry.kind && candidate.text === entry.text)) {
      entries.push(entry);
    }
  }
  for (const entry of toolCallTraceEntries(toolCalls)) {
    if (!entries.some((candidate) => candidate.kind === entry.kind && candidate.text === entry.text)) {
      entries.push(entry);
    }
  }
  if (entries.length === 0) {
    const fallback = String(rawText || "").trim();
    if (fallback) {
      entries.push({ kind: "response", text: fallback });
    }
  }
  return entries;
}

function actionEntriesFromMessage(rawText) {
  const payload = extractJsonObject(String(rawText || ""));
  if (!payload || typeof payload !== "object") {
    const fallback = String(rawText || "").trim();
    return fallback ? [{ kind: "response", text: fallback }] : [];
  }
  const entries = [];
  const thinking = String(payload.thinking || "").trim();
  if (thinking) {
    entries.push({ kind: "thinking", text: thinking });
  }
  const actions = Array.isArray(payload.actions) ? payload.actions : [];
  for (const entry of actionEntries(actions)) {
    entries.push(entry);
  }
  return entries;
}

function summaryTransferKey(childId, parentId) {
  return `${childId}::${parentId}`;
}

function registerChildSummaryTransfer(childId, parentId, timestampText) {
  const normalizedChildId = String(childId || "").trim();
  const normalizedParentId = String(parentId || "").trim();
  if (!normalizedChildId || !normalizedParentId) {
    return;
  }
  const key = summaryTransferKey(normalizedChildId, normalizedParentId);
  const existing = state.workflow.summaryTransfers.get(key);
  if (existing) {
    existing.count += 1;
    existing.timestamp = timestampText || existing.timestamp;
    state.workflow.summaryTransfers.set(key, existing);
    markWorkflowGraphDirty();
    return;
  }
  state.workflow.summaryTransfers.set(key, {
    childId: normalizedChildId,
    parentId: normalizedParentId,
    timestamp: timestampText || "",
    count: 1,
  });
  markWorkflowGraphDirty();
}

function summarizeAction(action) {
  if (!action || typeof action !== "object") {
    return String(action || "");
  }
  const type = String(action.type || "action");
  const toolCallSuffix = toolCallIdSuffix(action);
  if (type === "shell") {
    const command = String(action.command || "").slice(0, 120);
    return command ? `shell${toolCallSuffix}: ${command}` : `shell${toolCallSuffix}`;
  }
  if (type === "wait_time") {
    return `wait_time${toolCallSuffix}(seconds=${String(action.seconds || "-")})`;
  }
  if (type === "spawn_agent") {
    return `spawn_agent${toolCallSuffix}(name=${String(action.name || "worker")})`;
  }
  if (type === "cancel_agent") {
    return `cancel_agent${toolCallSuffix}(agent_id=${String(action.agent_id || "-")})`;
  }
  if (type === "list_agent_runs") {
    return `list_agent_runs${toolCallSuffix}()`;
  }
  if (type === "get_agent_run") {
    return `get_agent_run${toolCallSuffix}(agent_id=${String(action.agent_id || "-")})`;
  }
  if (type === "list_tool_runs") {
    const cursor = String(action.cursor || "").trim();
    if (cursor) {
      return `list_tool_runs${toolCallSuffix}(status=${String(action.status || "-")}, cursor=...)`;
    }
    return `list_tool_runs${toolCallSuffix}(status=${String(action.status || "-")})`;
  }
  if (type === "get_tool_run") {
    return `get_tool_run${toolCallSuffix}(tool_run_id=${String(action.tool_run_id || "-")})`;
  }
  if (type === "wait_run") {
    const toolRunId = String(action.tool_run_id || "").trim();
    const agentId = String(action.agent_id || "").trim();
    if (toolRunId) {
      return `wait_run${toolCallSuffix}(tool_run_id=${toolRunId})`;
    }
    return `wait_run${toolCallSuffix}(agent_id=${agentId || "-"})`;
  }
  if (type === "cancel_tool_run") {
    return `cancel_tool_run${toolCallSuffix}(tool_run_id=${String(action.tool_run_id || "-")})`;
  }
  if (type === "finish") {
    return `finish${toolCallSuffix}(status=${String(action.status || "-")})`;
  }
  return `${type}${toolCallSuffix}`;
}

function toolCallIdSuffix(action) {
  const callId = String(action && action._tool_call_id ? action._tool_call_id : "").trim();
  if (!callId) {
    return "";
  }
  return ` (tool_call_id=${callId})`;
}

function toolCallResultPayload(payload) {
  const action = payload.action && typeof payload.action === "object" ? payload.action : {};
  const actionType = String(action.type || "");
  if (payload.result && typeof payload.result === "object") {
    return sanitizeToolResultForStream(actionType, payload.result);
  }
  if (typeof payload.result_preview === "string") {
    try {
      const parsed = JSON.parse(payload.result_preview);
      if (parsed && typeof parsed === "object") {
        return sanitizeToolResultForStream(actionType, parsed);
      }
    } catch (_error) {
      return null;
    }
  }
  return null;
}

function sanitizeToolResultForStream(actionType, result) {
  if (!result || typeof result !== "object") {
    return result;
  }
  if (String(actionType || "") !== "shell") {
    return result;
  }
  const sanitized = { ...result };
  delete sanitized.command;
  return sanitized;
}

function toolCallResultEntries(payload) {
  const action = payload.action && typeof payload.action === "object" ? payload.action : {};
  const actionType = String(action.type || "");
  const actionLabel = actionType || "tool";
  const actionKind = actionStreamKind(action, "return");
  const result = toolCallResultPayload(payload);
  const entries = [];
  if (actionType === "spawn_agent" && result && typeof result.child_agent_id === "string") {
    const childAgentId = String(result.child_agent_id || "").trim();
    const toolRunId = String(result.tool_run_id || "").trim();
    if (childAgentId) {
      entries.push({
        kind: "multiagent_return",
        text: toolRunId
          ? `spawn_agent result: child_agent_id=${childAgentId}, tool_run_id=${toolRunId}`
          : `spawn_agent result: child_agent_id=${childAgentId}`,
      });
    }
  } else if (actionType === "wait_run" && result && typeof result.wait_run_status === "boolean") {
    entries.push({
      kind: "tool_return",
      text: `wait_run result: status=${String(result.wait_run_status)}`,
    });
  } else if (actionType === "wait_time" && result && typeof result.wait_time_status === "boolean") {
    entries.push({
      kind: "tool_return",
      text: `wait_time result: status=${String(result.wait_time_status)}`,
    });
  } else if (actionType === "cancel_agent" && result && typeof result.cancel_agent_status === "boolean") {
    entries.push({
      kind: "multiagent_return",
      text: `cancel_agent result: status=${String(result.cancel_agent_status)}`,
    });
  } else if (actionType === "list_agent_runs" && result && Array.isArray(result.agent_runs)) {
    const nextCursor = String(result.next_cursor || "").trim();
    entries.push({
      kind: "multiagent_return",
      text: nextCursor
        ? `list_agent_runs result: runs_count=${result.agent_runs.length}, next_cursor=...`
        : `list_agent_runs result: runs_count=${result.agent_runs.length}`,
    });
  } else if (actionType === "list_tool_runs" && result && Array.isArray(result.tool_runs)) {
    const nextCursor = String(result.next_cursor || "").trim();
    entries.push({
      kind: "tool_return",
      text: nextCursor
        ? `list_tool_runs result: runs_count=${result.tool_runs.length}, next_cursor=...`
        : `list_tool_runs result: runs_count=${result.tool_runs.length}`,
    });
  } else if (actionType === "cancel_tool_run" && result && typeof result.final_status === "string") {
    entries.push({
      kind: "tool_return",
      text: `cancel_tool_run result: status=${String(result.final_status || "-")}`,
    });
  }
  if (result) {
    const warning = String(result.warning || "").trim();
    if (warning) {
      entries.push({ kind: "error", text: warning });
    }
    const errorText = String(result.error || "").trim();
    if (errorText) {
      entries.push({ kind: "error", text: errorText });
    }
  }
  if (entries.length > 0) {
    if (result) {
      entries.push({
        kind: actionKind,
        text: formatLabeledStructuredValue(`${actionLabel} result`, result, { fallback: "{}" }),
      });
    }
    return entries;
  }
  if (typeof payload.result_preview === "string" && payload.result_preview.trim()) {
    const preview = formatStructuredValue(payload.result_preview.trim());
    return [
      {
        kind: actionKind,
        text: formatLabeledStructuredValue(`${actionLabel} result`, preview),
      },
    ];
  }
  if (result) {
    return [
      {
        kind: actionKind,
        text: formatLabeledStructuredValue(`${actionLabel} result`, result, { fallback: "{}" }),
      },
    ];
  }
  return [];
}

function actionStreamKind(action, stage = "call") {
  const defaultKind = stage === "return" ? "tool_return" : "tool_call";
  if (!action || typeof action !== "object") {
    return defaultKind;
  }
  const type = String(action.type || "");
  if (type === "spawn_agent" || type === "cancel_agent" || type === "list_agent_runs") {
    return stage === "return" ? "multiagent_return" : "multiagent_call";
  }
  return defaultKind;
}

function actionEntries(actions) {
  if (!Array.isArray(actions)) {
    return [];
  }
  const entries = [];
  for (const action of actions) {
    if (!action || typeof action !== "object") {
      continue;
    }
    entries.push({
      kind: actionStreamKind(action, "call"),
      text: summarizeAction(action),
    });
  }
  return entries;
}

function toolCallTraceEntries(toolCalls) {
  if (!Array.isArray(toolCalls)) {
    return [];
  }
  const entries = [];
  for (const toolCall of toolCalls) {
    if (!toolCall || typeof toolCall !== "object") {
      continue;
    }
    const callId = String(toolCall.id || "").trim();
    const name = String(toolCall.name || "").trim();
    if (!callId && !name) {
      continue;
    }
    const lines = [];
    if (callId) {
      lines.push(`tool_call_id=${callId}`);
    }
    if (name) {
      lines.push(`name=${name}`);
    }
    const argumentValue =
      toolCall.arguments === undefined || toolCall.arguments === null ? {} : toolCall.arguments;
    lines.push(formatLabeledStructuredValue("arguments", argumentValue, { fallback: "{}" }));
    const text = lines.join("\n");
    entries.push({ kind: "tool_call", text });
  }
  return entries;
}

function controlMessageText(payload) {
  const kind = String(payload.kind || "").trim();
  const content = String(payload.content || "").trim();
  if (kind && content) {
    return `[${kind}] ${content}`;
  }
  if (content) {
    return content;
  }
  if (kind) {
    return `[${kind}]`;
  }
  return "";
}

function toolCallIdFromAction(action) {
  if (!action || typeof action !== "object") {
    return "";
  }
  return String(action._tool_call_id || "").trim();
}

function findToolRunFromCurrentPage(toolRunId) {
  const target = String(toolRunId || "").trim();
  if (!target) {
    return null;
  }
  const runs = Array.isArray(state.toolRuns.page.tool_runs) ? state.toolRuns.page.tool_runs : [];
  for (const run of runs) {
    if (!run || typeof run !== "object") {
      continue;
    }
    if (String(run.id || "").trim() === target) {
      return run;
    }
  }
  return null;
}

function snapshotToolRun(run) {
  if (!run || typeof run !== "object") {
    return null;
  }
  try {
    return JSON.parse(JSON.stringify(run));
  } catch (_error) {
    return { ...run };
  }
}

function updateToolRunDetailSnapshot(toolRunId) {
  const target = String(toolRunId || "").trim();
  if (!target) {
    return;
  }
  const current = findToolRunFromCurrentPage(target);
  if (!current) {
    return;
  }
  const nextSnapshot = snapshotToolRun(current);
  if (!nextSnapshot) {
    return;
  }
  const existingSnapshot = state.toolRuns.detail.runSnapshot;
  if (
    existingSnapshot &&
    typeof existingSnapshot === "object" &&
    String(existingSnapshot.id || "").trim() === target
  ) {
    if (
      (nextSnapshot.stdout === undefined || nextSnapshot.stdout === "") &&
      typeof existingSnapshot.stdout === "string"
    ) {
      nextSnapshot.stdout = existingSnapshot.stdout;
    }
    if (
      (nextSnapshot.stderr === undefined || nextSnapshot.stderr === "") &&
      typeof existingSnapshot.stderr === "string"
    ) {
      nextSnapshot.stderr = existingSnapshot.stderr;
    }
    if (nextSnapshot.result === undefined && existingSnapshot.result !== undefined) {
      nextSnapshot.result = existingSnapshot.result;
    }
  }
  state.toolRuns.detail.runSnapshot = nextSnapshot;
}

function openToolRunDetail(toolRunId) {
  const target = String(toolRunId || "").trim();
  if (!target) {
    return;
  }
  state.toolRuns.detail.open = true;
  state.toolRuns.detail.runId = target;
  state.toolRuns.detail.error = "";
  updateToolRunDetailSnapshot(target);
  void refreshToolRunDetail();
}

function closeToolRunDetail() {
  state.toolRuns.detail.open = false;
  state.toolRuns.detail.runId = null;
  state.toolRuns.detail.runSnapshot = null;
  state.toolRuns.detail.loading = false;
  state.toolRuns.detail.error = "";
}

async function refreshToolRunDetail() {
  const sessionId = activeSessionId();
  const runId = String(state.toolRuns.detail.runId || "").trim();
  if (!state.toolRuns.detail.open || !sessionId || !runId) {
    return;
  }
  if (state.toolRuns.detail.loading) {
    return;
  }
  state.toolRuns.detail.loading = true;
  try {
    const payload = await fetchJson(
      `/api/session/${encodeURIComponent(sessionId)}/tool-runs/${encodeURIComponent(runId)}`
    );
    const latestRun = payload && payload.tool_run && typeof payload.tool_run === "object" ? payload.tool_run : null;
    const activeRunId = String(state.toolRuns.detail.runId || "").trim();
    if (!state.toolRuns.detail.open || !latestRun || activeRunId !== runId) {
      return;
    }
    state.toolRuns.detail.runSnapshot = snapshotToolRun(latestRun);
    state.toolRuns.detail.error = "";
  } catch (error) {
    state.toolRuns.detail.error = String(error.message || "");
  } finally {
    state.toolRuns.detail.loading = false;
    scheduleRender();
  }
}

function registerToolRunTimelineEvent(record) {
  const eventType = String(record.event_type || "");
  if (
    eventType !== "tool_call_started" &&
    eventType !== "tool_call" &&
    eventType !== "tool_run_submitted" &&
    eventType !== "tool_run_updated"
  ) {
    return;
  }
  const payload = record.payload && typeof record.payload === "object" ? record.payload : {};
  const action = payload.action && typeof payload.action === "object" ? payload.action : {};
  const callId = toolCallIdFromAction(action);
  let toolRunId = String(payload.tool_run_id || "").trim();
  if (eventType === "tool_run_submitted" && toolRunId && callId) {
    state.toolRuns.callIdToRunId.set(callId, toolRunId);
  }
  if (!toolRunId && callId && state.toolRuns.callIdToRunId.has(callId)) {
    toolRunId = String(state.toolRuns.callIdToRunId.get(callId) || "").trim();
  }
  if (!toolRunId && (eventType === "tool_call_started" || eventType === "tool_call")) {
    toolRunId = String(action.tool_run_id || "").trim();
  }
  if (!toolRunId) {
    return;
  }
  const timeline = state.toolRuns.eventTimelineByRunId.get(toolRunId) || [];
  const entry = {
    timestamp: String(record.timestamp || ""),
    event_type: eventType,
    phase: String(record.phase || ""),
    agent_id: String(record.agent_id || ""),
    payload,
  };
  const last = timeline[timeline.length - 1];
  if (
    last &&
    last.timestamp === entry.timestamp &&
    last.event_type === entry.event_type &&
    last.phase === entry.phase
  ) {
    return;
  }
  timeline.push(entry);
  if (timeline.length > 300) {
    timeline.splice(0, timeline.length - 300);
  }
  state.toolRuns.eventTimelineByRunId.set(toolRunId, timeline);
  if (state.toolRuns.detail.open && state.toolRuns.detail.runId === toolRunId) {
    updateToolRunDetailSnapshot(toolRunId);
  }
}

function eventStatus(eventType, currentStatus, payload = {}) {
  if (eventType === "session_finalized") {
    const normalizedCurrentStatus = normalizeStatus(currentStatus);
    if (
      normalizedCurrentStatus === "cancelled" ||
      normalizedCurrentStatus === "terminated" ||
      normalizedCurrentStatus === "failed"
    ) {
      return normalizedCurrentStatus;
    }
    return "completed";
  }
  const mapping = {
    agent_spawned: "pending",
    agent_prompt: "running",
    llm_reasoning: "running",
    llm_token: "running",
    agent_response: "running",
    tool_run_submitted: "running",
    tool_run_updated: currentStatus,
    tool_call_started: "running",
    agent_paused: "paused",
    agent_cancelled: "cancelled",
    agent_terminated: "terminated",
    control_message: currentStatus,
    session_interrupted: "terminated",
    session_failed: "failed",
  };
  return mapping[eventType] || currentStatus;
}

function eventDetail(record, agent) {
  const eventType = String(record.event_type || "");
  const payload = record.payload && typeof record.payload === "object" ? record.payload : {};
  if (eventType === "agent_prompt") {
    return `${t("step_label")} ${payload.step_count || agent.stepCount || 1}`;
  }
  if (eventType === "tool_call_started" || eventType === "tool_call") {
    return summarizeAction(payload.action || {});
  }
  if (eventType === "tool_run_submitted") {
    return `tool_run submitted id=${String(payload.tool_run_id || "-")} tool=${String(payload.tool_name || "-")}`;
  }
  if (eventType === "tool_run_updated") {
    return `tool_run updated id=${String(payload.tool_run_id || "-")} status=${String(payload.status || "-")}`;
  }
  if (eventType === "steer_run_submitted") {
    return `steer_run submitted id=${String(payload.steer_run_id || "-")} from=${formatSteerSourceActor(payload)} status=${String(payload.status || "-")}`;
  }
  if (eventType === "steer_run_updated") {
    return `steer_run updated id=${String(payload.steer_run_id || "-")} from=${formatSteerSourceActor(payload)} status=${String(payload.status || "-")}`;
  }
  if (eventType === "agent_completed") {
    return String(payload.summary || "").slice(0, 120);
  }
  if (eventType === "session_finalized") {
    return String(payload.user_summary || "").slice(0, 120);
  }
  if (eventType === "session_failed") {
    return String(payload.error || "").slice(0, 120);
  }
  if (eventType === "control_message") {
    return controlMessageText(payload).slice(0, 120);
  }
  if (eventType === "context_compacted") {
    const stepRangeText = compactedRangeText(payload.step_range);
    return `${t("compressed_block_label")} ${stepRangeText}`.slice(0, 120);
  }
  return eventType;
}

function appendActivity(record) {
  if (STREAM_SKIP_ACTIVITY.has(String(record.event_type || ""))) {
    return;
  }
  const rendered = formatActivity(record);
  state.activityEntries.push(rendered);
  if (state.activityEntries.length > WORKFLOW_ACTIVITY_MAX_ENTRIES) {
    const overflow = state.activityEntries.length - WORKFLOW_ACTIVITY_MAX_ENTRIES;
    state.activityEntries.splice(0, overflow);
    markWorkflowActivityDirty({ full: true });
    return;
  }
  markWorkflowActivityDirty();
}

function formatActivity(record) {
  const timestampText = String(record.timestamp || "");
  const hhmmss = timestampText.length >= 19 ? timestampText.slice(11, 19) : "--:--:--";
  const payload = record.payload && typeof record.payload === "object" ? record.payload : {};
  const eventType = String(record.event_type || "");
  const actor = String(payload.agent_name || payload.root_agent_name || record.agent_id || "session");
  if (eventType === "session_started") {
    return `[${hhmmss}] 🚀 session started ${String(payload.task || "").slice(0, 120)}`;
  }
  if (eventType === "session_resumed") {
    return `[${hhmmss}] 🔁 session continued`;
  }
  if (eventType === "session_context_imported") {
    return `[${hhmmss}] 📂 session context loaded`;
  }
  if (eventType === "session_finalized") {
    return `[${hhmmss}] ✅ session finalized ${String(payload.user_summary || "").slice(0, 120)}`;
  }
  if (eventType === "project_sync_staged") {
    return `[${hhmmss}] 📦 sync staged (+${payload.added || 0}/~${payload.modified || 0}/-${payload.deleted || 0})`;
  }
  if (eventType === "project_sync_applied") {
    return `[${hhmmss}] 🟢 sync applied (+${payload.added || 0}/~${payload.modified || 0}/-${payload.deleted || 0})`;
  }
  if (eventType === "project_sync_reverted") {
    return `[${hhmmss}] ↩️ sync reverted (removed=${payload.removed || 0}, restored=${payload.restored || 0})`;
  }
  if (eventType === "session_interrupted") {
    return `[${hhmmss}] ⛔ session interrupted`;
  }
  if (eventType === "session_failed") {
    return `[${hhmmss}] ❌ session failed ${String(payload.error || "").slice(0, 120)}`;
  }
  if (eventType === "agent_spawned") {
    return `[${hhmmss}] 🧩 ${actor} spawned ${String(payload.instruction || "").slice(0, 120)}`;
  }
  if (eventType === "agent_prompt") {
    return `[${hhmmss}] 📝 ${actor} prompting step=${payload.step_count || 0}`;
  }
  if (eventType === "agent_paused") {
    return `[${hhmmss}] ⏸️ ${actor} paused`;
  }
  if (eventType === "agent_cancelled") {
    return `[${hhmmss}] 🛑 ${actor} cancelled`;
  }
  if (eventType === "agent_terminated") {
    return `[${hhmmss}] 🛑 ${actor} terminated`;
  }
  if (eventType === "tool_call_started") {
    return `[${hhmmss}] 🛠️ ${actor} ${summarizeAction(payload.action || {})}`;
  }
  if (eventType === "tool_call") {
    return `[${hhmmss}] 📥 ${actor} finished ${summarizeAction(payload.action || {})}`;
  }
  if (eventType === "tool_run_submitted") {
    return `[${hhmmss}] 🧾 ${actor} tool_run submitted id=${String(payload.tool_run_id || "-")} tool=${String(payload.tool_name || "-")}`;
  }
  if (eventType === "tool_run_updated") {
    return `[${hhmmss}] 🔄 ${actor} tool_run updated id=${String(payload.tool_run_id || "-")} status=${String(payload.status || "-")}`;
  }
  if (eventType === "steer_run_submitted") {
    return `[${hhmmss}] 🧭 ${actor} steer_run submitted id=${String(payload.steer_run_id || "-")} from=${formatSteerSourceActor(payload)} status=${String(payload.status || "-")}`;
  }
  if (eventType === "steer_run_updated") {
    return `[${hhmmss}] 🧭 ${actor} steer_run updated id=${String(payload.steer_run_id || "-")} from=${formatSteerSourceActor(payload)} status=${String(payload.status || "-")}`;
  }
  if (eventType === "agent_completed") {
    return `[${hhmmss}] 🏁 ${actor} completed ${String(payload.summary || "").slice(0, 120)}`;
  }
  if (eventType === "control_message") {
    return `[${hhmmss}] 🧭 ${actor} control ${controlMessageText(payload)}`;
  }
  if (eventType === "context_compacted") {
    return `[${hhmmss}] 🗜️ ${actor} ${t("compressed_block_label")} ${compactedRangeText(payload.step_range)}`;
  }
  return `[${hhmmss}] ${actor} ${eventType}`;
}

function consumeRuntimeEvent(record) {
  const payload = record.payload && typeof record.payload === "object" ? record.payload : {};
  registerToolRunTimelineEvent(record);
  appendActivity(record);

  if (record.session_id) {
    const sid = String(record.session_id);
    state.runtime.current_session_id = sid;
    state.runtime.configured_resume_session_id = sid;
    state.launchConfig.session_id = sid;
  }
  if (payload.task) {
    state.runtime.task = String(payload.task);
  }

  const eventType = String(record.event_type || "");
  if (eventType === "session_started") {
    state.runtime.session_status = String(payload.session_status || "running");
    state.runtime.status_message = t("started");
    state.runtime.running = true;
  } else if (eventType === "session_resumed") {
    state.runtime.session_status = "running";
    state.runtime.status_message = t("resume_started");
    state.runtime.running = true;
  } else if (eventType === "session_context_imported") {
    state.runtime.session_status = String(payload.session_status || state.runtime.session_status || "idle");
    state.runtime.status_message = t("configuration_saved");
    state.runtime.running = false;
  } else if (eventType === "session_finalized") {
    state.runtime.session_status = String(payload.session_status || "completed");
    state.runtime.summary = String(payload.user_summary || "");
    state.runtime.status_message = state.runtime.summary || t("session_completed");
    state.runtime.running = false;
    state.diff.dirty = true;
  } else if (eventType === "session_interrupted") {
    state.runtime.session_status = String(payload.session_status || "interrupted");
    state.runtime.status_message = t("session_interrupted");
    state.runtime.running = false;
  } else if (eventType === "session_failed") {
    state.runtime.session_status = String(payload.session_status || "failed");
    state.runtime.summary = String(payload.error || "");
    state.runtime.status_message = state.runtime.summary || t("session_failed");
    state.runtime.running = false;
  } else if (eventType === "project_sync_staged") {
    state.runtime.status_message = t("sync_state_pending");
    state.diff.dirty = true;
  } else if (eventType === "project_sync_applied") {
    state.runtime.status_message = t("sync_apply_done");
    state.diff.dirty = true;
  } else if (eventType === "project_sync_reverted") {
    state.runtime.status_message = t("sync_undo_done");
    state.diff.dirty = true;
  } else if (eventType === "tool_run_submitted" || eventType === "tool_run_updated") {
    state.toolRuns.dirty = true;
  } else if (eventType === "steer_run_submitted" || eventType === "steer_run_updated") {
    state.steerRuns.dirty = true;
  }

  const agent = ensureAgent(record, payload);
  if (!agent) {
    return;
  }
  const previousStatus = String(agent.status || "");
  const previousStepCount = Number(agent.stepCount || 0);
  const previousSummary = String(agent.summary || "");

  if (!AGENT_META_SKIP_EVENTS.has(eventType)) {
    agent.lastEvent = eventType;
    agent.lastPhase = String(record.phase || "runtime");
  }
  if (payload.step_count !== undefined) {
    const stepCount = Number(payload.step_count);
    if (Number.isFinite(stepCount) && stepCount >= 0) {
      agent.stepCount = Math.floor(stepCount);
    }
  }
  if (payload.agent_status) {
    agent.status = String(payload.agent_status);
  } else {
    agent.status = eventStatus(eventType, agent.status, payload);
  }
  if (payload.agent_model || payload.model) {
    agent.model = String(payload.agent_model || payload.model);
  }
  const contextTokens = coerceNonNegativeInt(payload.current_context_tokens);
  if (contextTokens !== null) {
    agent.currentContextTokens = contextTokens;
  }
  const contextLimit = coerceNonNegativeInt(payload.context_limit_tokens);
  if (contextLimit !== null) {
    agent.contextLimitTokens = contextLimit;
  }
  const usageRatio = Number(payload.usage_ratio);
  if (Number.isFinite(usageRatio) && usageRatio >= 0) {
    agent.usageRatio = usageRatio;
  }
  const compressionCount = coerceNonNegativeInt(payload.compression_count);
  if (compressionCount !== null) {
    agent.compressionCount = compressionCount;
  }
  applyContextSummaryMetrics(agent, payload);
  applyUsageMetrics(agent, payload);
  const compactedMessageRange = normalizeRange(payload.last_compacted_message_range);
  if (compactedMessageRange) {
    agent.lastCompactedMessageRange = compactedMessageRange;
  }
  const compactedStepRange = normalizeRange(payload.last_compacted_step_range);
  if (compactedStepRange) {
    agent.lastCompactedStepRange = compactedStepRange;
    recordCompactedStepRange(agent, compactedStepRange);
  }
  if (eventType === "context_compacted") {
    const eventStepRange = normalizeRange(payload.step_range);
    if (eventStepRange) {
      agent.lastCompactedStepRange = eventStepRange;
      recordCompactedStepRange(agent, eventStepRange);
    }
    const eventMessageRange = normalizeRange(payload.message_range);
    if (eventMessageRange) {
      agent.lastCompactedMessageRange = eventMessageRange;
    }
    const afterTokens = coerceNonNegativeInt(payload.context_tokens_after);
    if (afterTokens !== null) {
      agent.currentContextTokens = afterTokens;
    }
    const limitTokens = coerceNonNegativeInt(payload.context_limit_tokens);
    if (limitTokens !== null) {
      agent.contextLimitTokens = limitTokens;
    }
    if (agent.contextLimitTokens > 0) {
      agent.usageRatio = Number(
        (Math.max(0, agent.currentContextTokens) / Math.max(1, agent.contextLimitTokens)).toFixed(4)
      );
    }
  }
  if (!AGENT_META_SKIP_EVENTS.has(eventType)) {
    agent.lastDetail = eventDetail(record, agent);
  }

  const stepNumber = stepNumberFor(agent, payload);
  if (eventType === "agent_prompt") {
    agent.isGenerating = false;
    clearPreviewEntries(agent, stepNumber);
    ensureStepEntries(agent, stepNumber);
  } else if (eventType === "llm_reasoning") {
    agent.isGenerating = true;
    appendStepEntry(agent, stepNumber, "thinking_preview", String(payload.token || ""), { merge: true });
  } else if (eventType === "llm_token") {
    agent.isGenerating = true;
    appendStepEntry(agent, stepNumber, "reply_preview", String(payload.token || ""), { merge: true });
  } else if (eventType === "agent_response") {
    agent.isGenerating = false;
  } else if (eventType === "tool_call_started") {
    const action = payload.action || {};
    const kind = extraKind(actionStreamKind(action, "call"));
    const text = summarizeAction(action);
    if (!stepHasEntry(agent, stepNumber, kind, text)) {
      appendStepEntry(agent, stepNumber, kind, text);
    }
  } else if (eventType === "tool_call") {
    const resultEntries = toolCallResultEntries(payload);
    if (resultEntries.length > 0) {
      for (const entry of resultEntries) {
        const kind = extraKind(String(entry.kind || ""));
        const text = String(entry.text || "");
        if (!stepHasEntry(agent, stepNumber, kind, text)) {
          appendStepEntry(agent, stepNumber, kind, text);
        }
      }
    } else {
      const kind = extraKind(actionStreamKind(payload.action || {}, "return"));
      const text = summarizeAction(payload.action || {});
      if (!stepHasEntry(agent, stepNumber, kind, text)) {
        appendStepEntry(agent, stepNumber, kind, text);
      }
    }
  } else if (eventType === "tool_run_submitted") {
    const toolRunId = String(payload.tool_run_id || "").trim();
    const toolName = String(payload.tool_name || "-");
    if (toolRunId) {
      appendStepEntry(
        agent,
        stepNumber,
        extraKind("tool_call"),
        `tool_run submitted: id=${toolRunId}, tool=${toolName}`
      );
    }
  } else if (eventType === "tool_run_updated") {
    const toolRunId = String(payload.tool_run_id || "").trim();
    const status = String(payload.status || "-");
    if (toolRunId) {
      appendStepEntry(
        agent,
        stepNumber,
        extraKind("tool_return"),
        `tool_run updated: id=${toolRunId}, status=${status}`
      );
    }
  } else if (eventType === "steer_run_submitted") {
    const steerRunId = String(payload.steer_run_id || "").trim();
    const status = String(payload.status || "-");
    const fromActor = formatSteerSourceActor(payload);
    if (steerRunId) {
      appendStepEntry(
        agent,
        stepNumber,
        extraKind("control"),
        `steer_run submitted: id=${steerRunId}, from=${fromActor}, status=${status}`
      );
    }
  } else if (eventType === "steer_run_updated") {
    const steerRunId = String(payload.steer_run_id || "").trim();
    const status = String(payload.status || "-");
    const fromActor = formatSteerSourceActor(payload);
    if (steerRunId) {
      appendStepEntry(
        agent,
        stepNumber,
        extraKind("control"),
        `steer_run updated: id=${steerRunId}, from=${fromActor}, status=${status}`
      );
    }
  } else if (eventType === "shell_stream") {
    const kind = String(record.phase || "") === "stderr" ? extraKind("stderr") : extraKind("stdout");
    appendStepEntry(agent, stepNumber, kind, String(payload.text || ""), { merge: true });
  } else if (eventType === "child_summaries_received") {
    const children = Array.isArray(payload.children) ? payload.children : [];
    for (const child of children) {
      if (!child || typeof child !== "object") {
        continue;
      }
      const childId = String(child.id || "");
      const childDisplayId = childId;
      const childName = String(child.name || childDisplayId || "child");
      const status = String(child.status || "");
      const summary = String(child.summary || t("none_value"));
      appendStepEntry(
        agent,
        stepNumber,
        extraKind("multiagent_return"),
        `${childName} (${childDisplayId}) [${status}]: ${summary}`
      );
      registerChildSummaryTransfer(childId, agent.id, String(record.timestamp || ""));
    }
  } else if (eventType === "agent_completed") {
    agent.summary = String(payload.summary || "");
    if (agent.summary) {
      appendStepEntry(agent, stepNumber, extraKind("summary"), agent.summary);
    }
  } else if (eventType === "protocol_error" || eventType === "sandbox_violation") {
    appendStepEntry(agent, stepNumber, extraKind("error"), String(payload.error || ""));
  } else if (eventType === "control_message") {
    const controlKind = String(payload.kind || "").trim();
    if (controlKind === "context_pressure_reminder") {
      agent.contextWarningCount = Math.max(0, Number(agent.contextWarningCount || 0)) + 1;
      const warningRatio = Number(payload.usage_ratio);
      if (Number.isFinite(warningRatio) && warningRatio >= 0) {
        agent.lastContextWarningRatio = warningRatio;
      }
      const warningTokens = coerceNonNegativeInt(payload.current_context_tokens);
      if (warningTokens !== null) {
        agent.lastContextWarningTokens = warningTokens;
      }
      const warningLimit = coerceNonNegativeInt(payload.context_limit_tokens);
      if (warningLimit !== null) {
        agent.lastContextWarningLimit = warningLimit;
      }
    }
    const text = controlMessageText(payload);
    if (text) {
      // Make context-pressure reminders visible in step stream; keep other control messages internal-only.
      const entryKind = controlKind === "context_pressure_reminder" ? "control" : extraKind("control");
      appendStepEntry(agent, stepNumber, entryKind, text);
    }
  }
  if (MESSAGE_SYNC_EVENT_TYPES.has(eventType)) {
    scheduleMessageSync();
  }
  if (
    String(agent.status || "") !== previousStatus ||
    Number(agent.stepCount || 0) !== previousStepCount ||
    String(agent.summary || "") !== previousSummary
  ) {
    markWorkflowGraphDirty();
  }
  markAgentPanelDirty();
}

async function loadSessionEvents(sessionId) {
  if (!sessionId) {
    return;
  }
  const payload = await fetchJson(`/api/session/${encodeURIComponent(sessionId)}/events`);
  if (!payload || !Array.isArray(payload.events)) {
    return;
  }
  const snapshotAgents = payload && Array.isArray(payload.agents) ? payload.agents : [];
  resetRuntimeViews();
  for (const record of payload.events) {
    consumeRuntimeEvent(record);
  }
  await loadSessionMessages(sessionId);
  if (snapshotAgents.length > 0) {
    applyAgentSnapshot(snapshotAgents);
  }
  scheduleRender();
}

async function loadSessionMessages(sessionId) {
  if (!sessionId) {
    return;
  }
  state.messages.cursor = null;
  resetAgentMessageEntries({ preserveNonMessage: false });
  let cursor = null;
  for (let pageIndex = 0; pageIndex < 200; pageIndex += 1) {
    const query = new URLSearchParams({ limit: "500" });
    if (cursor) {
      query.set("cursor", cursor);
    }
    const payload = await fetchJson(
      `/api/session/${encodeURIComponent(sessionId)}/messages?${query.toString()}`
    );
    const records = payload && Array.isArray(payload.messages) ? payload.messages : [];
    if (records.length > 0) {
      applyMessageRecords(records);
    }
    const nextCursor = payload && typeof payload.next_cursor === "string" ? payload.next_cursor : null;
    const hasMore = Boolean(payload && payload.has_more);
    if (!hasMore) {
      cursor = nextCursor || cursor;
      break;
    }
    if (!nextCursor || nextCursor === cursor) {
      break;
    }
    cursor = nextCursor;
  }
  state.messages.cursor = cursor;
  markAgentPanelDirty();
}

async function syncSessionMessagesIncremental() {
  const sessionId = activeSessionId();
  if (!sessionId || state.messages.syncing) {
    return;
  }
  state.messages.syncing = true;
  try {
    let cursor = state.messages.cursor;
    for (let pageIndex = 0; pageIndex < 100; pageIndex += 1) {
      const query = new URLSearchParams({ limit: "500" });
      if (cursor) {
        query.set("cursor", cursor);
      }
      const payload = await fetchJson(
        `/api/session/${encodeURIComponent(sessionId)}/messages?${query.toString()}`
      );
      const records = payload && Array.isArray(payload.messages) ? payload.messages : [];
      if (records.length > 0) {
        applyMessageRecords(records);
      }
      const nextCursor = payload && typeof payload.next_cursor === "string" ? payload.next_cursor : null;
      const hasMore = Boolean(payload && payload.has_more);
      if (!hasMore) {
        cursor = nextCursor || cursor;
        break;
      }
      if (!nextCursor || nextCursor === cursor) {
        break;
      }
      cursor = nextCursor;
    }
    state.messages.cursor = cursor;
  } catch (_error) {
    // Ignore transient sync errors; next event-triggered sync will retry.
  } finally {
    state.messages.syncing = false;
    markAgentPanelDirty();
    scheduleRender();
  }
}

function scheduleMessageSync(delayMs = 90) {
  if (state.messages.timer !== null) {
    return;
  }
  state.messages.timer = window.setTimeout(() => {
    state.messages.timer = null;
    void syncSessionMessagesIncremental();
  }, Math.max(0, Math.floor(delayMs)));
}

function render() {
  renderHeader();
  renderControls();
  renderTabs();
  if (state.activeTab === "overview") {
    renderOverviewTab();
  }
  if (state.activeTab === "workflow") {
    renderWorkflowTab();
  }
  renderAgentPanel();
  renderToolRunsPanel();
  renderSteerRunsPanel();
  renderAgentFocusOverlay();
  renderSteerComposeOverlay();
  renderToolRunDetailOverlay();
  renderSetupOverlay();
  renderDiffPanel();
  renderConfigPanel();
}

function renderHeader() {
  dom.appTitle.textContent = t("app_title");
  dom.localeEnButton.textContent = t("locale_en");
  dom.localeZhButton.textContent = t("locale_zh");
  dom.localeEnButton.style.borderColor = state.locale === "en" ? "var(--accent)" : "var(--border)";
  dom.localeZhButton.style.borderColor = state.locale === "zh" ? "var(--accent)" : "var(--border)";
}

function autoSizeTaskInput() {
  const computed = window.getComputedStyle(dom.taskInput);
  const lineHeight = Number.parseFloat(computed.lineHeight);
  const lineHeightPx = Number.isFinite(lineHeight) && lineHeight > 0 ? lineHeight : 20;
  const paddingTop = Number.parseFloat(computed.paddingTop) || 0;
  const paddingBottom = Number.parseFloat(computed.paddingBottom) || 0;
  const borderTop = Number.parseFloat(computed.borderTopWidth) || 0;
  const borderBottom = Number.parseFloat(computed.borderBottomWidth) || 0;
  const minHeight = lineHeightPx * TASK_INPUT_MIN_ROWS + paddingTop + paddingBottom + borderTop + borderBottom;
  const maxHeight = lineHeightPx * TASK_INPUT_MAX_ROWS + paddingTop + paddingBottom + borderTop + borderBottom;

  dom.taskInput.style.height = `${minHeight}px`;
  const naturalHeight = Math.max(dom.taskInput.scrollHeight, minHeight);
  const nextHeight = Math.min(naturalHeight, maxHeight);
  dom.taskInput.style.height = `${nextHeight}px`;
  dom.taskInput.style.overflowY = naturalHeight > maxHeight ? "auto" : "hidden";
}

function defaultTaskInputValue() {
  const localized = t("task_input_default_value");
  return typeof localized === "string" ? localized : "";
}

function syncTaskInputValue() {
  const runtimeTask = String(state.runtime.task || "");
  const currentValue = String(dom.taskInput.value || "");
  const seededValue = state.taskInputSeededValue;
  if (runtimeTask.trim()) {
    if (!currentValue.trim() || (seededValue !== null && currentValue === seededValue)) {
      dom.taskInput.value = runtimeTask;
    }
    state.taskInputSeededValue = null;
    return;
  }
  const localizedDefault = defaultTaskInputValue();
  if (!localizedDefault) {
    return;
  }
  if (!currentValue.trim()) {
    dom.taskInput.value = localizedDefault;
    state.taskInputSeededValue = localizedDefault;
    return;
  }
  if (seededValue !== null && currentValue === seededValue && currentValue !== localizedDefault) {
    dom.taskInput.value = localizedDefault;
    state.taskInputSeededValue = localizedDefault;
  }
}

function renderControls() {
  dom.taskInput.placeholder = t("task_input");
  syncTaskInputValue();
  dom.modelLabel.textContent = t("model_input_label");
  dom.modelInput.placeholder = t("model_input_placeholder");
  if (state.runtime.model && !dom.modelInput.value.trim()) {
    dom.modelInput.value = state.runtime.model;
  }
  dom.rootAgentNameLabel.textContent = t("root_agent_name_label");
  dom.rootAgentNameInput.placeholder = t("root_agent_name_placeholder");
  if (state.runtime.root_agent_name && !dom.rootAgentNameInput.value.trim()) {
    dom.rootAgentNameInput.value = state.runtime.root_agent_name;
  }
  autoSizeTaskInput();
  const runSubmitting = Boolean(state.runtime.runSubmitting);
  dom.runButton.textContent = runSubmitting ? t("run_submitting") : t("run");
  dom.terminalButton.textContent = t("terminal");
  dom.setupButton.textContent = t("reconfigure");
  dom.interruptButton.textContent = t("interrupt");
  dom.applyButton.textContent = t("apply");
  dom.undoButton.textContent = t("undo");
  dom.configSaveButton.textContent = t("save");
  dom.configReloadButton.textContent = t("reload");

  const running = Boolean(state.runtime.running);
  const syncBusy = Boolean(state.runtime.project_sync_action_in_progress);
  const setupBusy = Boolean(state.setup.busy);
  const directMode = isDirectWorkspaceMode();
  dom.runButton.disabled = runSubmitting || syncBusy || setupBusy || !state.launchConfig.can_run;
  dom.modelInput.disabled = syncBusy || setupBusy;
  dom.rootAgentNameInput.disabled = syncBusy || setupBusy;
  dom.terminalButton.disabled = syncBusy || setupBusy || !activeSessionId();
  dom.setupButton.disabled = running || syncBusy || setupBusy;
  dom.interruptButton.disabled = !running;
  dom.applyButton.disabled = running || syncBusy || setupBusy || directMode || !activeSessionId();
  dom.undoButton.disabled = running || syncBusy || setupBusy || directMode || !activeSessionId();

  const stats = agentStats();
  const statusText = localizeStatus(state.runtime.session_status || "idle");
  const sessionIdText = activeSessionId() || t("pending_value");
  const remoteText = isRemoteWorkspaceConfigured() ? remoteWorkspaceLabel() : t("unset_value");
  dom.controlSummary.textContent = `${t("session_status")}: ${statusText} | ${t("session_id")}: ${sessionIdText} | ${t("workspace_mode_label")}: ${localizeWorkspaceMode(activeWorkspaceMode())} | ${t("sandbox_backend_label")}: ${localizeSandboxBackend(activeSandboxBackend())} | ${t("remote_workspace_label")}: ${remoteText} | ${t("agents")}: ${stats.total} | ${t("status_running")}: ${stats.running} | ${t("status_paused")}: ${stats.paused} | ${t("status_completed")}: ${stats.completed} | ${t("status_failed")}: ${stats.failed} | ${t("status_cancelled")}: ${stats.cancelled} | ${t("status_terminated")}: ${stats.terminated}`;
}

function renderTabs() {
  const directMode = isDirectWorkspaceMode();
  if (directMode && state.activeTab === "diff") {
    state.activeTab = "overview";
  }
  dom.tabButtons.forEach((button) => {
    const tabName = button.dataset.tab;
    const disabled = tabName === "diff" && directMode;
    button.disabled = disabled;
    button.classList.toggle("active", state.activeTab === tabName);
    if (tabName === "overview") {
      button.textContent = t("overview_tab_title");
    } else if (tabName === "workflow") {
      button.textContent = t("workflow_tab_title");
    } else if (tabName === "agents") {
      button.textContent = t("agents_tab_title");
    } else if (tabName === "tool-runs") {
      button.textContent = t("tool_runs_tab_title");
    } else if (tabName === "steer-runs") {
      button.textContent = t("steer_runs_tab_title");
    } else if (tabName === "diff") {
      button.textContent = t("diff_tab_title");
      button.title = disabled ? t("diff_disabled_direct") : "";
    } else if (tabName === "config") {
      button.textContent = t("config_tab_title");
    }
  });

  dom.tabOverview.classList.toggle("active", state.activeTab === "overview");
  dom.tabWorkflow.classList.toggle("active", state.activeTab === "workflow");
  dom.tabAgents.classList.toggle("active", state.activeTab === "agents");
  dom.tabToolRuns.classList.toggle("active", state.activeTab === "tool-runs");
  dom.tabSteerRuns.classList.toggle("active", state.activeTab === "steer-runs");
  dom.tabDiff.classList.toggle("active", state.activeTab === "diff");
  dom.tabConfig.classList.toggle("active", state.activeTab === "config");

  dom.overviewLaunchTitle.textContent = t("overview_launch_title");
  dom.overviewFeedTitle.textContent = t("overview_feed_title");
  dom.workflowTitle.textContent = t("workflow_title");
  dom.activityTitle.textContent = t("activity");
  dom.agentsTitle.textContent = t("agents_tab_title");
  if (dom.agentsRoleFilterLabel) {
    dom.agentsRoleFilterLabel.textContent = t("agents_role_filter_label");
  }
  if (dom.agentsRoleFilter) {
    const roleOptionLabels = {
      all: t("agents_role_filter_all"),
      root: t("agents_role_filter_root"),
      worker: t("agents_role_filter_worker"),
    };
    for (const option of dom.agentsRoleFilter.options) {
      const key = String(option.value || "all");
      if (roleOptionLabels[key]) {
        option.textContent = roleOptionLabels[key];
      }
    }
    dom.agentsRoleFilter.value = state.agentsView.roleFilter;
  }
  if (dom.agentsSearchInput) {
    dom.agentsSearchInput.placeholder = t("agents_search_placeholder");
  }
  if (dom.steerRunsSearchInput) {
    dom.steerRunsSearchInput.placeholder = t("steer_runs_search_placeholder");
    if (dom.steerRunsSearchInput.value !== state.steerRuns.searchQuery) {
      dom.steerRunsSearchInput.value = state.steerRuns.searchQuery;
    }
  }

  const workflowExpanded = Boolean(state.workflow.expanded && state.activeTab === "workflow");
  if (dom.workflowPanel) {
    dom.workflowPanel.classList.toggle("expanded", workflowExpanded);
  }
  document.body.classList.toggle("workflow-immersive", workflowExpanded);
}

function renderOverviewTab() {
  const runtimeMessage = state.runtime.summary || state.runtime.status_message || "-";
  const focusAgent = state.agentOrder
    .map((id) => state.agents.get(id))
    .filter((agent) => agent !== undefined)
    .at(-1);
  const recentActivity = state.activityEntries.slice(-16).reverse();
  const stats = agentStats();
  const statusClass = `status-${normalizeStatus(state.runtime.session_status || "idle")}`;
  const currentSessionId = activeSessionId() || t("pending_value");
  const focusText = focusAgent
    ? `${focusAgent.name} (${focusAgent.id})`
    : t("none_value");

  dom.overviewLaunch.innerHTML = `<div class="compact-info">${renderInfoRows([
    [
      t("configuration_title"),
      state.launchConfig.can_run ? t("configuration_ready") : t("configuration_missing"),
    ],
    [t("workspace_mode_label"), localizeWorkspaceMode(activeWorkspaceMode())],
    [t("sandbox_backend_label"), localizeSandboxBackend(activeSandboxBackend())],
    [t("remote_workspace_label"), remoteWorkspaceLabel()],
    [t("project_dir"), state.launchConfig.project_dir || t("unset_value")],
    [t("session_id"), state.launchConfig.session_id || t("unset_value")],
    [t("sessions_root"), state.sessionsDir || t("unset_value")],
  ])}</div>`;

  const insightMessageBody = state.runtime.summary
    ? renderMarkdown(state.runtime.summary)
    : `<div class="plain-inline">${escapeHtml(runtimeMessage)}</div>`;
  const insightMessageTitle = state.runtime.summary ? t("summary") : t("message");
  const activityHtml =
    recentActivity.length > 0
      ? recentActivity
          .map(
            (entry) =>
              `<div class="insight-activity-row"><span class="insight-activity-text">${escapeHtml(
                entry
              )}</span></div>`
          )
          .join("")
      : `<div class="entry-sub">${escapeHtml(t("none_value"))}</div>`;

  dom.overviewFeed.innerHTML = `
    <div class="overview-insight-grid">
      <div class="overview-insight-kpis">
        <div class="overview-insight-kpi">
          <div class="overview-insight-kpi-label">${escapeHtml(t("session_status"))}</div>
          <div class="overview-insight-kpi-value">
            <span class="badge ${statusClass}">${escapeHtml(
              localizeStatus(state.runtime.session_status || "idle")
            )}</span>
          </div>
        </div>
        <div class="overview-insight-kpi">
          <div class="overview-insight-kpi-label">${escapeHtml(t("session_id"))}</div>
          <div class="overview-insight-kpi-value">${escapeHtml(currentSessionId)}</div>
        </div>
        <div class="overview-insight-kpi">
          <div class="overview-insight-kpi-label">${escapeHtml(t("task"))}</div>
          <div class="overview-insight-kpi-value">${escapeHtml(state.runtime.task || t("none_value"))}</div>
        </div>
        <div class="overview-insight-kpi">
          <div class="overview-insight-kpi-label">${escapeHtml(t("current_focus"))}</div>
          <div class="overview-insight-kpi-value">${escapeHtml(focusText)}</div>
        </div>
        <div class="overview-insight-kpi">
          <div class="overview-insight-kpi-label">${escapeHtml(t("agents"))}</div>
          <div class="overview-insight-kpi-value">${escapeHtml(String(stats.total))}</div>
        </div>
        <div class="overview-insight-kpi">
          <div class="overview-insight-kpi-label">${escapeHtml(t("status_running"))}</div>
          <div class="overview-insight-kpi-value">${escapeHtml(String(stats.running))}</div>
        </div>
        <div class="overview-insight-kpi">
          <div class="overview-insight-kpi-label">${escapeHtml(t("status_paused"))}</div>
          <div class="overview-insight-kpi-value">${escapeHtml(String(stats.paused))}</div>
        </div>
        <div class="overview-insight-kpi">
          <div class="overview-insight-kpi-label">${escapeHtml(t("status_completed"))}</div>
          <div class="overview-insight-kpi-value">${escapeHtml(String(stats.completed))}</div>
        </div>
        <div class="overview-insight-kpi">
          <div class="overview-insight-kpi-label">${escapeHtml(t("status_failed"))}</div>
          <div class="overview-insight-kpi-value">${escapeHtml(String(stats.failed))}</div>
        </div>
        <div class="overview-insight-kpi">
          <div class="overview-insight-kpi-label">${escapeHtml(t("status_cancelled"))}</div>
          <div class="overview-insight-kpi-value">${escapeHtml(String(stats.cancelled))}</div>
        </div>
        <div class="overview-insight-kpi">
          <div class="overview-insight-kpi-label">${escapeHtml(t("status_terminated"))}</div>
          <div class="overview-insight-kpi-value">${escapeHtml(String(stats.terminated))}</div>
        </div>
      </div>
      <div class="overview-insight-block">
        <div class="overview-insight-head">${escapeHtml(insightMessageTitle)}</div>
        <div class="overview-insight-message">${insightMessageBody}</div>
      </div>
      <div class="overview-insight-block">
        <div class="overview-insight-head">${escapeHtml(t("activity"))}</div>
        <div class="overview-insight-activity">${activityHtml}</div>
      </div>
    </div>
  `;
}

function renderWorkflowTab() {
  const workflowExpanded = Boolean(state.workflow.expanded);
  if (dom.workflowOriginButton) {
    dom.workflowOriginButton.textContent = t("workflow_origin_button");
  }
  if (dom.workflowExpandButton) {
    dom.workflowExpandButton.textContent = workflowExpanded ? t("collapse_view") : t("expand_view");
    dom.workflowExpandButton.setAttribute("aria-pressed", workflowExpanded ? "true" : "false");
  }
  if (state.workflow.graphDirty) {
    renderWorkflowGraph();
    state.workflow.graphDirty = false;
  }
  if (dom.workflowZoomResetButton) {
    dom.workflowZoomResetButton.textContent = `${Math.round(state.workflow.scale * 100)}%`;
  }
  renderWorkflowActivityLog();
}

function renderWorkflowActivityLog() {
  if (!state.workflow.activityDirty) {
    return;
  }
  const items = state.activityEntries;
  const atBottom =
    dom.activityLog.scrollTop + dom.activityLog.clientHeight >= dom.activityLog.scrollHeight - 8;
  const renderedCount = Math.max(0, Number(state.workflow.activityRenderedCount || 0));
  const needsFullRender =
    state.workflow.activityNeedsFullRender || renderedCount > items.length;

  if (needsFullRender) {
    dom.activityLog.innerHTML = items
      .map((entry) => `<div class="activity-entry">${escapeHtml(entry)}</div>`)
      .join("");
    state.workflow.activityNeedsFullRender = false;
    state.workflow.activityRenderedCount = items.length;
  } else if (renderedCount < items.length) {
    const fragment = document.createDocumentFragment();
    for (let index = renderedCount; index < items.length; index += 1) {
      const row = document.createElement("div");
      row.className = "activity-entry";
      row.textContent = String(items[index] || "");
      fragment.appendChild(row);
    }
    dom.activityLog.appendChild(fragment);
    state.workflow.activityRenderedCount = items.length;
  }
  state.workflow.activityDirty = false;
  if (atBottom) {
    dom.activityLog.scrollTop = dom.activityLog.scrollHeight;
  }
}

function renderWorkflowGraph() {
  const model = buildGraphModel();
  if (!model || model.nodes.length === 0) {
    state.workflow.nodeDetails.clear();
    dom.workflowGraph.innerHTML = `<div class="entry-sub graph-placeholder">${escapeHtml(
      t("flow_idle")
    )}</div>`;
    hideWorkflowNodeTooltip();
    if (dom.workflowZoomResetButton) {
      dom.workflowZoomResetButton.textContent = "100%";
    }
    return;
  }
  state.workflow.nodeDetails.clear();
  for (const node of model.nodes) {
    state.workflow.nodeDetails.set(node.id, node);
  }

  const nodeWidth = model.nodeWidth;
  const nodeHeight = model.nodeHeight;
  const depthGuideHtml = model.depthGuides
    .map((x) => {
      const guideX = Math.round(x);
      return `<line class="graph-depth-guide" x1="${guideX}" y1="12" x2="${guideX}" y2="${Math.round(
        model.height - 14
      )}" />`;
    })
    .join("");
  const edgeHtml = model.edges
    .map((edge) => {
      const from = model.positions.get(edge.from);
      const to = model.positions.get(edge.to);
      if (!from || !to) {
        return "";
      }
      const startX = from.x + nodeWidth;
      const startY = from.y + nodeHeight / 2;
      const endX = to.x - 10;
      const endY = to.y + nodeHeight / 2;
      const distanceX = Math.max(68, endX - startX);
      const curve = Math.max(70, Math.min(148, distanceX * 0.42));
      const c1x = startX + curve;
      const c2x = endX - curve;
      return `<path class="graph-edge" d="M ${startX} ${startY} C ${c1x} ${startY}, ${c2x} ${endY}, ${endX} ${endY}" marker-end="url(#edge-arrow)" />`;
    })
    .join("");

  const summaryEdgeHtml = model.summaryTransfers
    .map((edge, index) => {
      const childPosition = model.positions.get(edge.from);
      const parentPosition = model.positions.get(edge.to);
      if (!childPosition || !parentPosition) {
        return "";
      }
      const laneOffset = ((index % 4) - 1.5) * 9;
      const startX = childPosition.x;
      const startY = childPosition.y + nodeHeight * 0.72 + laneOffset;
      const endX = parentPosition.x + nodeWidth + 10;
      const endY = parentPosition.y + nodeHeight * 0.72 + laneOffset;
      const c1x = startX - 62;
      const c2x = endX + 62;
      const midX = (startX + endX) / 2;
      const midY = (startY + endY) / 2 - 5;
      const label = edge.count > 1 ? `summary x${edge.count}` : "summary";
      return `
        <path class="graph-edge-return" d="M ${startX} ${startY} C ${c1x} ${startY}, ${c2x} ${endY}, ${endX} ${endY}" marker-end="url(#edge-return-arrow)" />
        <text class="graph-edge-return-label" x="${midX}" y="${midY}">${escapeHtml(label)}</text>
      `;
    })
    .join("");

  const nodeHtml = model.nodes
    .map((node, nodeIndex) => {
      const position = model.positions.get(node.id);
      if (!position) {
        return "";
      }
      const statusClass = `status-${normalizeStatus(node.status)}`;
      const clipId = `graph-node-clip-${nodeIndex}`;
      const title = shortDisplayText(node.name || node.id, 24);
      const statusEmoji = statusToEmoji(node.status);
      const nodeIdText = shortDisplayText(node.id, 30);
      const taskText = shortDisplayText(node.instruction || t("none_value"), 30);
      const summaryText = node.summary ? shortDisplayText(node.summary, 30) : t("none_value");
      return `
        <g class="graph-node ${statusClass}" data-node-id="${escapeHtml(node.id)}" transform="translate(${position.x}, ${position.y})">
          <defs>
            <clipPath id="${clipId}">
              <rect x="10" y="8" width="${nodeWidth - 20}" height="${nodeHeight - 16}" rx="8" ry="8" />
            </clipPath>
          </defs>
          <rect class="graph-node-card" width="${nodeWidth}" height="${nodeHeight}" />
          <rect class="graph-node-head" x="2" y="2" width="${nodeWidth - 4}" height="34" />
          <circle class="graph-node-dot" cx="15" cy="19" r="4"></circle>
          <g clip-path="url(#${clipId})">
            <text class="graph-node-label" x="24" y="23">${escapeHtml(`${statusEmoji} ${title}`)}</text>
            <text class="graph-node-id" x="12" y="52">${escapeHtml(`id · ${nodeIdText}`)}</text>
            <text class="graph-node-stat" x="12" y="72">${escapeHtml(
              `${localizeStatus(node.status)} | ${t("step_label")} ${node.stepCount || 0}`
            )}</text>
            <text class="graph-node-stat" x="12" y="90">${escapeHtml(
              `role: ${node.role || "worker"} | children: ${node.childCount || 0}`
            )}</text>
            <text class="graph-node-sub" x="12" y="112">${escapeHtml(`task: ${taskText}`)}</text>
            <text class="graph-node-sub" x="12" y="132">${escapeHtml(`summary: ${summaryText}`)}</text>
          </g>
        </g>
      `;
    })
    .join("");

  dom.workflowGraph.innerHTML = `
    <svg class="workflow-svg" viewBox="0 0 ${model.width} ${model.height}" width="${model.width}" height="${model.height}" role="img" aria-label="workflow graph">
      <defs>
        <filter id="graph-node-shadow" x="-20%" y="-20%" width="150%" height="150%">
          <feDropShadow dx="0" dy="7" stdDeviation="5" flood-color="rgba(2, 8, 14, 0.6)" />
        </filter>
        <marker id="edge-arrow" markerWidth="12" markerHeight="12" refX="10" refY="6" orient="auto" markerUnits="userSpaceOnUse">
          <path d="M1,1 L10,6 L1,11 z" fill="#9ec3e7"></path>
        </marker>
        <marker id="edge-return-arrow" markerWidth="12" markerHeight="12" refX="10" refY="6" orient="auto" markerUnits="userSpaceOnUse">
          <path d="M1,1 L10,6 L1,11 z" fill="#66ddb4"></path>
        </marker>
      </defs>
      <rect class="graph-canvas-backdrop" x="0" y="0" width="${model.width}" height="${model.height}" />
      ${depthGuideHtml}
      ${edgeHtml}
      ${summaryEdgeHtml}
      ${nodeHtml}
    </svg>
    <div class="graph-node-tooltip is-hidden" aria-hidden="true"></div>
  `;
  applyWorkflowTransform();
}

function workflowTooltipElement() {
  return dom.workflowGraph.querySelector(".graph-node-tooltip");
}

function hideWorkflowNodeTooltip() {
  const tooltip = workflowTooltipElement();
  if (!tooltip) {
    return;
  }
  tooltip.classList.add("is-hidden");
  tooltip.setAttribute("aria-hidden", "true");
}

function positionWorkflowNodeTooltip(tooltip, clientX, clientY) {
  const rect = dom.workflowGraph.getBoundingClientRect();
  const padding = 8;
  const offset = 14;
  let x = clientX - rect.left + offset;
  let y = clientY - rect.top + offset;
  const maxX = Math.max(padding, rect.width - tooltip.offsetWidth - padding);
  const maxY = Math.max(padding, rect.height - tooltip.offsetHeight - padding);
  x = Math.max(padding, Math.min(maxX, x));
  y = Math.max(padding, Math.min(maxY, y));
  tooltip.style.left = `${x}px`;
  tooltip.style.top = `${y}px`;
}

function showWorkflowNodeTooltip(nodeId, clientX, clientY) {
  const id = String(nodeId || "").trim();
  if (!id) {
    hideWorkflowNodeTooltip();
    return;
  }
  const details = state.workflow.nodeDetails.get(id);
  const tooltip = workflowTooltipElement();
  if (!details || !tooltip) {
    hideWorkflowNodeTooltip();
    return;
  }
  const taskText = String(details.instruction || "").trim() || t("none_value");
  const summaryText = String(details.summary || "").trim() || t("none_value");
  const modelText = String(details.model || "").trim() || t("none_value");
  tooltip.innerHTML = `
    <div class="graph-tooltip-title">${escapeHtml(details.name || id)}</div>
    <div class="graph-tooltip-sub">${escapeHtml(id)}</div>
    <div class="graph-tooltip-block">
      <div class="graph-tooltip-key">${escapeHtml(t("task"))}</div>
      <div class="graph-tooltip-value">${escapeHtml(taskText)}</div>
    </div>
    <div class="graph-tooltip-block">
      <div class="graph-tooltip-key">${escapeHtml(t("agent_model_label"))}</div>
      <div class="graph-tooltip-value">${escapeHtml(modelText)}</div>
    </div>
    <div class="graph-tooltip-block">
      <div class="graph-tooltip-key">${escapeHtml(t("summary"))}</div>
      <div class="graph-tooltip-value">${escapeHtml(summaryText)}</div>
    </div>
  `;
  tooltip.classList.remove("is-hidden");
  tooltip.setAttribute("aria-hidden", "false");
  positionWorkflowNodeTooltip(tooltip, clientX, clientY);
}

function buildGraphModel() {
  const nodes = state.agentOrder
    .map((id) => state.agents.get(id))
    .filter((agent) => agent !== undefined);
  if (nodes.length === 0) {
    return null;
  }

  const order = new Map(state.agentOrder.map((id, index) => [id, index]));
  const orderOf = (id) => (order.has(id) ? order.get(id) : Number.MAX_SAFE_INTEGER);
  const nodeIds = new Set(nodes.map((node) => node.id));
  const edges = nodes
    .filter((node) => node.parentAgentId && nodeIds.has(node.parentAgentId))
    .map((node) => ({ from: node.parentAgentId, to: node.id }));
  const childrenByParent = new Map(nodes.map((node) => [node.id, []]));
  for (const edge of edges) {
    if (!childrenByParent.has(edge.from)) {
      childrenByParent.set(edge.from, []);
    }
    childrenByParent.get(edge.from).push(edge.to);
  }
  for (const children of childrenByParent.values()) {
    children.sort((left, right) => orderOf(left) - orderOf(right));
  }

  const rootIds = nodes
    .filter((node) => !node.parentAgentId || !nodeIds.has(node.parentAgentId))
    .map((node) => node.id)
    .sort((left, right) => orderOf(left) - orderOf(right));
  if (rootIds.length === 0 && nodes.length > 0) {
    rootIds.push(nodes[0].id);
  }

  const depth = new Map();
  const queue = [...rootIds];
  for (const rootId of rootIds) {
    depth.set(rootId, 0);
  }
  while (queue.length > 0) {
    const currentId = queue.shift();
    const currentDepth = depth.get(currentId) || 0;
    const children = childrenByParent.get(currentId) || [];
    for (const childId of children) {
      const nextDepth = currentDepth + 1;
      const known = depth.get(childId);
      if (known === undefined || nextDepth > known) {
        depth.set(childId, nextDepth);
      }
      queue.push(childId);
    }
  }
  for (const node of nodes) {
    if (!depth.has(node.id)) {
      depth.set(node.id, 0);
    }
  }

  const centerIndex = new Map();
  const visiting = new Set();
  let leafCursor = 0;
  const assignCenter = (nodeId) => {
    if (centerIndex.has(nodeId)) {
      return centerIndex.get(nodeId);
    }
    if (visiting.has(nodeId)) {
      const fallback = leafCursor;
      leafCursor += 1;
      centerIndex.set(nodeId, fallback);
      return fallback;
    }
    visiting.add(nodeId);
    const children = childrenByParent.get(nodeId) || [];
    let center = 0;
    if (children.length === 0) {
      center = leafCursor;
      leafCursor += 1;
    } else {
      const childCenters = children.map((childId) => assignCenter(childId));
      const minCenter = Math.min(...childCenters);
      const maxCenter = Math.max(...childCenters);
      center = (minCenter + maxCenter) / 2;
    }
    visiting.delete(nodeId);
    centerIndex.set(nodeId, center);
    return center;
  };
  for (const rootId of rootIds) {
    assignCenter(rootId);
  }
  const remainingNodeIds = nodes
    .map((node) => node.id)
    .filter((id) => !centerIndex.has(id))
    .sort((left, right) => orderOf(left) - orderOf(right));
  for (const nodeId of remainingNodeIds) {
    assignCenter(nodeId);
  }

  const layout = {
    leftPadding: 42,
    topPadding: 26,
    rightPadding: 68,
    bottomPadding: 38,
    nodeWidth: 282,
    nodeHeight: 146,
    colGap: 344,
    rowGap: 184,
  };

  const positions = new Map();
  let maxDepth = 0;
  let maxX = layout.leftPadding + layout.nodeWidth;
  let maxY = layout.topPadding + layout.nodeHeight;
  for (const node of nodes) {
    const d = depth.get(node.id) || 0;
    const center = Number(centerIndex.get(node.id));
    const yCenter = Number.isFinite(center) ? center : 0;
    const x = layout.leftPadding + d * layout.colGap;
    const y = layout.topPadding + yCenter * layout.rowGap;
    positions.set(node.id, { x, y });
    maxDepth = Math.max(maxDepth, d);
    maxX = Math.max(maxX, x + layout.nodeWidth);
    maxY = Math.max(maxY, y + layout.nodeHeight);
  }

  const sortedDepths = [...new Set([...depth.values()])].sort((a, b) => a - b);
  const depthGuides = sortedDepths.map((d) => layout.leftPadding + d * layout.colGap + layout.nodeWidth / 2);
  const childCount = new Map(nodes.map((node) => [node.id, (childrenByParent.get(node.id) || []).length]));
  const summaryTransfers = [...state.workflow.summaryTransfers.values()]
    .filter((entry) => nodeIds.has(entry.childId) && nodeIds.has(entry.parentId))
    .map((entry) => ({
      from: entry.childId,
      to: entry.parentId,
      count: Number(entry.count || 1),
      timestamp: String(entry.timestamp || ""),
    }));

  return {
    nodes: nodes.map((node) => ({
      id: node.id,
      name: node.name,
      status: node.status,
      role: node.role,
      model: node.model,
      stepCount: node.stepCount,
      instruction: node.instruction,
      summary: node.summary,
      childCount: childCount.get(node.id) || 0,
    })),
    edges,
    summaryTransfers,
    positions,
    depthGuides,
    nodeWidth: layout.nodeWidth,
    nodeHeight: layout.nodeHeight,
    width: Math.max(420, maxX + layout.rightPadding + (maxDepth > 0 ? 10 : 0)),
    height: Math.max(220, maxY + layout.bottomPadding),
  };
}

function clampWorkflowScale(value) {
  return Math.max(state.workflow.minScale, Math.min(state.workflow.maxScale, value));
}

function setWorkflowExpanded(expanded) {
  const next = Boolean(expanded);
  if (state.workflow.expanded === next) {
    return;
  }
  state.workflow.expanded = next;
  if (!next) {
    state.workflow.dragging = false;
    dom.workflowGraph.classList.remove("dragging");
    state.workflow.nativeFullscreen = false;
  }
}

function workflowFullscreenElement() {
  const candidate =
    document.fullscreenElement ||
    document.webkitFullscreenElement ||
    null;
  return candidate;
}

function isWorkflowNativeFullscreen() {
  return workflowFullscreenElement() === dom.workflowPanel;
}

async function requestWorkflowNativeFullscreen() {
  if (!dom.workflowPanel) {
    return false;
  }
  const request = dom.workflowPanel.requestFullscreen || dom.workflowPanel.webkitRequestFullscreen;
  if (typeof request !== "function") {
    return false;
  }
  try {
    const result = request.call(dom.workflowPanel);
    if (result && typeof result.then === "function") {
      await result;
    }
    return isWorkflowNativeFullscreen();
  } catch (_error) {
    return false;
  }
}

async function exitNativeFullscreen() {
  const exit = document.exitFullscreen || document.webkitExitFullscreen;
  if (typeof exit !== "function") {
    return false;
  }
  try {
    const result = exit.call(document);
    if (result && typeof result.then === "function") {
      await result;
    }
    return true;
  } catch (_error) {
    return false;
  }
}

function zoomWorkflow(scaleFactor, anchorX, anchorY) {
  const previousScale = state.workflow.scale;
  const nextScale = clampWorkflowScale(previousScale * scaleFactor);
  if (Math.abs(nextScale - previousScale) < 0.001) {
    return;
  }
  state.workflow.scale = nextScale;
  state.workflow.tx = anchorX - ((anchorX - state.workflow.tx) * nextScale) / previousScale;
  state.workflow.ty = anchorY - ((anchorY - state.workflow.ty) * nextScale) / previousScale;
  applyWorkflowTransform();
}

function resetWorkflowOrigin() {
  state.workflow.tx = 0;
  state.workflow.ty = 0;
  dom.workflowGraph.scrollLeft = 0;
  dom.workflowGraph.scrollTop = 0;
  applyWorkflowTransform();
}

function resetWorkflowView() {
  state.workflow.scale = 1;
  resetWorkflowOrigin();
}

function applyWorkflowTransform() {
  const svg = dom.workflowGraph.querySelector(".workflow-svg");
  if (!svg) {
    return;
  }
  svg.style.transformOrigin = "0 0";
  svg.style.transform = `translate(${state.workflow.tx}px, ${state.workflow.ty}px) scale(${state.workflow.scale})`;
  if (dom.workflowZoomResetButton) {
    dom.workflowZoomResetButton.textContent = `${Math.round(state.workflow.scale * 100)}%`;
  }
}

function agentMasonryColumnCount(agentCount) {
  const totalAgents = Math.max(0, Math.floor(Number(agentCount) || 0));
  if (totalAgents <= 1) {
    return totalAgents === 1 ? 1 : 0;
  }
  const containerWidth = Number(dom.agentsLive?.clientWidth || 0);
  if (!Number.isFinite(containerWidth) || containerWidth <= 0) {
    return 1;
  }
  const estimated = Math.floor(
    (containerWidth + AGENT_MASONRY_COLUMN_GAP_PX) /
      (AGENT_MASONRY_MIN_COLUMN_WIDTH_PX + AGENT_MASONRY_COLUMN_GAP_PX)
  );
  return Math.max(1, Math.min(totalAgents, estimated));
}

function renderAgentMasonryLayout(agents) {
  const columns = agentMasonryColumnCount(agents.length);
  if (columns <= 0) {
    return `<div class="entry-sub">${escapeHtml(t("no_active_stream"))}</div>`;
  }
  const buckets = Array.from({ length: columns }, () => []);
  for (let index = 0; index < agents.length; index += 1) {
    const targetColumn = index % columns;
    buckets[targetColumn].push(renderAgentCard(agents[index]));
  }
  const renderedColumns = buckets
    .map(
      (cards, index) =>
        `<div class="agent-masonry-column" data-column="${index}">${cards.join("")}</div>`
    )
    .join("");
  return `<div class="agent-grid-inner" style="--masonry-columns: ${columns};">${renderedColumns}</div>`;
}

function normalizedAgentRole(role) {
  const normalized = String(role || "worker").trim().toLowerCase();
  return normalized === "root" ? "root" : "worker";
}

function matchesAgentRoleFilter(agent) {
  const roleFilter = String(state.agentsView.roleFilter || "all").trim().toLowerCase();
  if (roleFilter === "all") {
    return true;
  }
  return normalizedAgentRole(agent.role) === roleFilter;
}

function matchesAgentSearch(agent) {
  const query = String(state.agentsView.searchQuery || "").trim().toLowerCase();
  if (!query) {
    return true;
  }
  const haystack = [
    String(agent.name || ""),
    String(agent.id || ""),
    String(agent.role || ""),
    String(agent.instruction || ""),
    String(agent.summary || ""),
  ]
    .join(" ")
    .toLowerCase();
  return haystack.includes(query);
}

function renderAgentPanel() {
  if (state.activeTab !== "agents" && !state.agentFocus.open) {
    return;
  }
  if (!state.agentPanel.dirty) {
    return;
  }
  const now = performance.now();
  const elapsed = now - state.agentPanel.lastRenderedAt;
  if (state.runtime.running && elapsed < AGENT_PANEL_MIN_RENDER_INTERVAL_MS) {
    scheduleAgentPanelDeferredRender(AGENT_PANEL_MIN_RENDER_INTERVAL_MS - elapsed);
    return;
  }
  const allAgents = state.agentOrder
    .map((id) => state.agents.get(id))
    .filter((agent) => agent !== undefined);
  const agents = allAgents.filter((agent) => matchesAgentRoleFilter(agent) && matchesAgentSearch(agent));
  const previousTop = dom.agentsLive.scrollTop;
  const preserveScrollNextRender = Boolean(state.agentPanel.preserveScrollNextRender);
  const preservedScrollTop = Number(state.agentPanel.preservedScrollTop);
  const suppressAutoStickToBottom = now < Number(state.agentPanel.suppressAutoStickToBottomUntil || 0);
  const atBottom =
    dom.agentsLive.scrollTop + dom.agentsLive.clientHeight >= dom.agentsLive.scrollHeight - 8;
  if (allAgents.length === 0) {
    dom.agentsLive.innerHTML = `<div class="entry-sub">${escapeHtml(t("no_active_stream"))}</div>`;
    state.agentPanel.preserveScrollNextRender = false;
    state.agentPanel.preservedScrollTop = null;
    state.agentPanel.dirty = false;
    state.agentPanel.lastRenderedAt = now;
    return;
  }
  if (agents.length === 0) {
    dom.agentsLive.innerHTML = `<div class="entry-sub">${escapeHtml(t("agents_filter_empty"))}</div>`;
    state.agentPanel.preserveScrollNextRender = false;
    state.agentPanel.preservedScrollTop = null;
    state.agentPanel.dirty = false;
    state.agentPanel.lastRenderedAt = now;
    return;
  }
  dom.agentsLive.innerHTML = renderAgentMasonryLayout(agents);
  if (preserveScrollNextRender && Number.isFinite(preservedScrollTop)) {
    dom.agentsLive.scrollTop = Math.max(0, preservedScrollTop);
    state.agentPanel.preserveScrollNextRender = false;
    state.agentPanel.preservedScrollTop = null;
  } else if (atBottom && !suppressAutoStickToBottom) {
    dom.agentsLive.scrollTop = dom.agentsLive.scrollHeight;
  } else {
    dom.agentsLive.scrollTop = previousTop;
  }
  state.agentPanel.dirty = false;
  state.agentPanel.lastRenderedAt = now;
}

function renderAgentFocusOverlay() {
  const agentId = state.agentFocus.agentId;
  const agent =
    agentId && state.agents.has(agentId) ? state.agents.get(agentId) : null;
  const open = Boolean(state.agentFocus.open && agent);
  dom.agentFocusOverlay.classList.toggle("hidden", !open);
  dom.agentFocusOverlay.setAttribute("aria-hidden", open ? "false" : "true");
  if (!open || !agent) {
    dom.agentFocusBody.innerHTML = "";
    return;
  }

  const parentText = agent.parentAgentId
    ? `${parentAgentName(agent.parentAgentId)} (${agent.parentAgentId})`
    : t("none_value");
  const children = sortedChildren(agent.id);
  const childText =
    children.length > 0
      ? children.map((item) => `${item.name} (${item.id})`).join(", ")
      : t("none_value");
  const stepGroups = groupedAgentEntries(agent);

  dom.agentFocusTitle.textContent = `${agent.name} (${agent.id})`;
  dom.agentFocusBody.innerHTML = `
    <div class="agent-focus-meta">
      ${renderInfoRows([
        [t("session_status"), localizeStatus(agent.status)],
        [t("step_label"), String(agent.stepCount || 0)],
        [t("output_tokens_total"), String(agent.outputTokensTotal || 0)],
        [
          t("context_tokens_total"),
          agent.contextLimitTokens > 0
            ? `${agent.currentContextTokens || 0}/${agent.contextLimitTokens}`
            : t("none_value"),
        ],
        [t("usage_last_cache_label"), usageCacheText(agent)],
        [t("usage_last_total_label"), usageTotalText(agent)],
        [
          t("usage_ratio_label"),
          Number.isFinite(Number(agent.usageRatio || 0))
            ? Number(agent.usageRatio || 0).toFixed(4)
            : "0.0000",
        ],
        [t("context_warning_count_label"), contextWarningSummaryText(agent)],
        [t("compression_count_label"), String(agent.compressionCount || 0)],
        [t("last_compacted_label"), compactedRangeText(agent.lastCompactedStepRange)],
        [t("keep_pinned_messages_label"), String(Math.max(0, Number(agent.keepPinnedMessages || 0)))],
        [t("summary_version_label"), summaryVersionText(agent)],
        [t("agent_model_label"), String(agent.model || t("none_value"))],
        [t("parent_agent_label"), parentText],
        [t("child_agents_label"), childText],
      ])}
    </div>
    <div class="agent-focus-stream">
      ${renderStepGroups(agent, stepGroups)}
    </div>
  `;
}

function resetSteerComposeState() {
  state.steerCompose.open = false;
  state.steerCompose.agentId = null;
  state.steerCompose.content = "";
  state.steerCompose.error = "";
  state.steerCompose.submitting = false;
  state.steerCompose.focusRequested = false;
}

function openSteerComposeOverlay(agentId) {
  const normalizedAgentId = String(agentId || "").trim();
  if (!normalizedAgentId || !state.agents.has(normalizedAgentId)) {
    return;
  }
  state.steerCompose.open = true;
  state.steerCompose.agentId = normalizedAgentId;
  state.steerCompose.content = "";
  state.steerCompose.error = "";
  state.steerCompose.submitting = false;
  state.steerCompose.focusRequested = true;
  scheduleRender();
}

function closeSteerComposeOverlay(options = {}) {
  if (state.steerCompose.submitting) {
    return;
  }
  const cancelled = Boolean(options.cancelled);
  const wasOpen = state.steerCompose.open;
  resetSteerComposeState();
  if (cancelled && wasOpen) {
    state.runtime.status_message = t("steer_submit_cancelled");
  }
  scheduleRender();
}

function renderSteerComposeOverlay() {
  const agentId = String(state.steerCompose.agentId || "").trim();
  const agent = agentId && state.agents.has(agentId) ? state.agents.get(agentId) : null;
  const open = Boolean(state.steerCompose.open && agent);
  dom.steerComposeOverlay.classList.toggle("hidden", !open);
  dom.steerComposeOverlay.setAttribute("aria-hidden", open ? "false" : "true");
  dom.steerComposeTitle.textContent = t("steer_button");
  dom.steerComposeCancelButton.textContent = t("cancel");
  dom.steerComposeSubmitButton.textContent = t("steer_button");

  if (!open || !agent) {
    dom.steerComposePrompt.textContent = "";
    dom.steerComposeInput.placeholder = t("steer_input_prompt");
    dom.steerComposeInput.value = "";
    dom.steerComposeInput.disabled = false;
    dom.steerComposeCancelButton.disabled = false;
    dom.steerComposeSubmitButton.disabled = false;
    dom.steerComposeError.textContent = "";
    return;
  }

  const agentLabel = `${agent.name} (${agent.id})`;
  dom.steerComposePrompt.textContent = `${t("steer_input_prompt")} ${agentLabel}`;
  dom.steerComposeInput.placeholder = t("steer_input_prompt");
  if (dom.steerComposeInput.value !== state.steerCompose.content) {
    dom.steerComposeInput.value = state.steerCompose.content;
  }
  dom.steerComposeInput.disabled = state.steerCompose.submitting;
  dom.steerComposeCancelButton.disabled = state.steerCompose.submitting;
  dom.steerComposeSubmitButton.disabled = state.steerCompose.submitting;
  dom.steerComposeError.textContent = state.steerCompose.error || "";
  if (state.steerCompose.focusRequested && !state.steerCompose.submitting) {
    state.steerCompose.focusRequested = false;
    window.requestAnimationFrame(() => {
      dom.steerComposeInput.focus();
      const caret = dom.steerComposeInput.value.length;
      dom.steerComposeInput.setSelectionRange(caret, caret);
    });
  }
}

function formatToolRunDetailTimestamp(value) {
  const text = String(value || "").trim();
  if (!text) {
    return "-";
  }
  if (text.length >= 19) {
    return `${text.slice(0, 10)} ${text.slice(11, 19)}`;
  }
  return text;
}

function summarizeToolRunTimelineEntry(entry) {
  if (!entry || typeof entry !== "object") {
    return "-";
  }
  const eventType = String(entry.event_type || "");
  const payload = entry.payload && typeof entry.payload === "object" ? entry.payload : {};
  if (eventType === "tool_call_started" || eventType === "tool_call") {
    const action = payload.action && typeof payload.action === "object" ? payload.action : {};
    return summarizeAction(action);
  }
  if (eventType === "tool_run_submitted") {
    return `id=${String(payload.tool_run_id || "-")} tool=${String(payload.tool_name || "-")}`;
  }
  if (eventType === "tool_run_updated") {
    return `id=${String(payload.tool_run_id || "-")} status=${String(payload.status || "-")}`;
  }
  return eventType || "-";
}

function renderToolRunJsonValue(value, emptyText) {
  const formatted = formatStructuredValue(value, { fallback: "" });
  if (!formatted) {
    return `<div class="entry-sub">${escapeHtml(emptyText)}</div>`;
  }
  return `<pre class="json-block"><code>${escapeHtml(formatted)}</code></pre>`;
}

function renderToolRunTimeline(runId) {
  const entries = state.toolRuns.eventTimelineByRunId.get(runId) || [];
  if (!Array.isArray(entries) || entries.length === 0) {
    return `<div class="entry-sub">${escapeHtml(t("tool_runs_detail_no_timeline"))}</div>`;
  }
  return entries
    .map((entry) => {
      const payload = entry.payload && typeof entry.payload === "object" ? entry.payload : {};
      return `
        <details class="tool-run-event-entry">
          <summary>
            <span class="badge">${escapeHtml(String(entry.event_type || "-"))}</span>
            <span class="tool-run-event-summary">${escapeHtml(summarizeToolRunTimelineEntry(entry))}</span>
            <span class="tool-run-event-time">${escapeHtml(
              formatToolRunDetailTimestamp(entry.timestamp)
            )}</span>
          </summary>
          <div class="tool-run-event-body">
            <div class="tool-run-event-actor">${escapeHtml(
              `${t("agents")}: ${String(entry.agent_id || t("none_value"))}`
            )}</div>
            ${renderToolRunJsonValue(payload, t("none_value"))}
          </div>
        </details>
      `;
    })
    .join("");
}

function renderToolRunDetailOverlay() {
  const runId = String(state.toolRuns.detail.runId || "").trim();
  const open = Boolean(state.toolRuns.detail.open && runId);
  dom.toolRunDetailOverlay.classList.toggle("hidden", !open);
  dom.toolRunDetailOverlay.setAttribute("aria-hidden", open ? "false" : "true");
  dom.toolRunDetailCloseButton.textContent = t("tool_runs_detail_close");
  if (!open) {
    dom.toolRunDetailTitle.textContent = t("tool_runs_detail_title");
    dom.toolRunDetailBody.innerHTML = "";
    return;
  }

  updateToolRunDetailSnapshot(runId);
  const run = state.toolRuns.detail.runSnapshot || findToolRunFromCurrentPage(runId);
  dom.toolRunDetailTitle.textContent = `${t("tool_runs_detail_title")} · ${runId}`;
  if (!run || typeof run !== "object") {
    dom.toolRunDetailBody.innerHTML = `<div class="entry-sub">${escapeHtml(t("none_value"))}</div>`;
    return;
  }

  const runError = String(run.error || "").trim();
  let resultValue = run.result === undefined ? null : run.result;
  if (String(run.tool_name || "").trim() === "shell") {
    const shellOutput = {
      stdout: String(run.stdout || ""),
      stderr: String(run.stderr || ""),
    };
    if (!resultValue || typeof resultValue !== "object") {
      resultValue = shellOutput;
    } else {
      resultValue = {
        ...resultValue,
        stdout:
          resultValue.stdout === undefined || resultValue.stdout === ""
            ? shellOutput.stdout
            : resultValue.stdout,
        stderr:
          resultValue.stderr === undefined || resultValue.stderr === ""
            ? shellOutput.stderr
            : resultValue.stderr,
      };
    }
  }
  dom.toolRunDetailBody.innerHTML = `
    <div class="tool-run-detail-layout">
      <section class="tool-run-detail-card">
        <div class="tool-run-detail-card-head">${escapeHtml(t("tool_runs_detail_overview"))}</div>
        ${renderInfoRows([
          [t("session_status"), localizeStatus(run.status || "pending")],
          [t("session_id"), String(run.session_id || "-")],
          [t("agents"), String(run.agent_id || "-")],
          [t("tool_runs_group_tool"), String(run.tool_name || "-")],
          [t("tool_runs_duration"), formatDurationMs(toolRunDurationMs(run))],
          [t("tool_runs_detail_parent_run"), String(run.parent_run_id || t("none_value"))],
          [t("tool_runs_detail_created_at"), formatToolRunDetailTimestamp(run.created_at)],
          [t("tool_runs_detail_started_at"), formatToolRunDetailTimestamp(run.started_at)],
          [t("tool_runs_detail_completed_at"), formatToolRunDetailTimestamp(run.completed_at)],
        ])}
      </section>
      <section class="tool-run-detail-card">
        <div class="tool-run-detail-card-head">${escapeHtml(t("tool_runs_detail_arguments"))}</div>
        ${renderToolRunJsonValue(run.arguments || {}, t("none_value"))}
      </section>
      <section class="tool-run-detail-card">
        <div class="tool-run-detail-card-head">${escapeHtml(t("tool_runs_detail_result"))}</div>
        ${renderToolRunJsonValue(resultValue, t("tool_runs_detail_no_result"))}
      </section>
      ${
        runError
          ? `<section class="tool-run-detail-card">
        <div class="tool-run-detail-card-head">${escapeHtml(t("tool_runs_detail_error"))}</div>
        <div class="plain-inline">${escapeHtml(runError)}</div>
      </section>`
          : ""
      }
      <section class="tool-run-detail-card tool-run-detail-card-timeline">
        <div class="tool-run-detail-card-head">${escapeHtml(t("tool_runs_detail_timeline"))}</div>
        <div class="tool-run-event-list">${renderToolRunTimeline(runId)}</div>
      </section>
    </div>
  `;
}

async function copyTextToClipboard(value) {
  const text = String(value || "");
  if (!text.trim()) {
    return false;
  }
  try {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_error) {
    // Fall back to a hidden textarea for environments without clipboard permission.
  }
  const textArea = document.createElement("textarea");
  textArea.value = text;
  textArea.setAttribute("readonly", "");
  textArea.style.position = "fixed";
  textArea.style.top = "-9999px";
  textArea.style.left = "-9999px";
  document.body.appendChild(textArea);
  textArea.focus();
  textArea.select();
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch (_error) {
    copied = false;
  } finally {
    document.body.removeChild(textArea);
  }
  return copied;
}

async function copyAgentField(value, successMessageKey) {
  const copied = await copyTextToClipboard(value);
  if (copied) {
    state.runtime.status_message = t(successMessageKey);
  } else {
    state.runtime.status_message = t("copy_failed");
  }
  scheduleRender();
}

function renderAgentCard(agent) {
  const parentButton = renderAgentJumpButton(agent.parentAgentId);
  const children = sortedChildren(agent.id);
  const childButtons =
    children.length > 0
      ? children
          .map((item) => renderAgentJumpButton(item.id, { label: `${item.name} (${item.id})` }))
          .join("")
      : `<span class="agent-meta-empty">${escapeHtml(t("none_value"))}</span>`;
  const statusClass = `status-${normalizeStatus(agent.status)}`;
  const stepGroups = groupedAgentEntries(agent);

  return `
    <section class="agent-live-card" data-agent-id="${escapeHtml(agent.id)}">
      <div class="agent-live-head">
        <div>
          <div class="agent-name">${escapeHtml(`${statusToEmoji(agent.status)} ${agent.name}`)}</div>
          <div class="agent-sub">
            <button
              type="button"
              class="agent-copy-chip"
              data-action="copy-agent-name"
              data-copy-value="${escapeHtml(agent.name)}"
              title="${escapeHtml(t("copy_agent_name_button"))}"
            >${escapeHtml(t("copy_agent_name_button"))}: ${escapeHtml(agent.name)}</button>
            <button
              type="button"
              class="agent-copy-chip"
              data-action="copy-agent-id"
              data-copy-value="${escapeHtml(agent.id)}"
              title="${escapeHtml(t("copy_agent_id_button"))}"
            >${escapeHtml(t("copy_agent_id_button"))}: ${escapeHtml(agent.id)}</button>
            <span class="agent-role-pill">${escapeHtml(agent.role || "worker")}</span>
          </div>
        </div>
        <div class="agent-head-actions">
          <div class="badge ${statusClass}">${escapeHtml(localizeStatus(agent.status))}</div>
          <button
            type="button"
            class="agent-focus-trigger"
            data-action="focus-agent"
            data-agent-id="${escapeHtml(agent.id)}"
          >${escapeHtml(t("expand_view"))}</button>
        </div>
      </div>
      <button
        type="button"
        class="agent-steer-trigger"
        data-action="steer-agent"
        data-agent-id="${escapeHtml(agent.id)}"
      >${escapeHtml(t("steer_button"))}</button>
      <button
        type="button"
        class="agent-terminate-trigger"
        data-action="terminate-agent"
        data-agent-id="${escapeHtml(agent.id)}"
      >${escapeHtml(t("terminate_button"))}</button>
      <div class="agent-meta-grid">
        <div class="agent-meta-line">
          <span class="agent-meta-key">${escapeHtml(t("step_label"))}</span>
          <span class="badge">${escapeHtml(String(agent.stepCount || 0))}</span>
        </div>
        <div class="agent-meta-line">
          <span class="agent-meta-key">${escapeHtml(t("output_tokens_total"))}</span>
          <span class="badge">${escapeHtml(String(agent.outputTokensTotal || 0))}</span>
        </div>
        <div class="agent-meta-line">
          <span class="agent-meta-key">${escapeHtml(t("context_tokens_total"))}</span>
          <span class="badge">${escapeHtml(
            agent.contextLimitTokens > 0
              ? `${agent.currentContextTokens || 0}/${agent.contextLimitTokens}`
              : t("none_value")
          )}</span>
        </div>
        <div class="agent-meta-line">
          <span class="agent-meta-key">${escapeHtml(t("usage_last_cache_label"))}</span>
          <span class="badge">${escapeHtml(usageCacheText(agent))}</span>
        </div>
        <div class="agent-meta-line">
          <span class="agent-meta-key">${escapeHtml(t("usage_last_total_label"))}</span>
          <span class="badge">${escapeHtml(usageTotalText(agent))}</span>
        </div>
        <div class="agent-meta-line">
          <span class="agent-meta-key">${escapeHtml(t("usage_ratio_label"))}</span>
          <span class="badge">${escapeHtml(
            Number.isFinite(Number(agent.usageRatio || 0))
              ? Number(agent.usageRatio || 0).toFixed(4)
              : "0.0000"
          )}</span>
        </div>
        <div class="agent-meta-line">
          <span class="agent-meta-key">${escapeHtml(t("context_warning_count_label"))}</span>
          <span class="badge">${escapeHtml(contextWarningSummaryText(agent))}</span>
        </div>
        <div class="agent-meta-line">
          <span class="agent-meta-key">${escapeHtml(t("compression_count_label"))}</span>
          <span class="badge">${escapeHtml(String(agent.compressionCount || 0))}</span>
        </div>
        <div class="agent-meta-line">
          <span class="agent-meta-key">${escapeHtml(t("last_compacted_label"))}</span>
          <span class="badge">${escapeHtml(compactedRangeText(agent.lastCompactedStepRange))}</span>
        </div>
        <div class="agent-meta-line">
          <span class="agent-meta-key">${escapeHtml(t("keep_pinned_messages_label"))}</span>
          <span class="badge">${escapeHtml(String(Math.max(0, Number(agent.keepPinnedMessages || 0))))}</span>
        </div>
        <div class="agent-meta-line">
          <span class="agent-meta-key">${escapeHtml(t("summary_version_label"))}</span>
          <span class="badge">${escapeHtml(summaryVersionText(agent))}</span>
        </div>
        <div class="agent-meta-line">
          <span class="agent-meta-key">${escapeHtml(t("agent_model_label"))}</span>
          <span class="badge">${escapeHtml(String(agent.model || t("none_value")))}</span>
        </div>
        <div class="agent-meta-line agent-meta-line-wide">
          <span class="agent-meta-key">${escapeHtml(t("parent_agent_label"))}</span>
          <div class="agent-link-wrap">${parentButton}</div>
        </div>
        <div class="agent-meta-line agent-meta-line-wide">
          <span class="agent-meta-key">${escapeHtml(t("child_agents_label"))}</span>
          <div class="agent-link-wrap">${childButtons}</div>
        </div>
      </div>
      <div class="agent-stream-list">${renderStepGroups(agent, stepGroups)}</div>
    </section>
  `;
}

function renderAgentJumpButton(agentId, options = {}) {
  const id = String(agentId || "").trim();
  if (!id || !state.agents.has(id)) {
    return `<span class="agent-meta-empty">${escapeHtml(t("none_value"))}</span>`;
  }
  const linked = state.agents.get(id);
  const label = String(options.label || `${linked.name} (${id})`);
  return `<button type="button" class="agent-jump-link" data-action="jump-agent" data-agent-id="${escapeHtml(
    id
  )}">${escapeHtml(label)}</button>`;
}

function jumpToAgentCard(agentId) {
  const id = String(agentId || "").trim();
  if (!id) {
    return;
  }
  const card = [...dom.agentsLive.querySelectorAll(".agent-live-card[data-agent-id]")].find(
    (element) => element.getAttribute("data-agent-id") === id
  );
  if (!card) {
    return;
  }
  card.scrollIntoView({ behavior: "smooth", block: "center" });
  card.classList.remove("jump-target");
  void card.offsetWidth;
  card.classList.add("jump-target");
}

function expandedStepsFor(agentId) {
  const id = String(agentId || "").trim();
  if (!id) {
    return new Set();
  }
  if (!state.agentPanel.expandedSteps.has(id)) {
    state.agentPanel.expandedSteps.set(id, new Set());
  }
  return state.agentPanel.expandedSteps.get(id);
}

function isStepExpanded(agentId, stepNumber) {
  const set = state.agentPanel.expandedSteps.get(String(agentId || "").trim());
  return Boolean(set && set.has(stepNumber));
}

function setStepExpanded(agentId, stepNumber, expanded) {
  const id = String(agentId || "").trim();
  if (!id || !Number.isFinite(stepNumber)) {
    return;
  }
  const set = expandedStepsFor(id);
  if (expanded) {
    set.add(stepNumber);
  } else {
    set.delete(stepNumber);
  }
  if (set.size === 0) {
    state.agentPanel.expandedSteps.delete(id);
  }
}

function flattenAgentEntries(agent) {
  const rows = [];
  for (const step of effectiveStepOrder(agent)) {
    for (const entry of entriesForStep(agent, step)) {
      if (!shouldDisplayAgentEntry(entry.kind)) {
        continue;
      }
      rows.push({
        step,
        kind: String(entry.kind || "reply"),
        text: String(entry.text || ""),
      });
    }
  }
  return rows;
}

function shouldDisplayAgentEntry(kind) {
  return !isNonMessageKind(kind);
}

function groupedAgentEntries(agent, maxEntries = null) {
  const rows = flattenAgentEntries(agent);
  const limit = Number(maxEntries);
  const visibleRows = Number.isFinite(limit) && limit > 0 ? rows.slice(-Math.floor(limit)) : rows;
  const grouped = new Map();
  for (const row of visibleRows) {
    const step = Number(row.step);
    if (!Number.isFinite(step)) {
      continue;
    }
    if (!grouped.has(step)) {
      grouped.set(step, []);
    }
    grouped.get(step).push(row);
  }
  const stepSet = new Set();
  for (const step of grouped.keys()) {
    stepSet.add(step);
  }
  if (Array.isArray(agent.stepOrder)) {
    for (const step of agent.stepOrder) {
      const normalized = Number(step);
      if (Number.isFinite(normalized) && normalized > 0) {
        stepSet.add(Math.floor(normalized));
      }
    }
  }
  const orderedSteps = [...stepSet].sort((a, b) => a - b);
  if (!hasContextSummary(agent)) {
    return orderedSteps.map((step) => ({
      step,
      title: `${t("step_label")} ${step}`,
      entries: grouped.get(step) || [],
    }));
  }
  const pinnedSteps = pinnedStepNumbers(agent, orderedSteps);
  const pinnedSet = new Set(pinnedSteps);
  const summarizedSet = summarizedStepNumbers(agent);
  const unsummarizedSteps = orderedSteps.filter((step) => !pinnedSet.has(step) && !summarizedSet.has(step));
  const rendered = [];
  for (const step of pinnedSteps) {
    rendered.push({
      step,
      title: `${t("step_label")} ${step}`,
      entries: grouped.get(step) || [],
    });
  }
  rendered.push({
    step: 0,
    title: t("context_latest_summary_label"),
    entries: [
      {
        step: 0,
        stepLabel: t("context_latest_summary_label"),
        kind: "summary",
        text: contextSummaryText(agent) || t("none_value"),
      },
    ],
  });
  for (const step of unsummarizedSteps) {
    rendered.push({
      step,
      title: `${t("step_label")} ${step}`,
      entries: grouped.get(step) || [],
    });
  }
  return rendered;
}

function tryParseJson(text) {
  const source = String(text || "").trim();
  if (!source) {
    return null;
  }
  const looksLikeJson =
    (source.startsWith("{") && source.endsWith("}")) ||
    (source.startsWith("[") && source.endsWith("]"));
  if (!looksLikeJson) {
    return null;
  }
  try {
    return JSON.parse(source);
  } catch (_error) {
    return null;
  }
}

function isLlmStreamKind(kind) {
  const normalizedKind = baseStreamKind(kind);
  return (
    normalizedKind === "thinking" ||
    normalizedKind === "reply" ||
    normalizedKind === "response" ||
    normalizedKind === "summary"
  );
}

function renderStreamContent(text, { markdown = false } = {}) {
  if (markdown) {
    return `<div class="markdown-inline">${renderMarkdown(text)}</div>`;
  }
  const parsed = tryParseJson(text);
  if (parsed !== null) {
    return `<pre class="json-block"><code>${escapeHtml(JSON.stringify(parsed, null, 2))}</code></pre>`;
  }
  return `<div class="plain-inline">${escapeHtml(String(text || ""))}</div>`;
}

function renderStreamEntry(entry) {
  const normalizedKind = baseStreamKind(entry.kind);
  const kindClass = `stream-kind-${normalizedKind.replace(/[^\w-]/g, "_")}`;
  const nonMessage =
    isNonMessageKind(entry.kind) || normalizedKind === "stdout" || normalizedKind === "stderr";
  const nonMessageTag = nonMessage ? ` · ${t("stream_not_in_messages")}` : "";
  const stepText = String(entry.stepLabel || `${t("step_label")} ${entry.step}`);
  const head = `${entryEmoji(entry.kind)} ${streamLabel(entry.kind)} · ${stepText}${nonMessageTag}`;
  const markdown = isLlmStreamKind(entry.kind);
  return `
    <div class="stream-item ${kindClass}${nonMessage ? " stream-non-message" : ""}">
      <div class="stream-head">${escapeHtml(head)}</div>
      ${renderStreamContent(entry.text, { markdown })}
    </div>
  `;
}

function renderStepGroups(agent, groups) {
  if (!Array.isArray(groups) || groups.length === 0) {
    return `<div class="entry-sub">${escapeHtml(t("no_active_stream"))}</div>`;
  }
  return `<div class="step-group-list">${groups
    .map((group) => {
      const step = Number(group.step);
      if (!Number.isFinite(step)) {
        return "";
      }
      const title = String(group.title || `${t("step_label")} ${step}`);
      const openAttr = isStepExpanded(agent.id, step) ? " open" : "";
      const entries = Array.isArray(group.entries) ? group.entries : [];
      return `
        <details class="step-group" data-agent-id="${escapeHtml(agent.id)}" data-step="${step}"${openAttr}>
          <summary>
            <span class="step-group-title">${escapeHtml(title)}</span>
            <span class="step-group-meta">${escapeHtml(String(entries.length))}</span>
          </summary>
          <div class="step-group-body">${entries.map((entry) => renderStreamEntry(entry)).join("")}</div>
        </details>
      `;
    })
    .join("")}</div>`;
}

function streamLabel(kind) {
  const normalizedKind = baseStreamKind(kind);
  const mapping = {
    thinking: t("stream_thinking"),
    reply: t("stream_reply"),
    response: t("stream_reply"),
    tool_call: t("stream_tool_call"),
    tool_return: t("stream_tool_return"),
    multiagent_call: t("stream_multiagent_call"),
    user_message: t("message"),
    tool_message: t("stream_tool_return"),
    multiagent_return: t("stream_multiagent_return"),
    stdout: t("stream_stdout"),
    stderr: t("stream_stderr"),
    summary: t("stream_summary"),
    error: t("stream_error"),
    control: t("stream_control"),
  };
  return mapping[normalizedKind] || normalizedKind;
}

function entryEmoji(kind) {
  const normalizedKind = baseStreamKind(kind);
  const mapping = {
    thinking: "🧠",
    reply: "💬",
    response: "🗣️",
    tool_call: "🛠️",
    tool_return: "📦",
    multiagent_call: "🤝",
    user_message: "👤",
    tool_message: "🧾",
    multiagent_return: "🤝",
    stdout: "📘",
    stderr: "⚠️",
    summary: "✅",
    error: "🚨",
    control: "🧭",
  };
  return mapping[normalizedKind] || "•";
}

function toolRunStatusesForFilter(filterKey) {
  const key = String(filterKey || "all");
  if (key === "pending") {
    return ["queued", "running"];
  }
  if (key === "running") {
    return ["running"];
  }
  if (key === "completed") {
    return ["completed"];
  }
  if (key === "failed") {
    return ["failed"];
  }
  if (key === "cancelled") {
    return ["cancelled"];
  }
  return [];
}

function toolRunFilterLabel(filterKey) {
  const key = String(filterKey || "all");
  const mapping = {
    all: t("tool_runs_filter_all"),
    pending: t("tool_runs_filter_pending"),
    running: t("tool_runs_filter_running"),
    completed: t("tool_runs_filter_completed"),
    failed: t("tool_runs_filter_failed"),
    cancelled: t("tool_runs_filter_cancelled"),
  };
  return mapping[key] || mapping.all;
}

function toolRunGroupLabel(groupBy) {
  const key = String(groupBy || "agent");
  const mapping = {
    agent: t("tool_runs_group_agent"),
    tool: t("tool_runs_group_tool"),
    status: t("tool_runs_group_status"),
  };
  return mapping[key] || mapping.agent;
}

function cycleToolRunFilter() {
  const current = TOOL_RUN_FILTER_KEYS.indexOf(state.toolRuns.filter);
  const next = current >= 0 ? (current + 1) % TOOL_RUN_FILTER_KEYS.length : 0;
  state.toolRuns.filter = TOOL_RUN_FILTER_KEYS[next];
  state.toolRuns.dirty = true;
}

function cycleToolRunGroupBy() {
  const current = TOOL_RUN_GROUP_KEYS.indexOf(state.toolRuns.groupBy);
  const next = current >= 0 ? (current + 1) % TOOL_RUN_GROUP_KEYS.length : 0;
  state.toolRuns.groupBy = TOOL_RUN_GROUP_KEYS[next];
}

function steerRunStatusesForFilter(filterKey) {
  const key = String(filterKey || "all");
  if (key === "waiting") {
    return ["waiting"];
  }
  if (key === "completed") {
    return ["completed"];
  }
  if (key === "cancelled") {
    return ["cancelled"];
  }
  return [];
}

function steerRunFilterLabel(filterKey) {
  const key = String(filterKey || "all");
  const mapping = {
    all: t("steer_runs_filter_all"),
    waiting: t("steer_runs_filter_waiting"),
    completed: t("steer_runs_filter_completed"),
    cancelled: t("steer_runs_filter_cancelled"),
  };
  return mapping[key] || mapping.all;
}

function steerRunGroupLabel(groupBy) {
  const key = String(groupBy || "agent");
  const mapping = {
    agent: t("steer_runs_group_agent"),
    status: t("steer_runs_group_status"),
    source: t("steer_runs_group_source"),
  };
  return mapping[key] || mapping.agent;
}

function cycleSteerRunFilter() {
  const current = STEER_RUN_FILTER_KEYS.indexOf(state.steerRuns.filter);
  const next = current >= 0 ? (current + 1) % STEER_RUN_FILTER_KEYS.length : 0;
  state.steerRuns.filter = STEER_RUN_FILTER_KEYS[next];
  state.steerRuns.dirty = true;
}

function cycleSteerRunGroupBy() {
  const current = STEER_RUN_GROUP_KEYS.indexOf(state.steerRuns.groupBy);
  const next = current >= 0 ? (current + 1) % STEER_RUN_GROUP_KEYS.length : 0;
  state.steerRuns.groupBy = STEER_RUN_GROUP_KEYS[next];
}

function steerRunSourceGroupKey(run) {
  const actor = formatSteerSourceActor(run);
  return String(actor || "-").trim() || "-";
}

function matchesSteerRunSearch(run) {
  const query = String(state.steerRuns.searchQuery || "").trim().toLowerCase();
  if (!query) {
    return true;
  }
  const targetActor = formatSteerRunTargetActor(run);
  const sourceActor = formatSteerSourceActor(run);
  const haystack = [
    String(run.id || ""),
    String(run.agent_id || ""),
    String(run.target_agent_name || ""),
    String(run.status || ""),
    String(run.content || ""),
    String(run.source || ""),
    sourceActor,
    targetActor,
    String(run.source_agent_name || ""),
    String(run.created_at || ""),
  ]
    .join(" ")
    .toLowerCase();
  return haystack.includes(query);
}

function parseTimestampMs(value) {
  const text = String(value || "").trim();
  if (!text) {
    return null;
  }
  const normalized = text.endsWith("Z") ? `${text.slice(0, -1)}+00:00` : text;
  const parsed = Date.parse(normalized);
  if (!Number.isFinite(parsed)) {
    return null;
  }
  return parsed;
}

function toolRunDurationMs(run) {
  const createdAt = parseTimestampMs(run.created_at);
  const startedAt = parseTimestampMs(run.started_at) ?? createdAt;
  if (startedAt === null) {
    return null;
  }
  const completedAt = parseTimestampMs(run.completed_at);
  let end = completedAt;
  if (end === null) {
    const status = String(run.status || "").toLowerCase();
    if (status === "completed" || status === "failed" || status === "cancelled") {
      return null;
    }
    end = Date.now();
  }
  const duration = Math.max(0, Math.round(end - startedAt));
  return Number.isFinite(duration) ? duration : null;
}

function formatDurationMs(value) {
  if (!Number.isFinite(value) || value < 0) {
    return "-";
  }
  if (value < 1000) {
    return `${value}ms`;
  }
  const seconds = value / 1000;
  if (seconds < 60) {
    return `${seconds.toFixed(seconds < 10 ? 2 : 1)}s`;
  }
  const minutes = Math.floor(seconds / 60);
  const remaining = Math.round(seconds % 60);
  return `${minutes}m${remaining}s`;
}

function agentNameById(agentId) {
  const normalizedAgentId = String(agentId || "").trim();
  if (!normalizedAgentId || !state.agents.has(normalizedAgentId)) {
    return "";
  }
  const agent = state.agents.get(normalizedAgentId);
  return agent && typeof agent.name === "string" ? String(agent.name).trim() : "";
}

function formatSteerRunTargetActor(value) {
  const targetAgentId = String((value && value.agent_id) || "").trim();
  if (!targetAgentId) {
    return "-";
  }
  const targetAgentName = String(
    (
      value
      && (value.target_agent_name || value.agent_name || agentNameById(targetAgentId))
    )
      || ""
  ).trim();
  if (targetAgentName && targetAgentName !== targetAgentId) {
    return `${targetAgentName} (${targetAgentId})`;
  }
  return targetAgentId;
}

function formatSteerSourceActor(value) {
  const sourceAgentId = String((value && value.source_agent_id) || "").trim();
  const sourceAgentName = String((value && value.source_agent_name) || "").trim();
  if (!sourceAgentId || sourceAgentId === "user") {
    return state.locale === "zh" ? "用户" : "user";
  }
  const resolvedName = sourceAgentName || agentNameById(sourceAgentId);
  if (resolvedName && resolvedName !== sourceAgentId) {
    return `${resolvedName} (${sourceAgentId})`;
  }
  return sourceAgentId;
}

function formatSteerRunDelivery(value) {
  const deliveredStep = Number((value && value.delivered_step) ?? NaN);
  if (Number.isInteger(deliveredStep) && deliveredStep > 0) {
    return `${t("step_label")} ${deliveredStep}`;
  }
  const status = normalizeStatus(String((value && value.status) || ""));
  if (status === "waiting") {
    return t("steer_runs_pending_delivery");
  }
  if (status === "cancelled") {
    return t("steer_runs_cancelled_before_delivery");
  }
  if (status === "completed") {
    return t("steer_runs_delivery_unknown");
  }
  return t("none_value");
}

function buildToolRunGroups(runs, groupBy) {
  const groups = new Map();
  for (const run of runs) {
    let key = "-";
    if (groupBy === "tool") {
      key = String(run.tool_name || "-");
    } else if (groupBy === "status") {
      key = String(run.status || "-");
    } else {
      key = String(run.agent_id || "-");
    }
    if (!groups.has(key)) {
      groups.set(key, []);
    }
    groups.get(key).push(run);
  }
  return [...groups.entries()].sort((a, b) => {
    if (b[1].length !== a[1].length) {
      return b[1].length - a[1].length;
    }
    return String(a[0]).localeCompare(String(b[0]));
  });
}

function metricCell(label, value, classPrefix = "tool-runs") {
  const normalizedPrefix = String(classPrefix || "tool-runs");
  return `
    <div class="${escapeHtml(`${normalizedPrefix}-metric`)}">
      <div class="${escapeHtml(`${normalizedPrefix}-metric-label`)}">${escapeHtml(String(label || ""))}</div>
      <div class="${escapeHtml(`${normalizedPrefix}-metric-value`)}">${escapeHtml(String(value ?? "-"))}</div>
    </div>
  `;
}

function renderToolRunsPanel() {
  if (state.activeTab !== "tool-runs") {
    return;
  }
  if (state.toolRuns.dirty && !state.toolRuns.loading) {
    void refreshToolRuns();
  }

  dom.toolRunsRefreshButton.textContent = t("reload");
  dom.toolRunsFilterButton.textContent = `${t("tool_runs_filter")}: ${toolRunFilterLabel(
    state.toolRuns.filter
  )}`;
  dom.toolRunsGroupButton.textContent = `${t("tool_runs_group")}: ${toolRunGroupLabel(
    state.toolRuns.groupBy
  )}`;
  dom.toolRunsRefreshButton.disabled = state.toolRuns.loading;
  dom.toolRunsFilterButton.disabled = state.toolRuns.loading;
  dom.toolRunsGroupButton.disabled = state.toolRuns.loading;

  const sessionId = activeSessionId();
  if (!sessionId) {
    dom.toolRunsStatus.textContent = t("tool_runs_no_session");
    dom.toolRunsSummary.innerHTML = "";
    dom.toolRunsContent.innerHTML = `<div class="entry-sub">${escapeHtml(t("tool_runs_no_session"))}</div>`;
    return;
  }

  if (state.toolRuns.error) {
    dom.toolRunsStatus.textContent = state.toolRuns.error;
  } else if (state.toolRuns.loading) {
    dom.toolRunsStatus.textContent = t("tool_runs_loading");
  } else {
    const runs = Array.isArray(state.toolRuns.page.tool_runs) ? state.toolRuns.page.tool_runs : [];
    dom.toolRunsStatus.textContent = `${t("tool_runs_count")}: ${runs.length}`;
  }

  const metrics = state.toolRuns.metrics && typeof state.toolRuns.metrics === "object"
    ? state.toolRuns.metrics
    : null;
  if (metrics) {
    const duration = metrics.duration_ms && typeof metrics.duration_ms === "object" ? metrics.duration_ms : {};
    const statusCounts =
      metrics.status_counts && typeof metrics.status_counts === "object" ? metrics.status_counts : {};
    const failureRate = Number(metrics.failure_rate || 0);
    const failedOrCancelRate = Number(metrics.failure_or_cancel_rate || 0);
    dom.toolRunsSummary.innerHTML = `
      <div class="tool-runs-metrics-grid">
        ${metricCell(t("tool_runs_metric_total"), metrics.total_runs ?? 0)}
        ${metricCell(t("tool_runs_metric_pending"), (statusCounts.queued || 0) + (statusCounts.running || 0))}
        ${metricCell(t("tool_runs_metric_terminal"), metrics.terminal_runs ?? 0)}
        ${metricCell(t("tool_runs_metric_failure_rate"), `${(failureRate * 100).toFixed(2)}%`)}
        ${metricCell(t("tool_runs_metric_failure_or_cancel_rate"), `${(failedOrCancelRate * 100).toFixed(2)}%`)}
        ${metricCell(t("tool_runs_metric_p50"), formatDurationMs(Number(duration.p50 ?? NaN)))}
        ${metricCell(t("tool_runs_metric_p95"), formatDurationMs(Number(duration.p95 ?? NaN)))}
        ${metricCell(t("tool_runs_metric_p99"), formatDurationMs(Number(duration.p99 ?? NaN)))}
      </div>
    `;
  } else {
    dom.toolRunsSummary.innerHTML = "";
  }

  const runs = Array.isArray(state.toolRuns.page.tool_runs) ? state.toolRuns.page.tool_runs : [];
  if (runs.length === 0) {
    dom.toolRunsContent.innerHTML = `<div class="entry-sub">${escapeHtml(t("tool_runs_empty"))}</div>`;
    return;
  }

  const groups = buildToolRunGroups(runs, state.toolRuns.groupBy);
  dom.toolRunsContent.innerHTML = groups
    .map(([groupKey, groupRuns]) => {
      const rows = groupRuns
        .map((run) => {
          const status = String(run.status || "unknown");
          const statusClass = `status-${normalizeStatus(status)}`;
          const runId = String(run.id || "-");
          const toolName = String(run.tool_name || "-");
          const agentId = String(run.agent_id || "-");
          const durationText = formatDurationMs(toolRunDurationMs(run));
          const createdAt = formatToolRunDetailTimestamp(run.created_at);
          return `
            <div class="tool-run-row">
              <div class="tool-run-row-main">
                <div class="tool-run-row-main-left">
                  <span class="badge ${statusClass}">${escapeHtml(localizeStatus(status))}</span>
                  <span class="tool-run-id">${escapeHtml(runId)}</span>
                  <span class="tool-run-tool">${escapeHtml(toolName)}</span>
                </div>
                <button
                  type="button"
                  class="tool-run-detail-button"
                  data-action="tool-run-detail"
                  data-tool-run-id="${escapeHtml(runId)}"
                >${escapeHtml(t("tool_runs_detail_button"))}</button>
              </div>
              <div class="tool-run-row-meta">
                <span>${escapeHtml(`${t("agents")}: ${agentId}`)}</span>
                <span>${escapeHtml(`${t("tool_runs_duration")}: ${durationText}`)}</span>
                <span>${escapeHtml(createdAt)}</span>
              </div>
            </div>
          `;
        })
        .join("");
      return `
        <section class="tool-run-group">
          <div class="tool-run-group-head">
            <span>${escapeHtml(String(groupKey || "-"))}</span>
            <span class="badge">${escapeHtml(String(groupRuns.length))}</span>
          </div>
          <div class="tool-run-group-body">${rows}</div>
        </section>
      `;
    })
    .join("");
}

async function refreshToolRuns() {
  const sessionId = activeSessionId();
  if (!sessionId) {
    state.toolRuns.page = { tool_runs: [], next_cursor: null };
    state.toolRuns.metrics = null;
    state.toolRuns.error = "";
    state.toolRuns.dirty = false;
    scheduleRender();
    return;
  }
  state.toolRuns.loading = true;
  state.toolRuns.error = "";
  scheduleRender();
  try {
    const params = new URLSearchParams();
    params.set("limit", String(state.toolRuns.limit));
    for (const status of toolRunStatusesForFilter(state.toolRuns.filter)) {
      params.append("status", status);
    }
    const [pagePayload, metricsPayload] = await Promise.all([
      fetchJson(`/api/session/${encodeURIComponent(sessionId)}/tool-runs?${params.toString()}`),
      fetchJson(`/api/session/${encodeURIComponent(sessionId)}/tool-runs/metrics`),
    ]);
    state.toolRuns.page = {
      tool_runs: Array.isArray(pagePayload.tool_runs) ? pagePayload.tool_runs : [],
      next_cursor: pagePayload.next_cursor || null,
    };
    state.toolRuns.metrics = metricsPayload && typeof metricsPayload === "object" ? metricsPayload : null;
    if (state.toolRuns.detail.open && state.toolRuns.detail.runId) {
      updateToolRunDetailSnapshot(state.toolRuns.detail.runId);
      void refreshToolRunDetail();
    }
    state.toolRuns.error = "";
  } catch (error) {
    state.toolRuns.page = { tool_runs: [], next_cursor: null };
    state.toolRuns.metrics = null;
    state.toolRuns.error = String(error.message || "");
  } finally {
    state.toolRuns.loading = false;
    state.toolRuns.dirty = false;
    scheduleRender();
  }
}

function buildSteerRunGroups(runs, groupBy) {
  const grouped = new Map();
  for (const run of runs) {
    if (!run || typeof run !== "object") {
      continue;
    }
    const normalizedGroupBy = String(groupBy || "agent");
    let key = formatSteerRunTargetActor(run);
    if (normalizedGroupBy === "status") {
      key = String(run.status || "-");
    } else if (normalizedGroupBy === "source") {
      key = steerRunSourceGroupKey(run);
    }
    if (!grouped.has(key)) {
      grouped.set(key, []);
    }
    grouped.get(key).push(run);
  }
  return [...grouped.entries()].sort((a, b) => {
    if (b[1].length !== a[1].length) {
      return b[1].length - a[1].length;
    }
    return String(a[0]).localeCompare(String(b[0]));
  });
}

function renderSteerRunsPanel() {
  if (state.activeTab !== "steer-runs") {
    return;
  }
  if (state.steerRuns.dirty && !state.steerRuns.loading) {
    void refreshSteerRuns();
  }

  dom.steerRunsRefreshButton.textContent = t("reload");
  dom.steerRunsFilterButton.textContent = `${t("steer_runs_filter")}: ${steerRunFilterLabel(
    state.steerRuns.filter
  )}`;
  dom.steerRunsGroupButton.textContent = `${t("steer_runs_group")}: ${steerRunGroupLabel(
    state.steerRuns.groupBy
  )}`;
  dom.steerRunsRefreshButton.disabled = state.steerRuns.loading;
  dom.steerRunsFilterButton.disabled = state.steerRuns.loading;
  dom.steerRunsGroupButton.disabled = state.steerRuns.loading;
  if (dom.steerRunsSearchInput) {
    dom.steerRunsSearchInput.disabled = state.steerRuns.loading;
  }

  const sessionId = activeSessionId();
  if (!sessionId) {
    dom.steerRunsStatus.textContent = t("steer_runs_no_session");
    dom.steerRunsSummary.innerHTML = "";
    dom.steerRunsContent.innerHTML = `<div class="entry-sub">${escapeHtml(t("steer_runs_no_session"))}</div>`;
    return;
  }

  if (state.steerRuns.error) {
    dom.steerRunsStatus.textContent = state.steerRuns.error;
  } else if (state.steerRuns.loading) {
    dom.steerRunsStatus.textContent = t("steer_runs_loading");
  } else {
    const allRuns = Array.isArray(state.steerRuns.page.steer_runs)
      ? state.steerRuns.page.steer_runs
      : [];
    const runs = allRuns.filter((run) => matchesSteerRunSearch(run));
    dom.steerRunsStatus.textContent = `${t("steer_runs_count")}: ${runs.length}`;
  }

  const metrics = state.steerRuns.metrics && typeof state.steerRuns.metrics === "object"
    ? state.steerRuns.metrics
    : null;
  if (metrics) {
    const statusCounts = metrics.status_counts && typeof metrics.status_counts === "object"
      ? metrics.status_counts
      : {};
    dom.steerRunsSummary.innerHTML = `
      <div class="steer-runs-metrics-grid">
        ${metricCell(t("steer_runs_metric_total"), metrics.total_runs ?? 0, "steer-runs")}
        ${metricCell(t("steer_runs_metric_waiting"), statusCounts.waiting ?? 0, "steer-runs")}
        ${metricCell(t("steer_runs_metric_completed"), statusCounts.completed ?? 0, "steer-runs")}
        ${metricCell(t("steer_runs_metric_cancelled"), statusCounts.cancelled ?? 0, "steer-runs")}
      </div>
    `;
  } else {
    dom.steerRunsSummary.innerHTML = "";
  }

  const allRuns = Array.isArray(state.steerRuns.page.steer_runs)
    ? state.steerRuns.page.steer_runs
    : [];
  const runs = allRuns.filter((run) => matchesSteerRunSearch(run));
  if (runs.length === 0) {
    dom.steerRunsContent.innerHTML = `<div class="entry-sub">${escapeHtml(t("steer_runs_empty"))}</div>`;
    return;
  }

  const groups = buildSteerRunGroups(runs, state.steerRuns.groupBy);
  dom.steerRunsContent.innerHTML = groups
    .map(([groupKey, groupRuns]) => {
      const rows = groupRuns
        .map((run) => {
          const status = String(run.status || "unknown");
          const statusClass = `status-${normalizeStatus(status)}`;
          const runId = String(run.id || "-");
          const targetActor = formatSteerRunTargetActor(run);
          const createdAt = formatToolRunDetailTimestamp(run.created_at);
          const source = String(run.source || "-");
          const fromActor = formatSteerSourceActor(run);
          const delivery = formatSteerRunDelivery(run);
          const content = String(run.content || "").trim();
          const canCancel = status === "waiting";
          return `
            <div class="steer-run-row">
              <div class="steer-run-row-main">
                <div class="steer-run-row-main-left">
                  <span class="badge ${statusClass}">${escapeHtml(localizeStatus(status))}</span>
                  <span class="steer-run-id">${escapeHtml(runId)}</span>
                </div>
                ${
                  canCancel
                    ? `<button
                  type="button"
                  class="steer-run-cancel-button"
                  data-action="cancel-steer-run"
                  data-steer-run-id="${escapeHtml(runId)}"
                >${escapeHtml(t("steer_runs_cancel_button"))}</button>`
                    : ""
                }
              </div>
              <div class="steer-run-row-grid">
                <div class="steer-run-info-card">
                  ${renderInfoRows([
                    [t("steer_runs_target_agent"), targetActor],
                    [t("steer_runs_source_actor"), fromActor],
                    [t("steer_runs_source_channel"), source],
                  ])}
                </div>
                <div class="steer-run-info-card">
                  ${renderInfoRows([
                    [t("steer_runs_created_at"), createdAt],
                    [t("steer_runs_inserted"), delivery],
                    [t("status"), localizeStatus(status)],
                  ])}
                </div>
              </div>
              <div class="steer-run-content-card">
                <div class="steer-run-content-label">${escapeHtml(t("message"))}</div>
                <div class="steer-run-content-body">${renderMarkdown(content)}</div>
              </div>
            </div>
          `;
        })
        .join("");
      return `
        <section class="steer-run-group">
          <div class="steer-run-group-head">
            <span>${escapeHtml(String(groupKey || "-"))}</span>
            <span class="badge">${escapeHtml(String(groupRuns.length))}</span>
          </div>
          <div class="steer-run-group-body">${rows}</div>
        </section>
      `;
    })
    .join("");
}

async function refreshSteerRuns() {
  const sessionId = activeSessionId();
  if (!sessionId) {
    state.steerRuns.page = { steer_runs: [], next_cursor: null };
    state.steerRuns.metrics = null;
    state.steerRuns.error = "";
    state.steerRuns.dirty = false;
    scheduleRender();
    return;
  }
  state.steerRuns.loading = true;
  state.steerRuns.error = "";
  scheduleRender();
  try {
    const params = new URLSearchParams();
    params.set("limit", String(state.steerRuns.limit));
    for (const status of steerRunStatusesForFilter(state.steerRuns.filter)) {
      params.append("status", status);
    }
    const [pagePayload, metricsPayload] = await Promise.all([
      fetchJson(`/api/session/${encodeURIComponent(sessionId)}/steer-runs?${params.toString()}`),
      fetchJson(`/api/session/${encodeURIComponent(sessionId)}/steer-runs/metrics`),
    ]);
    state.steerRuns.page = {
      steer_runs: Array.isArray(pagePayload.steer_runs) ? pagePayload.steer_runs : [],
      next_cursor: pagePayload.next_cursor || null,
    };
    state.steerRuns.metrics = metricsPayload && typeof metricsPayload === "object" ? metricsPayload : null;
    state.steerRuns.error = "";
  } catch (error) {
    state.steerRuns.page = { steer_runs: [], next_cursor: null };
    state.steerRuns.metrics = null;
    state.steerRuns.error = String(error.message || "");
  } finally {
    state.steerRuns.loading = false;
    state.steerRuns.dirty = false;
    scheduleRender();
  }
}

async function submitSteerForAgent(sessionId, agentId, content) {
  await fetchJson(`/api/session/${encodeURIComponent(sessionId)}/steers`, {
    method: "POST",
    body: JSON.stringify({
      agent_id: agentId,
      content,
      source: "webui",
    }),
  });
}

async function terminateAgentSubtree(sessionId, agentId) {
  return await fetchJson(
    `/api/session/${encodeURIComponent(sessionId)}/agents/${encodeURIComponent(agentId)}/terminate`,
    {
      method: "POST",
      body: JSON.stringify({
        source: "webui",
      }),
    }
  );
}

async function terminateAgentFromCard(agentId) {
  const sessionId = activeSessionId();
  const normalizedAgentId = String(agentId || "").trim();
  if (!sessionId || !normalizedAgentId) {
    state.runtime.status_message = t("error_session_required");
    scheduleRender();
    return;
  }
  try {
    const payload = await terminateAgentSubtree(sessionId, normalizedAgentId);
    const terminatedCount = Array.isArray(payload.terminated_agent_ids)
      ? payload.terminated_agent_ids.length
      : 0;
    const cancelledRunCount = Array.isArray(payload.cancelled_tool_run_ids)
      ? payload.cancelled_tool_run_ids.length
      : 0;
    if (terminatedCount > 0) {
      state.runtime.status_message = `${t("agent_terminate_requested")} (${terminatedCount} agents, ${cancelledRunCount} tool runs)`;
    } else {
      state.runtime.status_message = t("agent_terminate_noop");
    }
    state.toolRuns.dirty = true;
  } catch (error) {
    state.runtime.status_message = `${t("agent_terminate_failed")}: ${String(error.message || "")}`;
  }
  scheduleRender();
}

async function submitSteerFromOverlay() {
  const normalizedAgentId = String(state.steerCompose.agentId || "").trim();
  if (!normalizedAgentId || !state.agents.has(normalizedAgentId)) {
    resetSteerComposeState();
    scheduleRender();
    return;
  }
  const sessionId = activeSessionId();
  if (!sessionId) {
    state.runtime.status_message = t("error_session_required");
    scheduleRender();
    return;
  }
  const content = String(state.steerCompose.content || "").trim();
  if (!content) {
    state.steerCompose.error = t("steer_input_required");
    state.runtime.status_message = t("steer_input_required");
    scheduleRender();
    return;
  }

  state.steerCompose.submitting = true;
  state.steerCompose.error = "";
  scheduleRender();
  try {
    await submitSteerForAgent(sessionId, normalizedAgentId, content);
    state.runtime.status_message = t("steer_submitted");
    state.steerRuns.dirty = true;
    resetSteerComposeState();
    if (state.activeTab === "steer-runs") {
      await refreshSteerRuns();
    }
  } catch (error) {
    const detail = String(error.message || "");
    state.steerCompose.error = detail;
    state.steerCompose.submitting = false;
    state.runtime.status_message = `${t("steer_submit_failed")}: ${detail}`;
  }
  scheduleRender();
}

async function cancelSteerRun(steerRunId) {
  const sessionId = activeSessionId();
  const normalizedRunId = String(steerRunId || "").trim();
  if (!sessionId || !normalizedRunId) {
    return;
  }
  try {
    const payload = await fetchJson(
      `/api/session/${encodeURIComponent(sessionId)}/steer-runs/${encodeURIComponent(normalizedRunId)}/cancel`,
      {
        method: "POST",
        body: "{}",
      }
    );
    const finalStatus = String(payload.final_status || "").trim();
    if (finalStatus === "cancelled") {
      state.runtime.status_message = t("steer_cancelled");
    } else if (finalStatus === "completed") {
      state.runtime.status_message = t("steer_cancel_blocked_completed");
    } else {
      state.runtime.status_message = `${t("steer_cancel_failed")}: ${finalStatus || t("invalid_value")}`;
    }
    state.steerRuns.dirty = true;
    await refreshSteerRuns();
  } catch (error) {
    state.runtime.status_message = `${t("steer_cancel_failed")}: ${String(error.message || "")}`;
  }
  scheduleRender();
}

function renderDiffPanel() {
  if (state.activeTab !== "diff") {
    return;
  }
  if (isDirectWorkspaceMode()) {
    dom.diffStatus.textContent = t("sync_state_disabled");
    dom.diffContent.innerHTML = `<div class="entry-sub">${escapeHtml(t("diff_disabled_direct"))}</div>`;
    return;
  }
  if (state.diff.dirty) {
    void refreshDiff();
    return;
  }
  const preview = state.diff.preview;
  if (!preview) {
    dom.diffContent.innerHTML = `<div class="entry-sub">${escapeHtml(t("diff_no_staged_changes"))}</div>`;
    dom.diffStatus.textContent = state.diff.status || t("sync_state_none");
    return;
  }
  dom.diffStatus.textContent = state.diff.status || String(preview.status || t("sync_state_none"));
  if (!Array.isArray(preview.files) || preview.files.length === 0) {
    dom.diffContent.innerHTML = `<div class="entry-sub">${escapeHtml(t("diff_no_staged_changes"))}</div>`;
    return;
  }

  const summary = `
    <div>
      <div><strong>${escapeHtml(t("session_id"))}:</strong> ${escapeHtml(String(preview.session_id || "-"))}</div>
      <div><strong>${escapeHtml(t("project_dir"))}:</strong> ${escapeHtml(String(preview.project_dir || "-"))}</div>
      <div><strong>${escapeHtml(t("diff_changed_files"))}:</strong> +${preview.added_count || 0}/~${
    preview.modified_count || 0
  }/-${preview.deleted_count || 0}</div>
    </div>
  `;
  const files = preview.files
    .map((file) => {
      const title = `<div class="patch-file">${escapeHtml(file.path || "")} [${escapeHtml(
        String(file.change_type || "modified")
      )}]</div>`;
      if (file.is_binary) {
        return `${title}<div>${escapeHtml(t("diff_binary_modified"))}</div>`;
      }
      const patch = String(file.patch || "");
      const lines = patch
        .split("\n")
        .map((line) => {
          let cls = "patch-line";
          if (line.startsWith("+")) cls += " patch-add";
          else if (line.startsWith("-")) cls += " patch-del";
          else if (line.startsWith("@@")) cls += " patch-hunk";
          return `<div class="${cls}">${escapeHtml(line)}</div>`;
        })
        .join("");
      return `${title}<div>${lines || escapeHtml(t("diff_patch_empty"))}</div>`;
    })
    .join("<hr />");
  const truncated = preview.truncated
    ? `<div class="status-warn">${escapeHtml(t("diff_preview_truncated"))}</div>`
    : "";
  dom.diffContent.innerHTML = `${summary}<hr />${files}${truncated}`;
}

function renderConfigPanel() {
  if (state.activeTab !== "config") {
    return;
  }
  if (!state.configLoaded) {
    void refreshConfig();
    return;
  }

  const lineCount = state.config.text ? state.config.text.split(/\r?\n/).length : 0;
  const byteCount = new TextEncoder().encode(state.config.text || "").length;
  dom.configMeta.innerHTML = `
    ${renderInfoRows([
      [t("config_file_path"), state.config.path || "-"],
      [t("config_lines"), String(lineCount)],
      [t("config_bytes"), String(byteCount)],
      [t("config_state"), state.config.status || t("config_sync_clean")],
    ])}
    <div class="config-notes">
      <div class="config-notes-title">${escapeHtml(t("config_effect_title"))}</div>
      <ul>
        <li>${escapeHtml(t("config_effect_next_session"))}</li>
        <li>${escapeHtml(t("config_effect_running_session"))}</li>
      </ul>
    </div>
  `;

  dom.configSyncStatus.textContent = state.config.status || t("config_sync_clean");
  if (dom.configEditor.value !== state.config.text && !state.config.dirty) {
    dom.configEditor.value = state.config.text;
  }
  dom.configSaveButton.disabled = !state.config.dirty;
}

function renderSetupOverlay() {
  dom.setupOverlay.classList.toggle("hidden", !state.setup.open);
  dom.setupOverlay.setAttribute("aria-hidden", state.setup.open ? "false" : "true");

  const configReady = state.launchConfig.can_run;
  const busy = Boolean(state.setup.busy);

  dom.setupTitle.textContent = t("launch_config_title");
  dom.setupHelp.textContent = t("launch_config_help");
  dom.setupProjectHelp.textContent = t("launch_mode_project_help");
  dom.setupSessionHelp.textContent = t("launch_mode_session_help");

  dom.setupModeProjectButton.textContent = t("setup_mode_project");
  dom.setupModeSessionButton.textContent = t("setup_mode_session");
  dom.setupModeProjectButton.style.borderColor =
    state.setup.mode === "project" ? "var(--accent)" : "var(--border)";
  dom.setupModeSessionButton.style.borderColor =
    state.setup.mode === "session" ? "var(--accent)" : "var(--border)";

  dom.setupProjectSection.classList.toggle("hidden", state.setup.mode !== "project");
  dom.setupSessionSection.classList.toggle("hidden", state.setup.mode !== "session");

  const sandboxBackends = availableSandboxBackends();
  const sandboxBackend = setupDraftSandboxBackend();
  state.setup.sandboxBackendDraft = sandboxBackend;
  dom.sandboxBackendLabel.textContent = t("sandbox_backend_label");
  const sandboxOptionsHtml = sandboxBackends
    .map(
      (backend) =>
        `<option value="${escapeHtml(backend)}">${escapeHtml(localizeSandboxBackend(backend))}</option>`
    )
    .join("");
  if (dom.sandboxBackendSelect.innerHTML !== sandboxOptionsHtml) {
    dom.sandboxBackendSelect.innerHTML = sandboxOptionsHtml;
  }
  dom.sandboxBackendSelect.value = sandboxBackend;
  dom.sandboxBackendSelect.disabled = busy;
  dom.sandboxBackendStatus.innerHTML = `<div>${escapeHtml(
    `${t("sandbox_backend_label")}: ${localizeSandboxBackend(sandboxBackend)}`
  )}</div><div class="entry-sub">${escapeHtml(localizeSandboxBackendDescription(sandboxBackend))}</div>`;

  const currentWorkspaceMode = activeWorkspaceMode();
  const workspaceModeLocked = isSetupWorkspaceModeLocked();
  if (currentWorkspaceMode !== "direct" && isSetupRemoteSource()) {
    state.setup.workspaceSource = "local";
  }
  const workspaceModeSummary = workspaceModeLocked
    ? `${t("workspace_mode_label")}: ${localizeWorkspaceMode(currentWorkspaceMode)} (${t("configuration_ready")})`
    : `${t("workspace_mode_label")}: ${localizeWorkspaceMode(currentWorkspaceMode)}`;
  const workspaceModeDescription = localizeWorkspaceModeDescription(currentWorkspaceMode);
  dom.workspaceModeDirectButton.textContent = t("workspace_mode_direct");
  dom.workspaceModeStagedButton.textContent = t("workspace_mode_staged");
  dom.workspaceModeDirectButton.style.borderColor =
    currentWorkspaceMode === "direct" ? "var(--accent)" : "var(--border)";
  dom.workspaceModeStagedButton.style.borderColor =
    currentWorkspaceMode === "staged" ? "var(--accent)" : "var(--border)";
  dom.workspaceModeDirectButton.disabled = busy || workspaceModeLocked;
  dom.workspaceModeStagedButton.disabled = busy || workspaceModeLocked;
  dom.workspaceModeStatus.innerHTML = `<div>${escapeHtml(workspaceModeSummary)}</div><div class="entry-sub">${escapeHtml(
    workspaceModeDescription
  )}</div>`;
  dom.workspaceSourceLocalButton.textContent = t("workspace_source_local");
  dom.workspaceSourceRemoteButton.textContent = t("workspace_source_remote");
  dom.workspaceSourceLocalButton.style.borderColor =
    !isSetupRemoteSource() ? "var(--accent)" : "var(--border)";
  dom.workspaceSourceRemoteButton.style.borderColor =
    isSetupRemoteSource() ? "var(--accent)" : "var(--border)";
  dom.workspaceSourceLocalButton.disabled = busy;
  dom.workspaceSourceRemoteButton.disabled =
    busy || currentWorkspaceMode !== "direct" || workspaceModeLocked;
  dom.setupLocalSourceSection.classList.toggle("hidden", isSetupRemoteSource());
  dom.setupRemoteSourceSection.classList.toggle("hidden", !isSetupRemoteSource());
  if (dom.sessionWorkspaceMode) {
    dom.sessionWorkspaceMode.textContent = "";
    dom.sessionWorkspaceMode.classList.add("hidden");
  }

  dom.projectPickerButton.textContent = t("choose_project_dir");
  dom.projectPickerButton.disabled = busy;
  dom.remoteTargetLabel.textContent = t("remote_target_label");
  dom.remoteDirLabel.textContent = t("remote_dir_label");
  dom.remoteAuthLabel.textContent = t("remote_auth_label");
  dom.remoteKeyLabel.textContent = t("remote_key_label");
  dom.remotePasswordLabel.textContent = t("remote_password_label");
  dom.remoteKnownHostsLabel.textContent = t("remote_known_hosts_label");
  if (dom.remoteAuthSelect.options.length >= 2) {
    dom.remoteAuthSelect.options[0].textContent = t("remote_auth_key");
    dom.remoteAuthSelect.options[1].textContent = t("remote_auth_password");
  }
  if (dom.remoteKnownHostsSelect.options.length >= 2) {
    dom.remoteKnownHostsSelect.options[0].textContent = t("remote_known_hosts_accept_new");
    dom.remoteKnownHostsSelect.options[1].textContent = t("remote_known_hosts_strict");
  }
  dom.remoteTargetInput.placeholder = t("remote_target_placeholder");
  dom.remoteDirInput.placeholder = t("remote_dir_placeholder");
  dom.remoteKeyInput.placeholder = t("remote_key_placeholder");
  dom.remoteAuthSelect.value =
    String(state.setup.remoteDraft.auth_mode || "key").trim().toLowerCase() === "password"
      ? "password"
      : "key";
  state.setup.remoteDraft.auth_mode = dom.remoteAuthSelect.value;
  if (dom.remoteTargetInput.value !== String(state.setup.remoteDraft.ssh_target || "")) {
    dom.remoteTargetInput.value = String(state.setup.remoteDraft.ssh_target || "");
  }
  if (dom.remoteDirInput.value !== String(state.setup.remoteDraft.remote_dir || "")) {
    dom.remoteDirInput.value = String(state.setup.remoteDraft.remote_dir || "");
  }
  if (dom.remoteKeyInput.value !== String(state.setup.remoteDraft.identity_file || "")) {
    dom.remoteKeyInput.value = String(state.setup.remoteDraft.identity_file || "");
  }
  if (
    dom.remotePasswordInput.value !== String(state.setup.remoteDraft.remote_password || "") &&
    !dom.remotePasswordInput.matches(":focus")
  ) {
    dom.remotePasswordInput.value = String(state.setup.remoteDraft.remote_password || "");
  }
  dom.remoteKnownHostsSelect.value =
    String(state.setup.remoteDraft.known_hosts_policy || "accept_new").trim() === "strict"
      ? "strict"
      : "accept_new";
  state.setup.remoteDraft.known_hosts_policy = dom.remoteKnownHostsSelect.value;
  const remoteSetupActive = state.setup.mode === "project" && isSetupRemoteSource();
  const usingPasswordAuth = dom.remoteAuthSelect.value === "password";
  dom.remoteKeyRow.classList.toggle("hidden", usingPasswordAuth);
  dom.remotePasswordRow.classList.toggle("hidden", !usingPasswordAuth);
  dom.remoteTargetInput.disabled = busy || !remoteSetupActive;
  dom.remoteDirInput.disabled = busy || !remoteSetupActive;
  dom.remoteAuthSelect.disabled = busy || !remoteSetupActive;
  dom.remoteKeyInput.disabled = busy || !remoteSetupActive || usingPasswordAuth;
  dom.remotePasswordInput.disabled = busy || !remoteSetupActive || !usingPasswordAuth;
  dom.remoteKnownHostsSelect.disabled = busy || !remoteSetupActive;
  dom.remoteValidateButton.textContent = t("remote_validate_button");
  dom.remoteValidateButton.disabled = busy || state.setup.remoteValidateBusy || !remoteSetupActive;
  const remoteStatusMessage =
    state.setup.remoteValidateStatus ||
    (isRemoteWorkspaceConfigured() ? `${t("configuration_ready")}: ${remoteWorkspaceLabel()}` : "");
  dom.remoteValidateStatus.textContent = remoteStatusMessage;
  if (state.setup.remoteValidateOk === true) {
    dom.remoteValidateStatus.style.color = "#6ed49b";
  } else if (state.setup.remoteValidateOk === false) {
    dom.remoteValidateStatus.style.color = "#ff6f86";
  } else {
    dom.remoteValidateStatus.style.color = "";
  }
  dom.sessionPickerButton.textContent = t("choose_session_dir");
  dom.sessionPickerButton.disabled = busy;
  dom.sessionRefreshButton.textContent = t("reload");
  dom.sessionRefreshButton.disabled = busy;
  dom.setupCloseButton.textContent = t("cancel");
  dom.setupCloseButton.disabled = !configReady || busy;

  dom.projectCurrentPath.textContent = `${t("selected_project_dir")}: ${
    state.launchConfig.project_dir || t("unset_value")
  }`;
  dom.sessionsRootPath.textContent = `${t("sessions_root")}: ${state.sessionsDir || t("unset_value")}`;
  if (dom.sessionValidateStatus) {
    dom.sessionValidateStatus.textContent =
      state.setup.mode === "session" ? String(state.setup.remoteValidateStatus || "") : "";
    if (state.setup.mode === "session") {
      if (state.setup.remoteValidateOk === true) {
        dom.sessionValidateStatus.style.color = "#6ed49b";
      } else if (state.setup.remoteValidateOk === false) {
        dom.sessionValidateStatus.style.color = "#ff6f86";
      } else {
        dom.sessionValidateStatus.style.color = "";
      }
    } else {
      dom.sessionValidateStatus.style.color = "";
    }
  }

  dom.setupError.textContent = state.setup.error || "";
  dom.sessionDirectoryList.innerHTML = state.setup.sessionEntries
    .map((entry) => {
      const title = `${entry.session_id || "-"}`;
      const meta = `${entry.status || "-"} | ${localizeWorkspaceMode(
        entry.workspace_mode || "staged"
      )} | ${entry.updated_at || "-"}`;
      const workingPath = String(entry.project_dir || "").trim() || t("unset_value");
      const workingPathText = `${t("working_path")}: ${workingPath}`;
      const sessionId = String(entry.session_id || "");
      const loadingThisSession = state.setup.loadingSessionId === sessionId;
      const actionLabel = loadingThisSession ? t("load_context_loading") : t("load_context");
      const actionDisabled = busy ? "disabled" : "";
      return `
        <div class="session-entry">
          <div class="entry-main">
            <div class="entry-name">${escapeHtml(title)}</div>
            <div class="entry-sub">${escapeHtml(meta)}</div>
            <div class="entry-sub" title="${escapeHtml(workingPathText)}">${escapeHtml(
              workingPathText
            )}</div>
          </div>
          <div class="setup-inline">
            <button type="button" data-action="select-session" data-session-id="${escapeHtml(
              sessionId
            )}" ${actionDisabled}>${escapeHtml(actionLabel)}</button>
          </div>
        </div>
      `;
    })
    .join("");
}

function buildRootAgents() {
  const visible = state.agentOrder
    .map((id) => state.agents.get(id))
    .filter((agent) => agent !== undefined);
  const visibleIds = new Set(visible.map((agent) => agent.id));
  return visible.filter((agent) => !agent.parentAgentId || !visibleIds.has(agent.parentAgentId));
}

function sortedChildren(parentAgentId) {
  const order = new Map(state.agentOrder.map((id, index) => [id, index]));
  return [...state.agents.values()]
    .filter((agent) => agent.parentAgentId === parentAgentId)
    .sort((a, b) => (order.get(a.id) || 0) - (order.get(b.id) || 0));
}

function parentAgentName(agentId) {
  const parent = state.agents.get(agentId);
  return parent ? parent.name : agentId;
}

function effectiveStepOrder(agent) {
  const candidates = [];
  if (agent.stepOrder.length > 0) {
    candidates.push(...agent.stepOrder);
  }
  if (agent.stepEntries.size > 0) {
    candidates.push(...agent.stepEntries.keys());
  }
  if (candidates.length === 0) {
    return [];
  }
  const normalized = [...new Set(candidates)]
    .map((value) => Number(value))
    .filter((value) => Number.isFinite(value) && value > 0)
    .map((value) => Math.floor(value))
    .sort((a, b) => a - b);
  const changed =
    normalized.length !== agent.stepOrder.length ||
    normalized.some((step, index) => step !== agent.stepOrder[index]);
  if (changed) {
    agent.stepOrder = normalized;
  }
  return normalized;
}

function entriesForStep(agent, stepNumber) {
  const entries = agent.stepEntries.get(stepNumber);
  return Array.isArray(entries) ? entries : [];
}

function agentStats() {
  const agents = state.agentOrder
    .map((id) => state.agents.get(id))
    .filter((agent) => agent !== undefined);
  const ids = new Set(agents.map((agent) => agent.id));
  const stats = {
    total: agents.length,
    running: 0,
    paused: 0,
    completed: 0,
    failed: 0,
    cancelled: 0,
    terminated: 0,
    pending: 0,
    roots: 0,
    leaves: 0,
    generating: 0,
    edges: 0,
  };

  for (const agent of agents) {
    const status = normalizeStatus(agent.status);
    if (status === "running") stats.running += 1;
    else if (status === "paused") stats.paused += 1;
    else if (status === "completed") stats.completed += 1;
    else if (status === "failed") stats.failed += 1;
    else if (status === "cancelled") stats.cancelled += 1;
    else if (status === "terminated") stats.terminated += 1;
    else stats.pending += 1;

    if (agent.isGenerating) {
      stats.generating += 1;
    }

    if (!agent.parentAgentId || !ids.has(agent.parentAgentId)) {
      stats.roots += 1;
    }
    const children = sortedChildren(agent.id);
    stats.edges += children.length;
    if (children.length === 0) {
      stats.leaves += 1;
    }
  }

  return stats;
}

function normalizeStatus(status) {
  const normalized = String(status || "pending").toLowerCase();
  if (normalized.includes("run")) return "running";
  if (normalized === "waiting") return "waiting";
  if (normalized.includes("pause")) return "paused";
  if (normalized.includes("complete") || normalized.includes("done") || normalized === "success") {
    return "completed";
  }
  if (normalized.includes("cancel")) return "cancelled";
  if (normalized.includes("fail") || normalized.includes("error")) return "failed";
  if (normalized.includes("term") || normalized.includes("interrupt")) return "terminated";
  if (normalized.includes("start") || normalized.includes("resum")) return "running";
  return normalized || "pending";
}

function localizeStatus(status) {
  const normalized = normalizeStatus(status);
  const key = `status_${normalized}`;
  const translated = t(key);
  return translated === key ? normalized : translated;
}

function localizeWorkspaceMode(mode) {
  const normalized = String(mode || "").trim().toLowerCase();
  return normalized === "staged"
    ? t("workspace_mode_staged")
    : t("workspace_mode_direct");
}

function localizeWorkspaceModeDescription(mode) {
  const normalized = String(mode || "").trim().toLowerCase();
  return normalized === "staged"
    ? t("workspace_mode_staged_desc")
    : t("workspace_mode_direct_desc");
}

function localizeSandboxBackend(backend) {
  const normalized = normalizeSandboxBackend(backend, defaultSandboxBackend());
  const key = `sandbox_backend_${normalized}`;
  const translated = t(key);
  return translated === key ? normalized : translated;
}

function localizeSandboxBackendDescription(backend) {
  const normalized = normalizeSandboxBackend(backend, defaultSandboxBackend());
  const key = `sandbox_backend_${normalized}_desc`;
  const translated = t(key);
  return translated === key ? normalized : translated;
}

function statusToEmoji(status) {
  const normalized = normalizeStatus(status);
  if (normalized === "running") return "⚙️";
  if (normalized === "waiting") return "⏳";
  if (normalized === "paused") return "⏸️";
  if (normalized === "completed") return "✅";
  if (normalized === "failed") return "❌";
  if (normalized === "cancelled") return "🟣";
  if (normalized === "terminated") return "🛑";
  return "🧩";
}

function renderInfoRows(rows) {
  return `<div class="info-list">${rows
    .map(([key, value]) => {
      const normalizedValue =
        value === null || value === undefined || value === "" ? "-" : String(value);
      return `
        <div class="info-row">
          <span class="info-key">${escapeHtml(String(key || ""))}</span>
          <span class="info-value">${escapeHtml(normalizedValue)}</span>
        </div>
      `
    })
    .join("")}</div>`;
}

function inlineMarkdown(text) {
  let html = escapeHtml(String(text || ""));
  html = html.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
  );
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  html = html.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  html = html.replace(/_([^_]+)_/g, "<em>$1</em>");
  html = html.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  return html;
}

function renderMarkdown(text) {
  const source = String(text || "").replace(/\r\n/g, "\n");
  if (!source.trim()) {
    return `<div class="markdown-body"><p>${escapeHtml(t("none_value"))}</p></div>`;
  }

  const lines = source.split("\n");
  const parts = [];
  let listItems = [];
  let quoteItems = [];
  let inCodeBlock = false;
  let codeLines = [];

  function flushList() {
    if (listItems.length === 0) {
      return;
    }
    parts.push(`<ul>${listItems.map((item) => `<li>${inlineMarkdown(item)}</li>`).join("")}</ul>`);
    listItems = [];
  }

  function flushQuote() {
    if (quoteItems.length === 0) {
      return;
    }
    parts.push(`<blockquote>${quoteItems.map((item) => `<p>${inlineMarkdown(item)}</p>`).join("")}</blockquote>`);
    quoteItems = [];
  }

  function flushCodeBlock() {
    if (!inCodeBlock) {
      return;
    }
    parts.push(`<pre class="md-code"><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
    inCodeBlock = false;
    codeLines = [];
  }

  for (const rawLine of lines) {
    const line = String(rawLine || "");
    const trimmed = line.trim();

    if (trimmed.startsWith("```")) {
      flushList();
      flushQuote();
      if (!inCodeBlock) {
        inCodeBlock = true;
        codeLines = [];
      } else {
        flushCodeBlock();
      }
      continue;
    }

    if (inCodeBlock) {
      codeLines.push(line);
      continue;
    }

    if (!trimmed) {
      flushList();
      flushQuote();
      continue;
    }

    if (trimmed.startsWith("- ")) {
      flushQuote();
      listItems.push(trimmed.slice(2).trim());
      continue;
    }

    if (trimmed.startsWith("> ")) {
      flushList();
      quoteItems.push(trimmed.slice(2).trim());
      continue;
    }

    flushList();
    flushQuote();

    if (/^###\s+/.test(trimmed)) {
      parts.push(`<h4>${inlineMarkdown(trimmed.replace(/^###\s+/, ""))}</h4>`);
      continue;
    }
    if (/^##\s+/.test(trimmed)) {
      parts.push(`<h3>${inlineMarkdown(trimmed.replace(/^##\s+/, ""))}</h3>`);
      continue;
    }
    if (/^#\s+/.test(trimmed)) {
      parts.push(`<h2>${inlineMarkdown(trimmed.replace(/^#\s+/, ""))}</h2>`);
      continue;
    }

    parts.push(`<p>${inlineMarkdown(line)}</p>`);
  }

  flushList();
  flushQuote();
  flushCodeBlock();
  return `<div class="markdown-body">${parts.join("")}</div>`;
}

function shortText(value, maxLength) {
  const text = String(value || "");
  if (text.length <= maxLength) {
    return text;
  }
  return `${text.slice(0, maxLength - 1)}…`;
}

function shortDisplayText(value, maxUnits) {
  const text = String(value || "");
  const limit = Number.isFinite(maxUnits) ? Math.max(1, Math.floor(maxUnits)) : 24;
  if (!text) {
    return text;
  }
  let used = 0;
  let result = "";
  for (const ch of text) {
    const code = ch.codePointAt(0) || 0;
    const width = code <= 0xff ? 1 : 2;
    if (used + width > limit) {
      const trimmed = result.replace(/\s+$/u, "");
      return `${trimmed || result}…`;
    }
    result += ch;
    used += width;
  }
  return result;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

async function refreshDiff() {
  const sessionId = activeSessionId();
  if (!sessionId) {
    state.diff.status = t("diff_no_session");
    state.diff.preview = null;
    state.diff.dirty = false;
    scheduleRender();
    return;
  }
  if (isDirectWorkspaceMode()) {
    state.diff.status = t("sync_state_disabled");
    state.diff.preview = null;
    state.diff.dirty = false;
    scheduleRender();
    return;
  }
  try {
    const statusPayload = await fetchJson(
      `/api/session/${encodeURIComponent(sessionId)}/project-sync/status`
    );
    state.diff.status = String(statusPayload.status || "none");
    state.diff.preview = await fetchJson(
      `/api/session/${encodeURIComponent(sessionId)}/project-sync/preview`
    );
  } catch (error) {
    state.diff.status = String(error.message || t("diff_preview_failed"));
    state.diff.preview = null;
  } finally {
    state.diff.dirty = false;
    scheduleRender();
  }
}

async function refreshConfig() {
  try {
    const payload = await fetchJson("/api/config");
    if (payload.snapshot && typeof payload.snapshot === "object") {
      applySnapshot(payload.snapshot);
    }
    state.config.path = payload.path || state.config.path;
    state.config.text = String(payload.text || "");
    state.config.mtimeNs = payload.mtime_ns ?? state.config.mtimeNs;
    state.config.status = t("config_sync_clean");
    state.config.dirty = false;
    state.configLoaded = true;
  } catch (error) {
    state.config.status = String(error.message || t("config_load_failed"));
  }
  scheduleRender();
}

async function refreshConfigMeta() {
  if (!state.configLoaded) {
    return;
  }
  try {
    const payload = await fetchJson("/api/config/meta");
    if (payload.snapshot && typeof payload.snapshot === "object") {
      applySnapshot(payload.snapshot);
    }
    const remoteMtime = payload.mtime_ns ?? null;
    if (remoteMtime !== state.config.mtimeNs) {
      if (state.config.dirty) {
        state.config.status = t("config_external_conflict");
      } else {
        await refreshConfig();
        state.config.status = t("config_reloaded_external");
      }
    }
  } catch (error) {
    state.config.status = String(error.message || t("config_load_failed"));
    scheduleRender();
  }
}

async function refreshSessions() {
  try {
    const payload = await fetchJson("/api/sessions");
    state.sessionsDir = payload.sessions_dir || state.sessionsDir;
    state.setup.sessionEntries = Array.isArray(payload.items) ? payload.items : [];
    state.setup.error = "";
  } catch (error) {
    state.setup.error = String(error.message);
  }
  scheduleRender();
}

async function connectWebSocket() {
  if (state.websocket.socket) {
    state.websocket.socket.close();
  }
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${window.location.host}/api/events`);
  state.websocket.socket = socket;
  socket.onopen = () => {
    state.websocket.connected = true;
  };
  socket.onmessage = (event) => {
    let payload = null;
    try {
      payload = JSON.parse(event.data);
    } catch (_error) {
      return;
    }
    if (!payload || typeof payload !== "object") {
      return;
    }
    if (payload.event_type === "runtime_state") {
      const snapshot = payload.payload && payload.payload.snapshot;
      if (snapshot) {
        applySnapshot(snapshot);
        scheduleRender();
      }
      return;
    }
    if (payload.event_type === "event_batch") {
      const events =
        payload.payload && Array.isArray(payload.payload.events) ? payload.payload.events : [];
      for (const record of events) {
        consumeRuntimeEvent(record);
      }
      if (events.length > 0) {
        const lastEvent = events[events.length - 1];
        const eventType = String(lastEvent.event_type || "");
        if (eventType === "session_finalized" || eventType.startsWith("project_sync_")) {
          state.diff.dirty = true;
        }
      }
      scheduleRender();
      return;
    }
    consumeRuntimeEvent(payload);
    scheduleRender();
  };
  socket.onclose = () => {
    state.websocket.connected = false;
    state.websocket.socket = null;
    if (state.websocket.reconnectTimer) {
      window.clearTimeout(state.websocket.reconnectTimer);
    }
    state.websocket.reconnectTimer = window.setTimeout(() => {
      void connectWebSocket();
    }, 1000);
  };
}

function resolveRemotePassword({ allowPrompt }) {
  const remote = remotePayloadFromDraft() || activeRemoteConfig();
  if (!remote || remote.auth_mode !== "password") {
    return "";
  }
  const current = String(state.setup.remoteDraft.remote_password || "").trim();
  if (current) {
    return current;
  }
  if (remote.password_saved) {
    return "";
  }
  if (!allowPrompt) {
    return "";
  }
  const entered = window.prompt(t("remote_password_prompt")) || "";
  const normalized = String(entered).trim();
  state.setup.remoteDraft.remote_password = normalized;
  return normalized;
}

function normalizeRemoteValidateReason(reason) {
  const text = String(reason || "").trim();
  if (!text) {
    return "";
  }
  const prefixes = [
    `${t("remote_validate_failed")}:`,
    `${t("remote_validate_failed")}：`,
    "Remote validation failed:",
    "Remote validation failed：",
    "远程校验失败:",
    "远程校验失败：",
  ];
  for (const prefix of prefixes) {
    if (text.startsWith(prefix)) {
      return String(text.slice(prefix.length)).trim();
    }
  }
  return text;
}

function formatRemoteValidateFailed(reason) {
  const detail = normalizeRemoteValidateReason(reason);
  if (!detail) {
    return t("remote_validate_failed");
  }
  return `${t("remote_validate_failed")}: ${detail}`;
}

async function runSession(task, model, rootAgentName) {
  const sessionIdBefore = activeSessionId();
  const wasRunningBefore = Boolean(state.runtime.running);
  const hadAgentsBefore = state.agents.size > 0;
  const configuredSessionId = String(state.launchConfig.session_id || "").trim();

  // Keep live incremental state when appending a root to an active session.
  // Full resets are only needed when launching a brand-new session.
  if (!configuredSessionId && !wasRunningBefore) {
    resetRuntimeViews();
  }

  const body = {
    task,
    model: String(model || "").trim(),
    sandbox_backend: activeSandboxBackend(),
  };
  const normalizedRootAgentName = String(rootAgentName || "").trim();
  if (normalizedRootAgentName) {
    body.root_agent_name = normalizedRootAgentName;
  }
  const remote = activeRemoteConfig();
  if (remote) {
    const remotePassword = resolveRemotePassword({ allowPrompt: true });
    if (remote.auth_mode === "password" && !remotePassword && !remote.password_saved) {
      throw new Error(t("remote_password_required"));
    }
    if (remotePassword) {
      body.remote_password = remotePassword;
    }
  }
  if (configuredSessionId) {
    body.session_id = configuredSessionId;
    if (remote) {
      body.remote = remote;
    }
  } else if (remote) {
    body.remote = remote;
    body.session_mode = activeWorkspaceMode();
  } else if (state.launchConfig.project_dir) {
    body.project_dir = state.launchConfig.project_dir;
    body.session_mode = activeWorkspaceMode();
  }
  const payload = await fetchJson("/api/run", {
    method: "POST",
    body: JSON.stringify(body),
  });
  applySnapshot(payload);
  const sessionIdAfter = activeSessionId();
  const switchedSession = Boolean(sessionIdAfter && sessionIdAfter !== sessionIdBefore);
  const shouldReloadHistory =
    Boolean(sessionIdAfter) &&
    !wasRunningBefore &&
    (switchedSession || !hadAgentsBefore);
  if (shouldReloadHistory && sessionIdAfter) {
    await loadSessionEvents(sessionIdAfter);
  }
  state.diff.dirty = true;
  state.toolRuns.dirty = true;
  state.steerRuns.dirty = true;
  markAgentPanelDirty();
  scheduleRender();
}

async function persistLaunchConfig({
  projectDir,
  sessionId,
  sessionMode,
  sandboxBackend,
  remote,
  remotePassword,
}) {
  const requestBody = {
    project_dir: projectDir,
    session_id: sessionId,
    session_mode: sessionMode,
    remote: remote || null,
    remote_password: remotePassword || null,
  };
  if (sandboxBackend !== undefined && sandboxBackend !== null) {
    requestBody.sandbox_backend = normalizeSandboxBackend(sandboxBackend, defaultSandboxBackend());
  }
  const payload = await fetchJson("/api/launch-config", {
    method: "POST",
    body: JSON.stringify(requestBody),
  });
  applySnapshot(payload);
  return payload;
}

async function validateRemoteDraft() {
  const remote = remotePayloadFromDraft();
  if (!remote) {
    throw new Error(t("remote_config_incomplete"));
  }
  if (activeWorkspaceMode() !== "direct") {
    throw new Error(t("remote_requires_direct_mode"));
  }
  const remotePassword = resolveRemotePassword({ allowPrompt: true });
  if (remote.auth_mode === "password" && !remotePassword && !remote.password_saved) {
    throw new Error(t("remote_password_required"));
  }
  state.setup.remoteValidateBusy = true;
  state.setup.remoteValidateStatus = t("remote_validate_busy");
  state.setup.remoteValidateOk = null;
  scheduleRender();
  try {
    const payload = await fetchJson("/api/remote/validate", {
      method: "POST",
      body: JSON.stringify({
        remote,
        remote_password: remotePassword || null,
        session_mode: activeWorkspaceMode(),
        sandbox_backend: setupDraftSandboxBackend(),
      }),
    });
    if (payload && payload.ok) {
      state.setup.remoteValidateOk = true;
      const detail = String((payload && payload.stderr) || "").trim();
      state.setup.remoteValidateStatus = detail
        ? `${t("remote_validate_ok")} ${detail}`
        : t("remote_validate_ok");
      return {
        ok: true,
        remote,
        remotePassword,
      };
    } else {
      state.setup.remoteValidateOk = false;
      const reason =
        (payload && (payload.stderr || payload.stdout)) || t("remote_validate_failed");
      state.setup.remoteValidateStatus = formatRemoteValidateFailed(reason);
      return {
        ok: false,
        remote: null,
        remotePassword: "",
      };
    }
  } catch (error) {
    state.setup.remoteValidateOk = false;
    state.setup.remoteValidateStatus = formatRemoteValidateFailed(error.message || "");
    return {
      ok: false,
      remote: null,
      remotePassword: "",
    };
  } finally {
    state.setup.remoteValidateBusy = false;
    scheduleRender();
  }
}

async function applyRemoteDraft({ remote = null, remotePassword = null } = {}) {
  const nextRemote = remote || remotePayloadFromDraft();
  const nextRemotePassword =
    typeof remotePassword === "string"
      ? String(remotePassword).trim()
      : resolveRemotePassword({ allowPrompt: true });
  const remoteDraft = nextRemote;
  if (!remoteDraft) {
    throw new Error(t("remote_config_incomplete"));
  }
  if (activeWorkspaceMode() !== "direct") {
    throw new Error(t("remote_requires_direct_mode"));
  }
  if (remoteDraft.auth_mode === "password" && !nextRemotePassword && !remoteDraft.password_saved) {
    throw new Error(t("remote_password_required"));
  }
  state.setup.busy = true;
  state.setup.error = "";
  scheduleRender();
  try {
    await persistLaunchConfig({
      projectDir: null,
      sessionId: null,
      sessionMode: "direct",
      sandboxBackend: setupDraftSandboxBackend(),
      remote: remoteDraft,
      remotePassword: nextRemotePassword || null,
    });
    state.setup.open = false;
    state.setup.error = "";
    resetRuntimeViews();
    await refreshSessions();
  } finally {
    state.setup.busy = false;
    scheduleRender();
  }
}

async function validateAndApplyRemoteDraft() {
  const result = await validateRemoteDraft();
  if (!result || !result.ok) {
    return;
  }
  await applyRemoteDraft({
    remote: result.remote,
    remotePassword: result.remotePassword,
  });
}

async function chooseProjectDirectory() {
  let succeeded = false;
  state.setup.busy = true;
  state.setup.error = "";
  scheduleRender();
  try {
    const payload = await fetchJson("/api/picker/project", {
      method: "POST",
      body: JSON.stringify({
        session_mode: activeWorkspaceMode(),
        sandbox_backend: setupDraftSandboxBackend(),
      }),
    });
    applySnapshot(payload);
    resetRuntimeViews();
    state.setup.workspaceSource = "local";
    state.setup.remoteValidateOk = null;
    state.setup.remoteValidateStatus = "";
    state.setup.open = false;
    state.setup.error = "";
    succeeded = true;
  } catch (error) {
    state.setup.error = String(error.message || "");
  } finally {
    state.setup.busy = false;
    if (succeeded) {
      await refreshSessions();
    }
    scheduleRender();
  }
}

async function chooseSessionDirectory() {
  let succeeded = false;
  state.setup.busy = true;
  state.setup.error = "";
  state.setup.remoteValidateBusy = true;
  state.setup.remoteValidateOk = null;
  state.setup.remoteValidateStatus = t("remote_validate_busy");
  scheduleRender();
  try {
    const payload = await fetchJson("/api/picker/session", {
      method: "POST",
      body: JSON.stringify({ sandbox_backend: setupDraftSandboxBackend() }),
    });
    applySnapshot(payload);
    const selectedSessionId = payload && payload.launch_config ? payload.launch_config.session_id : null;
    if (selectedSessionId) {
      await loadSessionEvents(selectedSessionId);
    }
    state.setup.open = false;
    state.setup.error = "";
    state.setup.remoteValidateOk = null;
    state.setup.remoteValidateStatus = "";
    succeeded = true;
  } catch (error) {
    state.setup.remoteValidateOk = false;
    state.setup.remoteValidateStatus = formatRemoteValidateFailed(error.message || "");
    state.setup.error = String(error.message || "");
  } finally {
    state.setup.busy = false;
    state.setup.remoteValidateBusy = false;
    if (succeeded) {
      await refreshSessions();
    }
    scheduleRender();
  }
}

function bindEvents() {
  dom.tabButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const nextTab = button.dataset.tab;
      if (!nextTab) {
        return;
      }
      if (nextTab === "diff" && isDirectWorkspaceMode()) {
        state.activeTab = "overview";
        scheduleRender();
        return;
      }
      if (nextTab !== "workflow" && state.workflow.expanded) {
        if (state.workflow.nativeFullscreen || isWorkflowNativeFullscreen()) {
          void exitNativeFullscreen();
        }
        setWorkflowExpanded(false);
      }
      state.activeTab = nextTab;
      if (nextTab === "diff") {
        state.diff.dirty = true;
      } else if (nextTab === "tool-runs") {
        state.toolRuns.dirty = true;
      } else if (nextTab === "steer-runs") {
        state.steerRuns.dirty = true;
      } else if (nextTab === "config" && !state.configLoaded) {
        void refreshConfig();
      } else if (nextTab === "agents") {
        markAgentPanelDirty();
      }
      scheduleRender();
    });
  });

  dom.localeEnButton.addEventListener("click", async () => {
    await switchLocale("en");
  });
  dom.localeZhButton.addEventListener("click", async () => {
    await switchLocale("zh");
  });

  dom.workflowZoomInButton.addEventListener("click", () => {
    const rect = dom.workflowGraph.getBoundingClientRect();
    zoomWorkflow(1.14, rect.width / 2, rect.height / 2);
  });
  dom.workflowZoomOutButton.addEventListener("click", () => {
    const rect = dom.workflowGraph.getBoundingClientRect();
    zoomWorkflow(0.88, rect.width / 2, rect.height / 2);
  });
  dom.workflowZoomResetButton.addEventListener("click", () => {
    resetWorkflowView();
  });
  dom.workflowOriginButton.addEventListener("click", () => {
    resetWorkflowOrigin();
  });
  dom.workflowExpandButton.addEventListener("click", async () => {
    hideWorkflowNodeTooltip();
    if (state.workflow.expanded) {
      if (state.workflow.nativeFullscreen || isWorkflowNativeFullscreen()) {
        await exitNativeFullscreen();
      }
      setWorkflowExpanded(false);
      scheduleRender();
      return;
    }
    const nativeOpened = await requestWorkflowNativeFullscreen();
    state.workflow.nativeFullscreen = nativeOpened;
    setWorkflowExpanded(true);
    scheduleRender();
  });
  dom.workflowGraph.addEventListener(
    "wheel",
    (event) => {
      if (!dom.workflowGraph.querySelector(".workflow-svg")) {
        return;
      }
      event.preventDefault();
      const rect = dom.workflowGraph.getBoundingClientRect();
      const anchorX = event.clientX - rect.left;
      const anchorY = event.clientY - rect.top;
      const factor = event.deltaY < 0 ? 1.12 : 0.9;
      zoomWorkflow(factor, anchorX, anchorY);
    },
    { passive: false }
  );
  dom.workflowGraph.addEventListener("mousedown", (event) => {
    if (!dom.workflowGraph.querySelector(".workflow-svg")) {
      return;
    }
    if (event.button !== 0) {
      return;
    }
    event.preventDefault();
    hideWorkflowNodeTooltip();
    state.workflow.dragging = true;
    state.workflow.lastX = event.clientX;
    state.workflow.lastY = event.clientY;
    dom.workflowGraph.classList.add("dragging");
  });
  dom.workflowGraph.addEventListener("mousemove", (event) => {
    if (state.workflow.dragging) {
      hideWorkflowNodeTooltip();
      return;
    }
    const target = event.target;
    if (!(target instanceof Element)) {
      hideWorkflowNodeTooltip();
      return;
    }
    const node = target.closest(".graph-node[data-node-id]");
    if (!node) {
      hideWorkflowNodeTooltip();
      return;
    }
    const nodeId = node.getAttribute("data-node-id");
    if (!nodeId) {
      hideWorkflowNodeTooltip();
      return;
    }
    showWorkflowNodeTooltip(nodeId, event.clientX, event.clientY);
  });
  dom.workflowGraph.addEventListener("mouseleave", () => {
    hideWorkflowNodeTooltip();
  });
  window.addEventListener("mousemove", (event) => {
    if (!state.workflow.dragging) {
      return;
    }
    const dx = event.clientX - state.workflow.lastX;
    const dy = event.clientY - state.workflow.lastY;
    state.workflow.lastX = event.clientX;
    state.workflow.lastY = event.clientY;
    state.workflow.tx += dx;
    state.workflow.ty += dy;
    applyWorkflowTransform();
  });
  window.addEventListener("mouseup", () => {
    state.workflow.dragging = false;
    dom.workflowGraph.classList.remove("dragging");
  });

  document.addEventListener("fullscreenchange", () => {
    const opened = isWorkflowNativeFullscreen();
    const wasNative = Boolean(state.workflow.nativeFullscreen);
    state.workflow.nativeFullscreen = opened;
    if (opened && !state.workflow.expanded) {
      setWorkflowExpanded(true);
      scheduleRender();
      return;
    }
    if (!opened && wasNative && state.workflow.expanded) {
      setWorkflowExpanded(false);
      scheduleRender();
    }
  });
  document.addEventListener("webkitfullscreenchange", () => {
    const opened = isWorkflowNativeFullscreen();
    const wasNative = Boolean(state.workflow.nativeFullscreen);
    state.workflow.nativeFullscreen = opened;
    if (opened && !state.workflow.expanded) {
      setWorkflowExpanded(true);
      scheduleRender();
      return;
    }
    if (!opened && wasNative && state.workflow.expanded) {
      setWorkflowExpanded(false);
      scheduleRender();
    }
  });

  dom.runButton.addEventListener("click", async () => {
    if (state.runtime.runSubmitting) {
      return;
    }
    if (!state.launchConfig.can_run) {
      resetSetupSandboxBackendDraft();
      state.setup.open = true;
      state.setup.mode = "project";
      state.setup.error = t("error_config_required");
      scheduleRender();
      return;
    }
    state.runtime.runSubmitting = true;
    scheduleRender();
    try {
      await runSession(
        dom.taskInput.value || "",
        dom.modelInput.value || "",
        dom.rootAgentNameInput.value || ""
      );
    } catch (error) {
      state.runtime.status_message = String(error.message || "");
    } finally {
      state.runtime.runSubmitting = false;
      scheduleRender();
    }
  });

  dom.taskInput.addEventListener("input", () => {
    if (
      state.taskInputSeededValue !== null &&
      String(dom.taskInput.value || "") !== state.taskInputSeededValue
    ) {
      state.taskInputSeededValue = null;
    }
    autoSizeTaskInput();
  });

  dom.rootAgentNameInput.addEventListener("input", () => {
    state.runtime.root_agent_name = dom.rootAgentNameInput.value;
  });

  if (dom.agentsRoleFilter) {
    dom.agentsRoleFilter.addEventListener("change", () => {
      state.agentsView.roleFilter = String(dom.agentsRoleFilter.value || "all").trim().toLowerCase();
      state.agentPanel.preserveScrollNextRender = true;
      state.agentPanel.preservedScrollTop = 0;
      state.agentPanel.suppressAutoStickToBottomUntil = performance.now() + 1000;
      markAgentPanelDirty();
      scheduleRender();
    });
  }

  if (dom.agentsSearchInput) {
    dom.agentsSearchInput.addEventListener("input", () => {
      state.agentsView.searchQuery = dom.agentsSearchInput.value;
      state.agentPanel.preserveScrollNextRender = true;
      state.agentPanel.preservedScrollTop = 0;
      state.agentPanel.suppressAutoStickToBottomUntil = performance.now() + 1000;
      markAgentPanelDirty();
      scheduleRender();
    });
  }

  window.addEventListener("resize", () => {
    autoSizeTaskInput();
    markAgentPanelDirty();
    scheduleRender();
  });

  dom.terminalButton.addEventListener("click", async () => {
    const sessionId = activeSessionId();
    if (!sessionId) {
      state.runtime.status_message = t("error_session_required");
      scheduleRender();
      return;
    }
    try {
      const body = { session_id: sessionId };
      const remote = activeRemoteConfig();
      if (remote && remote.auth_mode === "password") {
        const remotePassword = resolveRemotePassword({ allowPrompt: true });
        if (!remotePassword && !remote.password_saved) {
          throw new Error(t("remote_password_required"));
        }
        if (remotePassword) {
          body.remote_password = remotePassword;
        }
      }
      const payload = await fetchJson("/api/terminal/open", {
        method: "POST",
        body: JSON.stringify(body),
      });
      const workspaceRoot = String(payload.workspace_root || "").trim();
      state.runtime.status_message = workspaceRoot
        ? `${t("terminal_opened")}: ${workspaceRoot}`
        : t("terminal_opened");
    } catch (error) {
      state.runtime.status_message = String(error.message || "");
    }
    scheduleRender();
  });

  dom.interruptButton.addEventListener("click", async () => {
    try {
      const payload = await fetchJson("/api/interrupt", {
        method: "POST",
        body: "{}",
      });
      applySnapshot(payload);
      scheduleRender();
    } catch (error) {
      state.runtime.status_message = String(error.message || "");
      scheduleRender();
    }
  });

  dom.setupButton.addEventListener("click", () => {
    resetSetupSandboxBackendDraft();
    state.setup.open = true;
    state.setup.error = "";
    state.setup.remoteValidateBusy = false;
    state.setup.remoteValidateOk = null;
    state.setup.remoteValidateStatus = "";
    syncRemoteDraftFromLaunch();
    if (!isRemoteWorkspaceConfigured() && !state.setup.workspaceSource) {
      state.setup.workspaceSource = "local";
    }
    void refreshSessions();
    scheduleRender();
  });

  dom.setupModeProjectButton.addEventListener("click", () => {
    state.setup.mode = "project";
    state.setup.error = "";
    state.setup.remoteValidateOk = null;
    state.setup.remoteValidateStatus = "";
    if (isRemoteWorkspaceConfigured()) {
      state.setup.workspaceSource = "remote";
    }
    scheduleRender();
  });
  dom.setupModeSessionButton.addEventListener("click", () => {
    state.setup.mode = "session";
    state.setup.error = "";
    state.setup.remoteValidateOk = null;
    state.setup.remoteValidateStatus = "";
    scheduleRender();
  });

  dom.sandboxBackendSelect.addEventListener("change", () => {
    state.setup.sandboxBackendDraft = normalizeSandboxBackend(
      dom.sandboxBackendSelect.value,
      defaultSandboxBackend()
    );
    state.setup.error = "";
    scheduleRender();
  });

  dom.projectPickerButton.addEventListener("click", () => {
    void chooseProjectDirectory();
  });

  dom.workspaceSourceLocalButton.addEventListener("click", () => {
    state.setup.workspaceSource = "local";
    state.setup.error = "";
    scheduleRender();
  });

  dom.workspaceSourceRemoteButton.addEventListener("click", () => {
    if (activeWorkspaceMode() !== "direct") {
      state.setup.error = t("remote_requires_direct_mode");
      scheduleRender();
      return;
    }
    state.setup.workspaceSource = "remote";
    state.setup.error = "";
    syncRemoteDraftFromLaunch();
    scheduleRender();
  });

  dom.remoteTargetInput.addEventListener("input", () => {
    state.setup.remoteDraft.ssh_target = dom.remoteTargetInput.value;
    state.setup.remoteDraft.password_saved = false;
    state.setup.remoteValidateOk = null;
    state.setup.remoteValidateStatus = "";
  });
  dom.remoteDirInput.addEventListener("input", () => {
    state.setup.remoteDraft.remote_dir = dom.remoteDirInput.value;
    state.setup.remoteDraft.password_saved = false;
    state.setup.remoteValidateOk = null;
    state.setup.remoteValidateStatus = "";
  });
  dom.remoteAuthSelect.addEventListener("change", () => {
    state.setup.remoteDraft.auth_mode =
      String(dom.remoteAuthSelect.value || "key").trim().toLowerCase() === "password"
        ? "password"
        : "key";
    state.setup.remoteDraft.password_saved = false;
    state.setup.remoteValidateOk = null;
    state.setup.remoteValidateStatus = "";
    scheduleRender();
  });
  dom.remoteKeyInput.addEventListener("input", () => {
    state.setup.remoteDraft.identity_file = dom.remoteKeyInput.value;
    state.setup.remoteDraft.password_saved = false;
    state.setup.remoteValidateOk = null;
    state.setup.remoteValidateStatus = "";
  });
  dom.remotePasswordInput.addEventListener("input", () => {
    state.setup.remoteDraft.remote_password = dom.remotePasswordInput.value;
    state.setup.remoteDraft.password_saved = false;
    state.setup.remoteValidateOk = null;
    state.setup.remoteValidateStatus = "";
  });
  dom.remoteKnownHostsSelect.addEventListener("change", () => {
    state.setup.remoteDraft.known_hosts_policy =
      String(dom.remoteKnownHostsSelect.value || "accept_new").trim() === "strict"
        ? "strict"
        : "accept_new";
    state.setup.remoteDraft.password_saved = false;
    state.setup.remoteValidateOk = null;
    state.setup.remoteValidateStatus = "";
  });
  dom.remoteValidateButton.addEventListener("click", async () => {
    try {
      await validateAndApplyRemoteDraft();
    } catch (error) {
      state.setup.remoteValidateOk = false;
      state.setup.remoteValidateStatus = formatRemoteValidateFailed(error.message || "");
      state.setup.error = String(error.message || "");
      scheduleRender();
    }
  });

  dom.workspaceModeDirectButton.addEventListener("click", async () => {
    if (isSetupWorkspaceModeLocked()) {
      return;
    }
    state.setup.busy = true;
    state.setup.error = "";
    scheduleRender();
    try {
      const remote = activeRemoteConfig();
      const remotePassword =
        remote && remote.auth_mode === "password"
          ? String(state.setup.remoteDraft.remote_password || "").trim()
          : "";
      await persistLaunchConfig({
        projectDir: state.launchConfig.project_dir,
        sessionId: null,
        sessionMode: "direct",
        remote,
        remotePassword: remotePassword || null,
      });
    } catch (error) {
      state.setup.error = String(error.message || "");
    } finally {
      state.setup.busy = false;
      scheduleRender();
    }
  });

  dom.workspaceModeStagedButton.addEventListener("click", async () => {
    if (isSetupWorkspaceModeLocked()) {
      return;
    }
    state.setup.busy = true;
    state.setup.error = "";
    scheduleRender();
    try {
      await persistLaunchConfig({
        projectDir: state.launchConfig.project_dir,
        sessionId: null,
        sessionMode: "staged",
        remote: null,
        remotePassword: null,
      });
      state.setup.workspaceSource = "local";
    } catch (error) {
      state.setup.error = String(error.message || "");
    } finally {
      state.setup.busy = false;
      scheduleRender();
    }
  });

  dom.sessionPickerButton.addEventListener("click", () => {
    void chooseSessionDirectory();
  });

  dom.sessionDirectoryList.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) {
      return;
    }
    const action = button.getAttribute("data-action");
    const sessionId = button.getAttribute("data-session-id");
    if (!sessionId) {
      return;
    }

    if (action === "select-session") {
      if (state.setup.busy) {
        return;
      }
      state.setup.busy = true;
      state.setup.loadingSessionId = sessionId;
      state.setup.error = "";
      state.setup.remoteValidateBusy = true;
      state.setup.remoteValidateOk = null;
      state.setup.remoteValidateStatus = t("remote_validate_busy");
      scheduleRender();
      try {
        const payload = await persistLaunchConfig({
          projectDir: null,
          sessionId,
          sessionMode: undefined,
          sandboxBackend: setupDraftSandboxBackend(),
          remote: null,
          remotePassword: null,
        });
        const loadedSessionId =
          (payload &&
            payload.launch_config &&
            typeof payload.launch_config.session_id === "string" &&
            payload.launch_config.session_id) ||
          activeSessionId() ||
          sessionId;
        await loadSessionEvents(loadedSessionId);
        syncRemoteDraftFromLaunch();
        state.setup.open = false;
        state.setup.error = "";
        state.setup.remoteValidateOk = null;
        state.setup.remoteValidateStatus = "";
      } catch (error) {
        const reason = String(error.message || "");
        state.setup.remoteValidateOk = false;
        state.setup.remoteValidateStatus = formatRemoteValidateFailed(reason);
        state.setup.error = reason;
      } finally {
        state.setup.busy = false;
        state.setup.loadingSessionId = null;
        state.setup.remoteValidateBusy = false;
      }
      scheduleRender();
    }
  });

  dom.sessionRefreshButton.addEventListener("click", () => {
    void refreshSessions();
  });

  const onStepGroupToggle = (event) => {
    const target = event.target;
    if (!(target instanceof Element)) {
      return;
    }
    if (!target.matches("details.step-group[data-agent-id][data-step]")) {
      return;
    }
    const agentId = target.getAttribute("data-agent-id");
    const stepRaw = Number(target.getAttribute("data-step"));
    if (!agentId || !Number.isFinite(stepRaw)) {
      return;
    }
    if (event.currentTarget === dom.agentsLive) {
      const preservedTop = dom.agentsLive.scrollTop;
      state.agentPanel.preserveScrollNextRender = true;
      state.agentPanel.preservedScrollTop = preservedTop;
      state.agentPanel.suppressAutoStickToBottomUntil = performance.now() + 1200;
      window.requestAnimationFrame(() => {
        dom.agentsLive.scrollTop = preservedTop;
      });
    }
    setStepExpanded(agentId, Math.floor(stepRaw), target.hasAttribute("open"));
  };
  dom.agentsLive.addEventListener("toggle", onStepGroupToggle, true);
  dom.agentFocusBody.addEventListener("toggle", onStepGroupToggle, true);

  dom.agentsLive.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) {
      return;
    }
    const action = button.getAttribute("data-action");
    if (action === "copy-agent-id") {
      const value = button.getAttribute("data-copy-value") || "";
      void copyAgentField(value, "copied_agent_id");
      return;
    }
    if (action === "copy-agent-name") {
      const value = button.getAttribute("data-copy-value") || "";
      void copyAgentField(value, "copied_agent_name");
      return;
    }
    if (action === "focus-agent") {
      const agentId = button.getAttribute("data-agent-id");
      if (!agentId || !state.agents.has(agentId)) {
        return;
      }
      state.agentFocus.open = true;
      state.agentFocus.agentId = agentId;
      scheduleRender();
      return;
    }
    if (action === "steer-agent") {
      const agentId = button.getAttribute("data-agent-id");
      if (!agentId || !state.agents.has(agentId)) {
        return;
      }
      openSteerComposeOverlay(agentId);
      return;
    }
    if (action === "terminate-agent") {
      const agentId = button.getAttribute("data-agent-id");
      if (!agentId || !state.agents.has(agentId)) {
        return;
      }
      void terminateAgentFromCard(agentId);
      return;
    }
    if (action === "jump-agent") {
      const agentId = button.getAttribute("data-agent-id");
      if (!agentId || !state.agents.has(agentId)) {
        return;
      }
      jumpToAgentCard(agentId);
    }
  });
  dom.agentFocusCloseButton.addEventListener("click", () => {
    state.agentFocus.open = false;
    state.agentFocus.agentId = null;
    scheduleRender();
  });
  dom.agentFocusOverlay.addEventListener("click", (event) => {
    if (event.target !== dom.agentFocusOverlay) {
      return;
    }
    state.agentFocus.open = false;
    state.agentFocus.agentId = null;
    scheduleRender();
  });

  dom.steerComposeInput.addEventListener("input", () => {
    state.steerCompose.content = dom.steerComposeInput.value;
    if (state.steerCompose.error) {
      state.steerCompose.error = "";
      scheduleRender();
    }
  });

  dom.steerComposeInput.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") {
      return;
    }
    if (!event.ctrlKey && !event.metaKey) {
      return;
    }
    event.preventDefault();
    void submitSteerFromOverlay();
  });

  dom.steerComposeCancelButton.addEventListener("click", () => {
    closeSteerComposeOverlay({ cancelled: true });
  });

  dom.steerComposeSubmitButton.addEventListener("click", () => {
    void submitSteerFromOverlay();
  });

  dom.steerComposeOverlay.addEventListener("click", (event) => {
    if (event.target !== dom.steerComposeOverlay) {
      return;
    }
    closeSteerComposeOverlay({ cancelled: true });
  });

  dom.toolRunDetailCloseButton.addEventListener("click", () => {
    closeToolRunDetail();
    scheduleRender();
  });
  dom.toolRunDetailOverlay.addEventListener("click", (event) => {
    if (event.target !== dom.toolRunDetailOverlay) {
      return;
    }
    closeToolRunDetail();
    scheduleRender();
  });

  window.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") {
      return;
    }
    if (state.toolRuns.detail.open) {
      closeToolRunDetail();
      scheduleRender();
      return;
    }
    if (state.agentFocus.open) {
      state.agentFocus.open = false;
      state.agentFocus.agentId = null;
      scheduleRender();
      return;
    }
    if (state.steerCompose.open) {
      closeSteerComposeOverlay({ cancelled: true });
      return;
    }
    if (state.workflow.expanded) {
      if (state.workflow.nativeFullscreen || isWorkflowNativeFullscreen()) {
        void exitNativeFullscreen();
      }
      setWorkflowExpanded(false);
      scheduleRender();
      return;
    }
    if (state.setup.open && state.launchConfig.can_run) {
      state.setup.open = false;
      state.setup.error = "";
      scheduleRender();
    }
  });

  dom.setupCloseButton.addEventListener("click", () => {
    state.setup.open = false;
    state.setup.error = "";
    scheduleRender();
  });

  dom.applyButton.addEventListener("click", async () => {
    if (isDirectWorkspaceMode()) {
      state.runtime.status_message = t("diff_disabled_direct");
      scheduleRender();
      return;
    }
    const sessionId = activeSessionId();
    if (!sessionId) {
      state.runtime.status_message = t("error_session_required");
      scheduleRender();
      return;
    }
    try {
      await fetchJson(`/api/session/${encodeURIComponent(sessionId)}/project-sync/apply`, {
        method: "POST",
        body: "{}",
      });
      state.diff.dirty = true;
      state.runtime.status_message = t("sync_apply_done");
    } catch (error) {
      state.runtime.status_message = String(error.message || "");
    }
    scheduleRender();
  });

  dom.undoButton.addEventListener("click", async () => {
    if (isDirectWorkspaceMode()) {
      state.runtime.status_message = t("diff_disabled_direct");
      scheduleRender();
      return;
    }
    const sessionId = activeSessionId();
    if (!sessionId) {
      state.runtime.status_message = t("error_session_required");
      scheduleRender();
      return;
    }
    try {
      await fetchJson(`/api/session/${encodeURIComponent(sessionId)}/project-sync/undo`, {
        method: "POST",
        body: "{}",
      });
      state.diff.dirty = true;
      state.runtime.status_message = t("sync_undo_done");
    } catch (error) {
      state.runtime.status_message = String(error.message || "");
    }
    scheduleRender();
  });

  dom.toolRunsRefreshButton.addEventListener("click", async () => {
    state.toolRuns.dirty = true;
    await refreshToolRuns();
  });

  dom.toolRunsFilterButton.addEventListener("click", async () => {
    cycleToolRunFilter();
    await refreshToolRuns();
  });

  dom.toolRunsGroupButton.addEventListener("click", () => {
    cycleToolRunGroupBy();
    scheduleRender();
  });
  dom.toolRunsContent.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action='tool-run-detail'][data-tool-run-id]");
    if (!button) {
      return;
    }
    const toolRunId = button.getAttribute("data-tool-run-id");
    if (!toolRunId) {
      return;
    }
    openToolRunDetail(toolRunId);
    scheduleRender();
  });

  dom.steerRunsRefreshButton.addEventListener("click", async () => {
    state.steerRuns.dirty = true;
    await refreshSteerRuns();
  });

  dom.steerRunsFilterButton.addEventListener("click", async () => {
    cycleSteerRunFilter();
    await refreshSteerRuns();
  });

  dom.steerRunsGroupButton.addEventListener("click", () => {
    cycleSteerRunGroupBy();
    scheduleRender();
  });

  if (dom.steerRunsSearchInput) {
    dom.steerRunsSearchInput.addEventListener("input", () => {
      state.steerRuns.searchQuery = dom.steerRunsSearchInput.value;
      scheduleRender();
    });
  }

  dom.steerRunsContent.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action='cancel-steer-run'][data-steer-run-id]");
    if (!button) {
      return;
    }
    const steerRunId = button.getAttribute("data-steer-run-id");
    if (!steerRunId) {
      return;
    }
    void cancelSteerRun(steerRunId);
  });

  dom.configEditor.addEventListener("input", () => {
    state.config.text = dom.configEditor.value;
    state.config.dirty = true;
    state.config.status = t("config_unsaved_changes");
    scheduleRender();
  });

  dom.configSaveButton.addEventListener("click", async () => {
    try {
      const payload = await fetchJson("/api/config/save", {
        method: "POST",
        body: JSON.stringify({ text: dom.configEditor.value }),
      });
      const verify = await fetchJson("/api/config/reload", {
        method: "POST",
        body: "{}",
      });
      if (payload.snapshot && typeof payload.snapshot === "object") {
        applySnapshot(payload.snapshot);
      }
      if (verify.snapshot && typeof verify.snapshot === "object") {
        applySnapshot(verify.snapshot);
      }
      state.config.text = String(verify.text || payload.text || "");
      state.config.path = verify.path || payload.path || state.config.path;
      state.config.mtimeNs = verify.mtime_ns ?? payload.mtime_ns ?? state.config.mtimeNs;
      state.config.dirty = false;
      if (String(verify.text || "") !== String(payload.text || "")) {
        state.config.status = t("config_saved_with_reload_note");
      } else {
        state.config.status = `${t("config_saved")}: ${state.config.path}`;
      }
    } catch (error) {
      state.config.status = String(error.message || "");
    }
    scheduleRender();
  });

  dom.configReloadButton.addEventListener("click", async () => {
    await refreshConfig();
    state.config.status = t("config_reloaded");
    scheduleRender();
  });
}

async function switchLocale(locale) {
  try {
    const payload = await fetchJson("/api/locale", {
      method: "POST",
      body: JSON.stringify({ locale }),
    });
    applySnapshot(payload);
    state.setup.remoteValidateOk = null;
    state.setup.remoteValidateStatus = "";
    state.config.status = "";
    markAgentPanelDirty();
    markWorkflowGraphDirty();
    scheduleRender();
  } catch (error) {
    state.runtime.status_message = String(error.message || "");
    scheduleRender();
  }
}

function startTimers() {
  window.setInterval(() => {
    void refreshConfigMeta();
    void refreshToolRunDetail();
  }, 1000);
}

async function main() {
  bindEvents();
  await bootstrap();
  await connectWebSocket();
  startTimers();
}

void main();
