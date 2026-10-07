/* The browser displays decisions made by the server. It never authorizes a booking. */
"use strict";

const $ = (id) => document.getElementById(id);
const STORAGE_KEY = "carepath.demo.session.v1";
const state = {config: {}, patient: "maya", token: null, staffToken: null, conversation: null, busy: false, messages: [], report: null, polling: false, feedbackSent: new Set()};
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
};
const humanize = (value) => String(value || "").replace(/[_-]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
const first = (...values) => values.find((v) => v !== undefined && v !== null && v !== "");

async function api(path, {method = "GET", body, staff = false, anonymous = false} = {}) {
  const headers = {"Accept": "application/json"};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const token = staff ? state.staffToken : state.token;
  if (!anonymous && token) headers.Authorization = `Bearer ${token}`;
  let response;
  try { response = await fetch(path, {method, headers, body: body !== undefined ? JSON.stringify(body) : undefined}); }
  catch { throw new Error("The local server could not be reached. Check that it is running, then refresh the conversation."); }
  let result;
  try { result = await response.json(); } catch { result = {}; }
  if (!response.ok) {
    const detail = result.detail;
    const reason = typeof detail === "string" ? detail : (detail && detail.message) || result.message;
    const error = new Error(reason || `The request could not be completed (${response.status}).`);
    error.status = response.status;
    throw error;
  }
  return result;
}

function saveSession() {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify({patient: state.patient, token: state.token, conversation: state.conversation})); } catch { /* Storage is optional. */ }
}
function showError(id, message) { $(id).textContent = message; $(id).hidden = !message; }
function setBusy(busy) {
  state.busy = busy;
  $("send-button").disabled = busy;
  $("message-input").disabled = busy;
  $("patient-select").disabled = busy;
  $("new-conversation").disabled = busy;
  document.querySelectorAll("[data-prompt], #conversation-actions button").forEach((button) => { button.disabled = busy; });
  $("typing").hidden = !busy;
  if (busy) scrollChat();
}
function scrollChat() { requestAnimationFrame(() => { $("chat-scroll").scrollTop = $("chat-scroll").scrollHeight; }); }
function patientName() {
  return first(state.config.patients?.find((patient) => first(patient.key, patient.patient_key, patient.id) === state.patient)?.name, $("patient-select").selectedOptions[0]?.textContent, "Demo patient");
}

async function signIn(patient = state.patient) {
  const session = await api("/api/sessions", {method: "POST", body: {patient_key: patient, role: "patient"}, anonymous: true});
  state.patient = patient;
  state.token = first(session.token, session.access_token);
  if (!state.token) throw new Error("The demo server did not return a patient session.");
  saveSession();
}
async function ensureStaff() {
  if (state.staffToken) return;
  const session = await api("/api/sessions", {method: "POST", body: {patient_key: state.patient, role: "staff"}, anonymous: true});
  state.staffToken = first(session.token, session.access_token);
  if (!state.staffToken) throw new Error("The demo server did not return a staff session.");
}

function appendMessage(role, content, persist = true) {
  if (!content) return;
  const user = role === "user" || role === "patient";
  const wrapper = el("div", `message ${user ? "user" : "assistant"}`);
  const avatar = el("span", "message-avatar", user ? patientName().split(/\s+/).map((part) => part[0]).slice(0, 2).join("") : "+");
  avatar.setAttribute("aria-hidden", "true");
  const body = el("div", "message-body");
  body.append(el("div", "message-bubble", content), el("div", "message-meta", user ? "You" : "Appointment assistant"));
  wrapper.append(avatar, body);
  $("messages").append(wrapper);
  $("chat-intro").hidden = true;
  if (persist) state.messages.push({role: user ? "user" : "assistant", content: String(content)});
  scrollChat();
}

