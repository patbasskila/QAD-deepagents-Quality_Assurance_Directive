(function () {
  const form = document.getElementById("upload-form");
  const submitBtn = document.getElementById("submit-btn");

  const statusBox = document.getElementById("statusBox");
  const jobIdEl = document.getElementById("jobId");
  const jobStatusEl = document.getElementById("jobStatus");
  const jobMsgEl = document.getElementById("jobMsg");

  const downloadsEl = document.getElementById("downloads");

  // HITL review controls
  const reviewPanelEl = document.getElementById("reviewPanel");
  const reviewFeedbackEl = document.getElementById("reviewFeedback");
  const btnApproveEl = document.getElementById("btnApprove");
  const btnRejectRerunEl = document.getElementById("btnRejectRerun");
  const reviewMsgEl = document.getElementById("reviewMsg");

  // matches index.html id="debugToggleRow"
  const debugRowEl = document.getElementById("debugToggleRow");
  const debugToggleEl = document.getElementById("debugToggle");

  // DeepAgents pretty panel (6.10)
  const deepagentsPanelEl = document.getElementById("deepagentsPanel");
  const deepagentsBadgeEl = document.getElementById("deepagentsBadge");
  const deepagentsSummaryEl = document.getElementById("deepagentsSummary");
  const deepagentsGridEl = document.getElementById("deepagentsGrid");
  const deepagentsNoteEl = document.getElementById("deepagentsNote");

  const daPresetEl = document.getElementById("daPreset");
  const daPlannerEl = document.getElementById("daPlanner");
  const daRepairEl = document.getElementById("daRepair");
  const daQualityEl = document.getElementById("daQuality");
  const daAreasCapEl = document.getElementById("daAreasCap");
  const daTempEl = document.getElementById("daTemp");
  const daExportEl = document.getElementById("daExport");

  let pollTimer = null;

  let allowDebugDownloads = false; // controlled by backend /ui-config
  let lastCompletedJobId = null;

  // Cache DeepAgents run config per job (avoid repeated downloads)
  const deepagentsRunConfigCache = new Map(); // jobId -> object

  function setStatusVisible(visible) {
    statusBox.style.display = visible ? "block" : "none";
  }

  function setDownloadsVisible(visible) {
    downloadsEl.style.display = visible ? "flex" : "none";
  }

  function setDebugRowVisible(visible) {
    if (!debugRowEl) return;
    debugRowEl.style.display = visible ? "block" : "none";
  }

  function setBoxStyle(kind) {
    statusBox.classList.remove("error", "success");
    if (kind === "error") statusBox.classList.add("error");
    if (kind === "success") statusBox.classList.add("success");
  }

  function clearDownloads() {
    downloadsEl.innerHTML = "";
  }

  function addDownloadLink(jobId, key, label) {
    const a = document.createElement("a");
    a.href = `/download/${encodeURIComponent(jobId)}/${encodeURIComponent(key)}`;
    a.textContent = label;
    a.target = "_blank";
    downloadsEl.appendChild(a);
  }

  function renderDownloads(jobId) {
    clearDownloads();

    // Always-visible user outputs
    addDownloadLink(jobId, "qad_csv", "Download CSV");
    addDownloadLink(jobId, "qad_excel", "Download Excel");

    // Debug-only outputs
    const showDebug = allowDebugDownloads && debugToggleEl && debugToggleEl.checked;
    if (!showDebug) {
      setDownloadsVisible(true);
      return;
    }

    // Core debug artifacts
    addDownloadLink(jobId, "qad_json", "Download QAD JSON");
    addDownloadLink(jobId, "contract_text", "Download Contract Text (txt)");
    addDownloadLink(jobId, "document_blocks", "Download Document Blocks (json)");
    addDownloadLink(jobId, "ingest_report", "Download Ingest Report (json)");
    addDownloadLink(jobId, "chunks", "Download Chunks (json)");
    addDownloadLink(jobId, "chunk_meta", "Download Chunk Meta (json)");
    addDownloadLink(jobId, "faiss_index", "Download FAISS Index (index)");

    // Separator
    const sep = document.createElement("div");
    sep.style.flexBasis = "100%";
    sep.style.height = "0";
    downloadsEl.appendChild(sep);

    // DeepAgents artifacts
    addDownloadLink(jobId, "agents_run_config", "Download Agent Run Config (json)");
    addDownloadLink(jobId, "agents_plan", "Download Agent Plan (json)");
    addDownloadLink(jobId, "agents_draft", "Download Agent Draft (json)");
    addDownloadLink(jobId, "agents_normalized", "Download Agent Normalized Checks (json)");
    addDownloadLink(jobId, "agents_validation", "Download Agent Validation Report (json)");
    addDownloadLink(jobId, "agents_final", "Download Agent Final Checks (json)");
    addDownloadLink(jobId, "agents_merged", "Download Agent Merged Drafts (json)");
    addDownloadLink(jobId, "agents_dedupe_report", "Download Agent Dedupe Report (json)");
    addDownloadLink(jobId, "agents_planner_raw", "Download Planner Raw (json)");
    addDownloadLink(jobId, "agents_repair_attempt", "Download Repair Attempt (json)");
    addDownloadLink(jobId, "agents_repair_report", "Download Repair Report (json)");
    addDownloadLink(jobId, "agents_quality_report", "Download Quality Report (json)");
    addDownloadLink(jobId, "agents_quality_summary", "Download Quality Summary (json)");
    addDownloadLink(jobId, "agents_export_policy", "Download Export Policy (json)");
    addDownloadLink(jobId, "agents_export_summary", "Download Export Summary (json)");
    addDownloadLink(jobId, "agents_export_ready_checks", "Download Export Ready Checks (json)");
    addDownloadLink(jobId, "agents_export_used", "Download Export Used Report (json)");

    setDownloadsVisible(true);
  }

  function renderEmailLine(email_status, email_error) {
    if (!email_status) return "";
    if (email_status === "sent") return "Email: ✅ sent";
    if (email_status === "disabled") return "Email: ⛔ disabled";
    if (email_status === "failed") return `Email: ❌ failed${email_error ? " — " + email_error : ""}`;
    return `Email: ${email_status}`;
  }

  function setReviewVisible(visible) {
    if (!reviewPanelEl) return;
    reviewPanelEl.style.display = visible ? "block" : "none";
    if (!visible && reviewMsgEl) reviewMsgEl.textContent = "";
  }

  async function submitReview(jobId, decision, feedback) {
    const resp = await fetch(`/jobs/${encodeURIComponent(jobId)}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision, feedback: feedback || null }),
    });
    if (!resp.ok) {
      const t = await resp.text();
      throw new Error(`Review HTTP ${resp.status}: ${t}`);
    }
    return resp.json();
  }

  async function rerunJob(jobId, feedback) {
    const resp = await fetch(`/jobs/${encodeURIComponent(jobId)}/rerun`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ feedback: feedback || null }),
    });
    if (!resp.ok) {
      const t = await resp.text();
      throw new Error(`Rerun HTTP ${resp.status}: ${t}`);
    }
    return resp.json();
  }

  function _boolWord(v) {
    return v ? "on" : "off";
  }

  function _humanToggle(v) {
    return v ? "Enabled" : "Disabled";
  }

  function _setBadge(kind, text) {
    if (!deepagentsBadgeEl) return;
    deepagentsBadgeEl.classList.remove("ok", "warn", "off");
    if (kind) deepagentsBadgeEl.classList.add(kind);
    deepagentsBadgeEl.textContent = text;
  }

  function hideDeepAgentsPanel() {
    if (!deepagentsPanelEl) return;
    deepagentsPanelEl.style.display = "none";
  }

  function showDeepAgentsPanel() {
    if (!deepagentsPanelEl) return;
    deepagentsPanelEl.style.display = "block";
  }

  function renderDeepAgentsPanelState({
    active,
    badgeKind,
    badgeText,
    summaryText,
    gridVisible,
    noteText,
    preset,
    planner,
    repair,
    quality,
    areasCap,
    temperature,
    exportText,
  }) {
    if (!deepagentsPanelEl) return;

    if (!active) {
      hideDeepAgentsPanel();
      return;
    }

    showDeepAgentsPanel();
    _setBadge(badgeKind || "off", badgeText || "inactive");

    if (deepagentsSummaryEl) deepagentsSummaryEl.textContent = summaryText || "";

    if (deepagentsGridEl) deepagentsGridEl.style.display = gridVisible ? "grid" : "none";

    if (deepagentsNoteEl) {
      if (noteText) {
        deepagentsNoteEl.style.display = "block";
        deepagentsNoteEl.textContent = noteText;
      } else {
        deepagentsNoteEl.style.display = "none";
        deepagentsNoteEl.textContent = "";
      }
    }

    if (daPresetEl) daPresetEl.textContent = preset ?? "—";
    if (daPlannerEl) daPlannerEl.textContent = planner ?? "—";
    if (daRepairEl) daRepairEl.textContent = repair ?? "—";
    if (daQualityEl) daQualityEl.textContent = quality ?? "—";
    if (daAreasCapEl) daAreasCapEl.textContent = areasCap ?? "—";
    if (daTempEl) daTempEl.textContent = temperature ?? "—";
    if (daExportEl) daExportEl.textContent = exportText ?? "—";
  }

  function extractDeepAgentsValues(cfg) {
    if (!cfg || typeof cfg !== "object") return null;

    const preset = cfg.deepagents_preset || cfg.preset || cfg.PRESET || "custom";

    const plannerEnabled =
      cfg.deepagents_planner_enabled ?? cfg.planner_enabled ?? cfg.plannerEnabled ?? false;
    const repairEnabled =
      cfg.deepagents_repair_enabled ?? cfg.repair_enabled ?? cfg.repairEnabled ?? false;
    const qualityEnabled =
      cfg.deepagents_quality_enabled ?? cfg.quality_enabled ?? cfg.qualityEnabled ?? false;

    const areasCap =
      cfg.deepagents_areas_cap ?? cfg.areas_cap ?? cfg.areasCap ?? null;

    const temperature =
      cfg.deepagents_temperature ?? cfg.temperature ?? null;

    const ep = cfg.export_policy || cfg.deepagents_export_policy || {};
    const dropBad = !!(ep.drop_bad ?? cfg.deepagents_export_drop_bad);
    const dropEmpty = !!(ep.drop_empty ?? cfg.deepagents_export_drop_empty);
    const minScore = ep.min_score ?? cfg.deepagents_export_min_score;
    const maxChecks = ep.max_checks ?? cfg.deepagents_export_max_checks;
    const sortDesc = ep.sort_by_score_desc ?? cfg.deepagents_export_sort_desc;

    const exportText =
      `sort_desc=${!!sortDesc} drop_bad=${dropBad} drop_empty=${dropEmpty} ` +
      `min_score=${minScore ?? "n/a"} max_checks=${maxChecks ?? "n/a"}`;

    return {
      preset,
      plannerEnabled: !!plannerEnabled,
      repairEnabled: !!repairEnabled,
      qualityEnabled: !!qualityEnabled,
      areasCap,
      temperature,
      exportText,
    };
  }

  async function tryLoadDeepAgentsRunConfig(jobId, artifactsMap) {
    // cache
    if (deepagentsRunConfigCache.has(jobId)) {
      return deepagentsRunConfigCache.get(jobId);
    }

    // only if artifact exists
    if (!artifactsMap || typeof artifactsMap !== "object") return null;
    if (!("agents_run_config" in artifactsMap)) return null;

    // server-side gate (if backend disallows debug downloads, download will 403)
    if (!allowDebugDownloads) return { _note: "server_debug_downloads_disabled" };

    try {
      const resp = await fetch(`/download/${encodeURIComponent(jobId)}/agents_run_config`);
      if (!resp.ok) {
        if (resp.status === 403) return { _note: "server_debug_downloads_disabled" };
        return null;
      }
      const cfg = await resp.json();
      deepagentsRunConfigCache.set(jobId, cfg);
      return cfg;
    } catch {
      return null;
    }
  }

  function renderDeepAgentsPanelFromStatus({ jobId, artifacts, cfg }) {
    // If DeepAgents isn't present at all, hide panel
    const hasAnyDeepAgentsArtifact =
      artifacts && typeof artifacts === "object" &&
      (
        ("agents_plan" in artifacts) ||
        ("agents_run_config" in artifacts) ||
        ("agents_final" in artifacts) ||
        ("agents_export_ready_checks" in artifacts)
      );

    if (!hasAnyDeepAgentsArtifact) {
      hideDeepAgentsPanel();
      return;
    }

    // If config isn't accessible due to server gating
    if (cfg && cfg._note === "server_debug_downloads_disabled") {
      renderDeepAgentsPanelState({
        active: true,
        badgeKind: "warn",
        badgeText: "limited",
        summaryText: "DeepAgents ran for this job, but server debug downloads are disabled — run_config.json cannot be fetched.",
        gridVisible: false,
        noteText: "Tip: set SERVER_ALLOW_DEBUG_DOWNLOADS=true (local only) if you want the UI to show run_config details.",
      });
      return;
    }

    // If we have the cfg, show full details
    const v = extractDeepAgentsValues(cfg);
    if (v) {
      renderDeepAgentsPanelState({
        active: true,
        badgeKind: "ok",
        badgeText: "active",
        summaryText: "DeepAgents configuration for this job:",
        gridVisible: true,
        noteText: "",
        preset: v.preset,
        planner: _humanToggle(v.plannerEnabled),
        repair: _humanToggle(v.repairEnabled),
        quality: _humanToggle(v.qualityEnabled),
        areasCap: (v.areasCap === null || v.areasCap === undefined) ? "—" : String(v.areasCap),
        temperature: (v.temperature === null || v.temperature === undefined) ? "—" : String(v.temperature),
        exportText: v.exportText,
      });
      return;
    }

    // Fallback: we know DeepAgents ran, but run_config artifact missing/unreadable
    renderDeepAgentsPanelState({
      active: true,
      badgeKind: "warn",
      badgeText: "unknown",
      summaryText: "DeepAgents ran for this job, but run_config details were not available.",
      gridVisible: false,
      noteText: "If you expect run_config.json, confirm orchestrator writes agents/run_config.json and routes exposes agents_run_config artifact key.",
    });
  }

  function formatDeepAgentsLine(cfg, artifacts) {
    // Keep status message concise but useful
    const hasDA = artifacts && typeof artifacts === "object" && ("agents_plan" in artifacts || "agents_final" in artifacts);
    if (!hasDA) return "";

    if (cfg && cfg._note === "server_debug_downloads_disabled") {
      return "DeepAgents: enabled (server debug downloads disabled)";
    }

    const v = extractDeepAgentsValues(cfg);
    if (!v) return "DeepAgents: enabled";

    return (
      `DeepAgents: ✅ preset=${v.preset} ` +
      `planner=${_boolWord(v.plannerEnabled)} repair=${_boolWord(v.repairEnabled)} quality=${_boolWord(v.qualityEnabled)} ` +
      `export(${v.exportText})`
    );
  }

  async function pollStatus(jobId) {
    try {
      const resp = await fetch(`/job-status/${encodeURIComponent(jobId)}`);
      if (!resp.ok) throw new Error(`Status HTTP ${resp.status}`);
      const data = await resp.json();

      jobIdEl.textContent = data.job_id;
      jobStatusEl.textContent = data.status;

      const baseMsg = data.error ? data.error : (data.message || "");
      const emailLine = renderEmailLine(data.email_status, data.email_error);

      // Load run_config if possible and render panel + optional status line
      const cfg = await tryLoadDeepAgentsRunConfig(jobId, data.artifacts);

      // Pretty panel (6.10)
      renderDeepAgentsPanelFromStatus({ jobId, artifacts: data.artifacts, cfg });

      // Keep statusBox msg readable (avoid duplicating too much)
      const deepagentsLine = formatDeepAgentsLine(cfg, data.artifacts);

      const lines = [];
      if (baseMsg) lines.push(baseMsg);
      if (emailLine) lines.push(emailLine);
      if (deepagentsLine) lines.push(deepagentsLine);

      jobMsgEl.textContent = lines.join("\n");

      setStatusVisible(true);

      // default: hide review until completion
      if (data.status !== "completed" && data.status !== "completed_with_warnings") {
        setReviewVisible(false);
      }

      if (data.status === "failed") {
        setBoxStyle("error");
        submitBtn.disabled = false;
        if (pollTimer) clearInterval(pollTimer);
        return;
      }

      if (data.status === "completed" || data.status === "completed_with_warnings") {
        setBoxStyle("success");
        submitBtn.disabled = false;
        if (pollTimer) clearInterval(pollTimer);

        lastCompletedJobId = jobId;
        renderDownloads(jobId);

        // HITL review: show panel when pending
        const rs = (data.review_status || "").toLowerCase();
        if (!rs || rs === "pending") {
          setReviewVisible(true);
          if (reviewMsgEl) reviewMsgEl.textContent = "";
        } else {
          setReviewVisible(true);
          if (reviewMsgEl) reviewMsgEl.textContent = `Review status: ${rs}`;
        }

        return;
      }

      setBoxStyle(null);
    } catch (err) {
      jobMsgEl.textContent = `Error polling status: ${err.message}`;
      setBoxStyle("error");
      submitBtn.disabled = false;
      if (pollTimer) clearInterval(pollTimer);

      // If polling errors, don’t show stale DeepAgents panel
      hideDeepAgentsPanel();
    }
  }

  async function loadUiConfig() {
    try {
      const resp = await fetch("/ui-config");
      if (!resp.ok) throw new Error(`ui-config HTTP ${resp.status}`);
      const cfg = await resp.json();

      allowDebugDownloads = !!cfg.allow_debug_downloads;

      // Only show the toggle if backend allows debug downloads
      setDebugRowVisible(allowDebugDownloads);

      // Default: unchecked
      if (debugToggleEl) debugToggleEl.checked = false;
    } catch (e) {
      // fail closed
      allowDebugDownloads = false;
      setDebugRowVisible(false);
    }
  }

  // Re-render downloads if user toggles debug after completion
  if (debugToggleEl) {
    debugToggleEl.addEventListener("change", () => {
      if (lastCompletedJobId) renderDownloads(lastCompletedJobId);
    });
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();

    clearDownloads();
    setDownloadsVisible(false);
    setReviewVisible(false);
    if (reviewFeedbackEl) reviewFeedbackEl.value = "";
    lastCompletedJobId = null;
    deepagentsRunConfigCache.clear();
    hideDeepAgentsPanel();

    const fileInput = document.getElementById("contract");
    const email = document.getElementById("email").value.trim();
    const program = document.getElementById("program").value.trim();
    const contractId = document.getElementById("contract_id").value.trim();
    const contractVersion = document.getElementById("contract_version").value.trim();

    if (!fileInput.files || !fileInput.files[0]) {
      alert("Please select a PDF or DOCX contract file.");
      return;
    }
    if (!email) {
      alert("Please enter at least one recipient email.");
      return;
    }

    submitBtn.disabled = true;
    setBoxStyle(null);
    setStatusVisible(true);
    jobIdEl.textContent = "";
    jobStatusEl.textContent = "uploading";
    jobMsgEl.textContent = "Uploading contract...";

    const fd = new FormData();
    fd.append("contract", fileInput.files[0]);
    fd.append("email", email);
    if (program) fd.append("program", program);
    if (contractId) fd.append("contract_id", contractId);
    if (contractVersion) fd.append("contract_version", contractVersion);

    try {
      const resp = await fetch("/upload", { method: "POST", body: fd });
      const data = await resp.json();

      if (!resp.ok) {
        const msg = data && (data.detail || data.error) ? (data.detail || data.error) : "Upload failed.";
        throw new Error(msg);
      }

      const jobId = data.job_id;
      jobIdEl.textContent = jobId;
      jobStatusEl.textContent = "queued";
      jobMsgEl.textContent = "Job queued. Starting processing...";

      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(() => pollStatus(jobId), 1500);
      await pollStatus(jobId);
    } catch (err) {
      jobStatusEl.textContent = "failed";
      jobMsgEl.textContent = err.message;
      setBoxStyle("error");
      submitBtn.disabled = false;
      hideDeepAgentsPanel();
    }
  });

  // HITL button handlers
  if (btnApproveEl) {
    btnApproveEl.addEventListener("click", async () => {
      if (!lastCompletedJobId) return;
      try {
        if (reviewMsgEl) reviewMsgEl.textContent = "Submitting approval...";
        await submitReview(lastCompletedJobId, "approve", null);
        if (reviewMsgEl) reviewMsgEl.textContent = "Approved. ✅";
      } catch (e) {
        if (reviewMsgEl) reviewMsgEl.textContent = `Approve failed: ${e.message}`;
      }
    });
  }

  if (btnRejectRerunEl) {
    btnRejectRerunEl.addEventListener("click", async () => {
      if (!lastCompletedJobId) return;
      const fb = (reviewFeedbackEl && reviewFeedbackEl.value) ? reviewFeedbackEl.value.trim() : "";
      if (!fb) {
        alert("Please provide feedback before rejecting.");
        return;
      }
      try {
        if (reviewMsgEl) reviewMsgEl.textContent = "Submitting rejection and re-running...";
        await submitReview(lastCompletedJobId, "reject", fb);
        const out = await rerunJob(lastCompletedJobId, fb);
        const newJobId = out.new_job_id;
        // Switch UI to new job
        lastCompletedJobId = null;
        clearDownloads();
        setDownloadsVisible(false);
        deepagentsRunConfigCache.clear();
        hideDeepAgentsPanel();
        setReviewVisible(false);
        if (reviewFeedbackEl) reviewFeedbackEl.value = "";

        jobIdEl.textContent = newJobId;
        jobStatusEl.textContent = "queued";
        jobMsgEl.textContent = "Rerun queued. Starting processing...";

        if (pollTimer) clearInterval(pollTimer);
        pollTimer = setInterval(() => pollStatus(newJobId), 1500);
        await pollStatus(newJobId);
      } catch (e) {
        if (reviewMsgEl) reviewMsgEl.textContent = `Reject/rerun failed: ${e.message}`;
      }
    });
  }

  // Init
  loadUiConfig();
})();