function slotFacts(value = {}) {
  const slot = value.slot || value.appointment || value;
  return {
    id: first(slot.id, slot.slot_id, value.slot_id),
    starts: first(slot.starts_at, slot.start_at, slot.start, slot.start_time, slot.datetime, value.starts_at, value.start_at),
    timezone: first(slot.timezone, value.timezone, state.config.timezone, "Asia/Kolkata"),
    clinician: first(slot.clinician_name, slot.clinician, slot.provider_name, slot.provider, value.clinician_name, value.clinician),
    location: first(slot.location_name, slot.location, slot.modality, value.location),
    service: first(slot.appointment_type, slot.service_name, slot.service, value.appointment_type, value.service),
    ends: first(slot.ends_at, slot.end_at, slot.end),
  };
}
function dateLabel(value, timezone, full = false) {
  if (!value) return "Time provided by the clinic";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  try {
    return new Intl.DateTimeFormat("en-IN", {timeZone: timezone, weekday: full ? "long" : "short", day: "numeric", month: "short", year: full ? "numeric" : undefined, hour: "numeric", minute: "2-digit"}).format(date);
  } catch { return String(value); }
}
function addFacts(parent, facts) {
  const list = el("dl", "appointment-facts");
  const rows = [["Appointment", humanize(facts.service)], ["When", facts.starts ? dateLabel(facts.starts, facts.timezone, true) : null], ["Timezone", facts.starts ? facts.timezone : null], ["Clinician", typeof facts.clinician === "object" ? first(facts.clinician.name, facts.clinician.id) : facts.clinician], ["Location", typeof facts.location === "object" ? first(facts.location.name, facts.location.id) : facts.location]];
  for (const [label, value] of rows) if (value) list.append(el("dt", "", label), el("dd", "", value));
  parent.append(list);
}
function actionButton(label, className, callback) {
  const button = el("button", `button ${className}`, label);
  button.type = "button";
  button.addEventListener("click", callback);
  return button;
}
function feedbackForm() {
  const form = el("form", "feedback-card");
  form.append(el("h3", "", "How did this conversation feel?"), el("p", "", "Your feedback is saved for review. It does not automatically change booking or safety rules."));
  if (state.feedbackSent.has(state.conversation)) { form.append(el("div", "feedback-thanks", "✓ Thank you. Your feedback is recorded.")); return form; }
  const fieldset = el("fieldset", "rating-options");
  fieldset.append(el("legend", "sr-only", "Rate the conversation from 1, poor, to 5, great"));
  [1, 2, 3, 4, 5].forEach((rating) => {
    const label = el("label", "rating-option");
    const input = el("input"); input.type = "radio"; input.name = "rating"; input.value = String(rating); input.required = true;
    label.append(input, el("span", "", `${rating}${rating === 1 ? " · Poor" : rating === 5 ? " · Great" : ""}`));
    fieldset.append(label);
  });
  const label = el("label", "feedback-label", "Anything we could do better? (optional)");
  const comment = el("textarea", "feedback-comment"); comment.rows = 2; comment.maxLength = 1000; comment.placeholder = "Share feedback about the scheduling experience…";
  label.append(comment);
  const submit = el("button", "button button-secondary small", "Send feedback"); submit.type = "submit";
  const error = el("div", "feedback-error"); error.setAttribute("role", "alert");
  form.append(fieldset, label, submit, error);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const selected = form.querySelector('input[name="rating"]:checked');
    if (!selected || !state.conversation) return;
    submit.disabled = true; error.textContent = "";
    try {
      await api(`/api/conversations/${encodeURIComponent(state.conversation)}/feedback`, {method: "POST", body: {rating: Number(selected.value), comment: comment.value.trim()}});
      state.feedbackSent.add(state.conversation);
      fieldset.disabled = true; comment.disabled = true; submit.textContent = "✓ Feedback recorded";
    } catch (failure) { error.textContent = failure.message; submit.disabled = false; }
  });
  return form;
}
function renderActions(result = {}) {
  const container = $("conversation-actions");
  container.replaceChildren();
  const status = result.status || "ready";
  const proposal = result.proposal;
  const booking = first(result.booking, result.appointment);
  const handoff = result.handoff;
  const slots = first(result.slots, result.options, []);
  const booked = ["booked", "confirmed", "booking_confirmed"].includes(status) || (booking && ["booked", "confirmed"].includes(booking.status));

  if (booked && booking) {
    const card = el("div", "action-card receipt-card");
    card.append(el("span", "receipt-icon", "✓"), el("h3", "", "Your appointment is booked."));
    addFacts(card, slotFacts(booking));
    const ref = first(booking.id, booking.appointment_id, booking.reference);
    if (ref) card.append(el("div", "status-ref", `Booking reference: ${ref}`));
    container.append(card);
  } else if (handoff && first(handoff.id, handoff.handoff_id, handoff.ticket_id)) {
    const card = el("div", "action-card handoff-card");
    card.append(el("h3", "", "Your request is queued for staff."), el("p", "", "The handoff is recorded. A queued request does not mean a staff member is connected yet."));
    const reason = first(handoff.reason, handoff.reason_code);
    if (reason) card.append(el("p", "", humanize(reason)));
    card.append(el("div", "status-ref", `Reference: ${first(handoff.id, handoff.handoff_id, handoff.ticket_id)} · ${humanize(handoff.status || "queued")}`));
    container.append(card);
  } else if (["outcome_unknown", "reconciliation_required", "booking_pending"].includes(status)) {
    const card = el("div", "action-card handoff-card");
    card.append(el("h3", "", "We’re checking the booking outcome."), el("p", "", "Please don’t start another booking for this appointment. The original attempt needs to be resolved first."));
    container.append(card);
  } else if (proposal && !["escalated", "emergency", "handoff", "closed"].includes(status)) {
    const card = el("div", "action-card");
    card.append(el("h3", "", "Does this appointment work for you?"), el("p", "", "Check the details below. Nothing is booked until you confirm this exact appointment."));
    addFacts(card, slotFacts(proposal));
    const proposalId = first(proposal.id, proposal.proposal_id);
    card.append(actionButton("Confirm this appointment", "button-primary", () => sendMessage("Yes.", {action: "confirm", proposal_id: proposalId})), actionButton("Change my preferences", "button-secondary", () => sendMessage("I want to change my preferences.")));
    container.append(card);
  } else if (Array.isArray(slots) && slots.length && !["escalated", "emergency", "handoff", "closed"].includes(status)) {
    const list = el("div", "slot-options");
    list.setAttribute("aria-label", "Available appointments");
    slots.forEach((slot) => {
      const facts = slotFacts(slot);
      const card = el("div", "slot-card");
      const info = el("div");
      info.append(el("div", "slot-date", dateLabel(facts.starts, facts.timezone)));
      info.append(el("div", "slot-info", [typeof facts.clinician === "object" ? facts.clinician.name : facts.clinician, typeof facts.location === "object" ? facts.location.name : facts.location].filter(Boolean).join(" · ")));
      const button = actionButton("Choose this time", "button-secondary", () => sendMessage("Choose this appointment.", {action: "select_slot", slot_id: facts.id}));
      button.disabled = !facts.id;
      card.append(info, button);
      list.append(card);
    });
    container.append(list);
  }
  if (booked || (handoff && first(handoff.id, handoff.handoff_id, handoff.ticket_id))) container.append(feedbackForm());
  $("workflow-status").textContent = humanize(status).slice(0, 35);
  $("chat-card")?.classList.toggle("status-emergency", status === "emergency");
  const step = booked ? 3 : proposal ? 2 : Array.isArray(slots) && slots.length ? 1 : 0;
  renderRequest(result.constraints || {});
  document.querySelectorAll(".journey li").forEach((item, index) => {
    item.classList.toggle("current", index === step);
    item.classList.toggle("completed", index < step);
    item.querySelector("span").textContent = index < step ? "✓" : String(index + 1);
  });
  scrollChat();
}
function renderRequest(constraints) {
  const container = $("request-facts");
  container.replaceChildren();
  const hourLabel = (hour) => `${hour % 12 || 12} ${hour >= 12 ? "PM" : "AM"}`;
  const time = [constraints.after_hour !== null && constraints.after_hour !== undefined ? `After ${hourLabel(constraints.after_hour)}` : null, constraints.before_hour !== null && constraints.before_hour !== undefined ? `Before ${hourLabel(constraints.before_hour)}` : null].filter(Boolean).join(" · ");
  const rows = [["Visit", constraints.appointment_type ? humanize(constraints.appointment_type) : null], ["Day", constraints.date], ["Time", time], ["Clinician", constraints.provider_id ? "Preferred clinician requested" : null]];
  rows.forEach(([name, value]) => { if (value) container.append(el("dt", "", name), el("dd", "", value)); });
  $("request-card").hidden = !container.children.length;
}

async function newConversation() {
  if (state.busy) return;
  setBusy(true);
  showError("composer-error", "");
  try {
    if (!state.token) await signIn();
    const conversation = await api("/api/conversations", {method: "POST", body: {}});
    state.conversation = first(conversation.conversation_id, conversation.id);
    if (!state.conversation) throw new Error("The server did not return a conversation reference.");
    state.messages = [];
    $("messages").replaceChildren();
    $("chat-intro").hidden = false;
    if (conversation.reply) appendMessage("assistant", conversation.reply);
    renderActions(conversation);
    saveSession();
  } catch (error) { showError("composer-error", error.message); }
  finally { setBusy(false); }
}
async function restoreConversation() {
  if (!state.conversation) return newConversation();
  const record = await api(`/api/conversations/${encodeURIComponent(state.conversation)}`);
  const messages = first(record.messages, record.history, record.state?.messages, []);
  $("messages").replaceChildren();
  state.messages = [];
  $("chat-intro").hidden = false;
  if (Array.isArray(messages)) messages.forEach((message) => {
    const content = first(message.content, message.text);
    const role = first(message.role, message.type);
    if (typeof content === "string" && ["user", "patient", "assistant", "human", "ai"].includes(role)) appendMessage(["user", "patient", "human"].includes(role) ? "user" : "assistant", content);
  });
  renderActions(record.state ? {...record.state, ...record} : record);
}
function conversationFailure(message) {
  const area = $("composer-error");
  area.replaceChildren(el("span", "", message + " "));
  const refresh = el("button", "", "Refresh conversation");
  refresh.type = "button";
  refresh.addEventListener("click", async () => {
    setBusy(true);
    try { await restoreConversation(); showError("composer-error", ""); }
    catch (error) { conversationFailure(error.message); }
    finally { setBusy(false); }
  });
  area.append(refresh);
  area.hidden = false;
}
async function sendMessage(text, action = {}) {
  text = text.trim();
  if (!text || state.busy || !state.conversation) return;
  showError("composer-error", "");
  appendMessage("user", text);
  $("message-input").value = "";
  $("message-input").style.height = "auto";
  setBusy(true);
  try {
    const result = await api(`/api/conversations/${encodeURIComponent(state.conversation)}/messages`, {method: "POST", body: {text, turn_id: crypto.randomUUID(), ...action}});
    appendMessage("assistant", first(result.reply, result.message));
    renderActions(result);
    saveSession();
  } catch (error) { conversationFailure(error.message + " The send will not be automatically repeated."); }
  finally { setBusy(false); $("message-input").focus(); }
}

async function loadHandoffs() {
  const button = $("refresh-handoffs");
  button.disabled = true;
  showError("staff-error", "");
  try {
    await ensureStaff();
    const response = await api("/api/handoffs", {staff: true});
    const handoffs = Array.isArray(response) ? response : first(response.handoffs, response.items, []);
    $("stat-queued").textContent = handoffs.filter((item) => ["queued", "open", "pending"].includes(item.status || "queued")).length;
    $("stat-review").textContent = handoffs.filter((item) => ["accepted", "in_review", "reviewing"].includes(item.status)).length;
    $("stat-resolved").textContent = handoffs.filter((item) => ["resolved", "closed"].includes(item.status)).length;
    const pending = handoffs.filter((item) => !["resolved", "closed"].includes(item.status)).length;
    $("handoff-count").textContent = pending;
    $("handoff-count").hidden = !pending;
    const list = $("handoff-list");
    list.replaceChildren();
    if (!handoffs.length) {
      const empty = el("div", "empty-state");
      empty.append(el("span", "empty-icon", "✓"), el("h3", "", "No handoffs waiting."), el("p", "", "Requests appear here when a patient asks for a person or the assistant reaches a situation that needs staff."));
      list.append(empty);
    }
    handoffs.forEach((handoff) => {
      const row = el("article", "handoff-row");
      const details = el("div");
      const heading = el("h3", "", first(handoff.patient_name, handoff.patient?.name, handoff.patient_key, handoff.patient_id, "Demo patient"));
      const status = handoff.status || "queued";
      heading.append(el("span", `tag ${["resolved", "closed"].includes(status) ? "good" : ["accepted", "in_review"].includes(status) ? "review" : ""}`, status === "accepted" ? "In review" : humanize(status)));
      details.append(heading, el("p", "", first(handoff.summary, handoff.request_summary, handoff.reason_detail, humanize(handoff.reason || handoff.reason_code), "Staff review requested.")));
      details.append(el("div", "handoff-meta", [first(handoff.id, handoff.handoff_id, handoff.ticket_id), humanize(first(handoff.destination, handoff.queue)), handoff.created_at ? dateLabel(handoff.created_at, state.config.timezone) : null].filter(Boolean).join(" · ")));
      const actions = el("div", "handoff-buttons");
      const id = first(handoff.id, handoff.handoff_id, handoff.ticket_id);
      if (!["resolved", "closed"].includes(status) && id) {
        if (!["accepted", "in_review"].includes(status)) actions.append(actionButton("Start review", "button-secondary small", () => updateHandoff(id, "accepted")));
        actions.append(actionButton("Mark resolved", "button-primary small", () => updateHandoff(id, "resolved")));
      }
      row.append(details, actions);
      list.append(row);
    });
  } catch (error) { showError("staff-error", error.message); }
  finally { button.disabled = false; }
}
async function updateHandoff(id, status) {
  document.querySelectorAll(".handoff-buttons button").forEach((button) => { button.disabled = true; });
  try { await api(`/api/handoffs/${encodeURIComponent(id)}`, {method: "PATCH", body: {status}, staff: true}); await loadHandoffs(); }
  catch (error) { showError("staff-error", error.message); document.querySelectorAll(".handoff-buttons button").forEach((button) => { button.disabled = false; }); }
}

function scoreData(section = {}) {
  const metrics = section.metrics || section.summary || section;
  const count = first(metrics.total, metrics.total_scenarios, metrics.total_runs, section.total);
  const passed = first(metrics.passed, metrics.resolved, metrics.passed_scenarios, metrics.verified_passes, section.passed);
  let rate = first(metrics.resolution_rate, metrics.verified_resolution_rate, metrics.task_resolution_rate, metrics.pass_rate, metrics.score);
  if (rate === undefined && count) rate = passed / count;
  if (typeof rate === "number" && rate > 1) rate /= 100;
  return {rate: typeof rate === "number" ? rate : null, count, passed, violations: first(metrics.critical_violations, metrics.safety_violations, 0)};
}
function renderScore(prefix, section) {
  const score = scoreData(section);
  $(prefix + "-score").textContent = score.rate !== null ? `${Math.round(score.rate * 100)}%` : "—";
  $(prefix + "-bar").style.width = `${Math.max(0, Math.min(100, (score.rate || 0) * 100))}%`;
  $(prefix + "-detail").textContent = [score.count !== undefined && score.passed !== undefined ? `${score.passed} / ${score.count} verified resolutions` : null, `${score.violations} critical violations`].filter(Boolean).join(" · ");
  $(prefix + "-version").textContent = first(section.behavior_version, section.version, section.experiment_name, humanize(prefix));
}
function statusTag(value) {
  const pass = value === true || ["pass", "passed", "success", "resolved"].includes(String(value).toLowerCase());
  const failure = value === false || ["fail", "failed", "failure"].includes(String(value).toLowerCase());
  return el("span", `tag ${pass ? "good" : failure ? "bad" : ""}`, pass ? "Pass" : failure ? "Fail" : first(value, "—"));
}
function safeExternalUrl(url) {
  try { const parsed = new URL(url); return parsed.protocol === "https:" && /(^|\.)smith\.langchain\.com$/.test(parsed.hostname) ? parsed.href : null; } catch { return null; }
}
function renderReport(report) {
  if (!report || typeof report !== "object") return;
  state.report = report;
  $("eval-empty").hidden = true;
  $("eval-report").hidden = false;
  const baseline = first(report.baseline, report.before, {});
  const candidate = first(report.candidate, report.after, {});
  renderScore("baseline", baseline);
  renderScore("candidate", candidate);
  const accepted = first(report.accepted, report.promoted, report.candidate?.accepted, report.gates?.accepted, report.promotion?.accepted, report.decision?.accepted);
  $("promotion-icon").textContent = accepted === true ? "✓" : accepted === false ? "×" : "—";
  $("promotion-title").textContent = accepted === true ? "Candidate accepted" : accepted === false ? "Baseline retained" : "Results available";
  $("promotion-detail").textContent = first(report.promotion?.reason, report.decision?.reason, report.reason, accepted ? "The candidate passed the declared safety and regression gates." : "Review the gate results before accepting a behavior change.");
  const gates = first(report.gates, report.promotion?.gates, report.decision?.gates, {});
  const gateList = $("gate-list");
  gateList.replaceChildren();
  const checkItems = Array.isArray(gates.checks) ? gates.checks : Array.isArray(gates) ? gates : null;
  const entries = checkItems ? checkItems.map((gate) => [first(gate.name, gate.key, gate.label), first(gate.passed, gate.pass, gate.value)]) : Object.entries(gates).filter(([key]) => key !== "accepted");
  entries.forEach(([name, value]) => {
    const passed = typeof value === "object" ? first(value.passed, value.pass, value.value) : value;
    const gate = el("div", `gate ${passed === false ? "bad" : ""}`);
    gate.append(el("span", "gate-symbol", passed === true ? "✓" : passed === false ? "×" : "·"), el("span", "", humanize(name)));
    gateList.append(gate);
  });
  const rows = $("scenario-rows");
  rows.replaceChildren();
  let scenarios = first(report.scenarios, report.comparison, report.scenario_comparison, []);
  if (!Array.isArray(scenarios)) scenarios = Object.entries(scenarios).map(([name, value]) => ({name, ...value}));
  const baselineRows = first(baseline.rows, baseline.results, []);
  const candidateRows = first(candidate.rows, candidate.results, []);
  if (!scenarios.length && Array.isArray(baselineRows)) scenarios = baselineRows.map((result, index) => {
    const other = candidateRows.find((item) => first(item.scenario_id, item.name, item.id) === first(result.scenario_id, result.name, result.id)) || candidateRows[index];
    return {name: first(result.name, result.scenario_id, result.id), baseline: first(result.passed, result.pass, result.task_resolution), candidate: first(other?.passed, other?.pass, other?.task_resolution), detail: first(other?.detail, other?.reason, result.detail)};
  });
  scenarios.forEach((scenario) => {
    const row = el("tr");
    const name = first(scenario.name, scenario.title, scenario.scenario_id, scenario.id, "Scenario");
    const before = typeof scenario.baseline === "object" ? first(scenario.baseline.passed, scenario.baseline.pass, scenario.baseline.status) : first(scenario.baseline, scenario.before, scenario.baseline_passed);
    const after = typeof scenario.candidate === "object" ? first(scenario.candidate.passed, scenario.candidate.pass, scenario.candidate.status) : first(scenario.candidate, scenario.after, scenario.candidate_passed);
    const beforeCell = el("td"); beforeCell.append(statusTag(before));
    const afterCell = el("td"); afterCell.append(statusTag(after));
    row.append(el("td", "", humanize(name)), beforeCell, afterCell, el("td", "", first(scenario.detail, scenario.note, scenario.reason, scenario.description, "")));
    rows.append(row);
  });
  if (!scenarios.length) { const row = el("tr"); const cell = el("td", "", "Per-scenario details are available in the downloaded report."); cell.colSpan = 4; row.append(cell); rows.append(row); }
  const artifact = first(report.improvement, report.artifact, report.improvement_artifact, candidate.artifact, {});
  $("improvement-artifact").textContent = JSON.stringify(artifact, null, 2);
  const limitations = Array.isArray(report.limitations) ? report.limitations.join(" ") : report.limitations;
  $("report-note").textContent = [report.generated_at ? `Run: ${dateLabel(report.generated_at, state.config.timezone)}.` : null, first(report.note, limitations, "Results cover synthetic scenarios in an isolated clinic. Repeated runs provide evidence, not a guarantee of production reliability.")].filter(Boolean).join(" ");
  if (report.engine) $("eval-runtime-text").textContent = `LangSmith SDK evaluation · ${report.engine.upload_results ? "cloud results enabled" : "local results; cloud upload off"} · ${report.engine.actor === "langchain-model" ? "LangChain model" : "deterministic demo actor"}`;
  const url = safeExternalUrl(first(report.langsmith_url, report.experiment_url, candidate.langsmith_url, candidate.experiment_url));
  $("langsmith-link").hidden = !url;
  if (url) $("langsmith-link").href = url;
  $("eval-state").textContent = accepted === true ? "Candidate accepted" : "Completed";
}

async function loadEvaluation() {
  showError("eval-error", "");
  try {
    await ensureStaff();
    const result = await api("/api/evaluations/latest", {staff: true});
    const report = first(result.report, result.result, result.baseline ? result : undefined);
    if (report) renderReport(report);
  } catch (error) { if (error.status !== 404) showError("eval-error", error.message); }
}
async function runEvaluation() {
  if (state.polling) return;
  showError("eval-error", "");
  $("run-evaluation").disabled = true;
  $("eval-progress").hidden = false;
  $("eval-state").textContent = "Running";
  state.polling = true;
  try {
    await ensureStaff();
    const result = await api("/api/evaluations/run", {method: "POST", body: {}, staff: true});
    if (result.report || result.baseline) { renderReport(result.report || result); return; }
    const jobId = first(result.job_id, result.id);
    if (!jobId) throw new Error("The evaluation service did not return a job reference.");
    for (let attempt = 0; attempt < 900; attempt += 1) {
      const job = await api(`/api/evaluations/jobs/${encodeURIComponent(jobId)}`, {staff: true});
      $("eval-progress-title").textContent = first(job.message, job.phase ? humanize(job.phase) : null, "Running the improvement loop");
      if (job.detail) $("eval-progress-detail").textContent = typeof job.detail === "string" ? job.detail : "Scenarios are running in isolated clinic fixtures.";
      if (["completed", "complete", "succeeded", "done"].includes(job.status)) {
        const report = first(job.report, job.result);
        if (!report) throw new Error("The evaluation completed without a report.");
        renderReport(report);
        return;
      }
      if (["failed", "error", "cancelled"].includes(job.status)) throw new Error(first(job.error, job.message, "The evaluation job could not complete. No candidate was accepted."));
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
    throw new Error("The job is still running. Reopen this view later to load its result.");
  } catch (error) { showError("eval-error", error.message); $("eval-state").textContent = "Could not complete"; }
  finally { state.polling = false; $("run-evaluation").disabled = false; $("eval-progress").hidden = true; }
}

function selectView() {
  const requested = location.hash.slice(1);
  const view = ["patient", "staff", "evaluation"].includes(requested) ? requested : "patient";
  document.querySelectorAll(".view").forEach((section) => { section.hidden = section.id !== `view-${view}`; });
  document.querySelectorAll(".nav-item").forEach((link) => {
    const active = link.dataset.view === view;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  });
  $("breadcrumb-view").textContent = {patient: "Patient experience", staff: "Staff workspace", evaluation: "Evaluation lab"}[view];
  if (view === "staff") loadHandoffs();
  if (view === "evaluation" && !state.polling) loadEvaluation();
}

async function boot() {
  try {
    state.config = await api("/api/config", {anonymous: true});
    $("connection-status").className = "connection online";
    $("connection-status").replaceChildren(el("span"), document.createTextNode("Local server online"));
    const patients = state.config.patients;
    if (Array.isArray(patients) && patients.length) {
      $("patient-select").replaceChildren();
      patients.forEach((patient) => { const option = el("option", "", first(patient.name, patient.label, patient.key)); option.value = first(patient.key, patient.patient_key, patient.id); $("patient-select").append(option); });
    }
    const mode = first(state.config.model_mode, state.config.mode, "demo");
    $("model-label").textContent = ["live", "llm", "openai"].includes(mode) ? "LangChain model · synthetic clinic" : "Deterministic demo · LangGraph workflow";
    const evaluationEnabled = first(state.config.langsmith_enabled, state.config.evaluation_enabled);
    $("eval-runtime-text").textContent = evaluationEnabled === false ? "LangSmith SDK evaluation · local results · cloud upload off" : "LangSmith evaluation · synthetic data · isolated booking side effects";
    let saved;
    try { saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null"); } catch { saved = null; }
    if (saved && Array.from($("patient-select").options).some((option) => option.value === saved.patient)) {
      state.patient = saved.patient;
      state.token = saved.token;
      state.conversation = saved.conversation;
    } else state.patient = $("patient-select").value;
    $("patient-select").value = state.patient;
    if (state.token && state.conversation) {
      try { await restoreConversation(); }
      catch { state.token = null; state.conversation = null; await signIn(); await newConversation(); }
    } else { await signIn(); await newConversation(); }
    selectView();
  } catch (error) {
    $("connection-status").className = "connection offline";
    $("connection-status").replaceChildren(el("span"), document.createTextNode("Server unavailable"));
    showError("global-error", error.message + " Refresh this page after the server is available.");
    $("send-button").disabled = true;
  }
}

$("message-form").addEventListener("submit", (event) => { event.preventDefault(); sendMessage($("message-input").value); });
$("message-input").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); if (!state.busy) sendMessage($("message-input").value); } });
$("message-input").addEventListener("input", () => { $("message-input").style.height = "auto"; $("message-input").style.height = Math.min($("message-input").scrollHeight, 100) + "px"; });
document.querySelectorAll("[data-prompt]").forEach((button) => button.addEventListener("click", () => sendMessage(button.dataset.prompt)));
$("new-conversation").addEventListener("click", newConversation);
$("patient-select").addEventListener("change", async () => {
  state.token = null;
  state.conversation = null;
  state.patient = $("patient-select").value;
  setBusy(true);
  try { await signIn(); }
  catch (error) { showError("composer-error", error.message); }
  finally { setBusy(false); }
  if (state.token) await newConversation();
});
$("refresh-handoffs").addEventListener("click", loadHandoffs);
$("run-evaluation").addEventListener("click", runEvaluation);
$("download-report").addEventListener("click", () => {
  if (!state.report) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(state.report, null, 2)], {type: "application/json"}));
  const link = el("a"); link.href = url; link.download = "carepath-evaluation-report.json"; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
window.addEventListener("hashchange", selectView);
boot();
