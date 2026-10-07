"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, errorText } from "@/lib/api";
import type { Action, Appointment, Config, Conversation, EvaluationJob, EvaluationReport, Handoff, Message, Session, Slot } from "@/lib/types";

type View = "patient" | "staff" | "evaluation";
const humanize = (value: string = "") => value.replace(/[_-]/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
const STORAGE_KEY = "carepath.next.conversation.v1";
const DEFAULT_PATIENTS = [{ id: "patient-maya", name: "Maya Patel" }, { id: "patient-arjun", name: "Arjun Mehta" }];

function Icon({ kind }: { kind: "cross" | "chat" | "staff" | "lab" | "send" | "info" | "shield" | "play" | "calendar" }) {
  const paths = {
    cross: <path d="M9 3h6v6h6v6h-6v6H9v-6H3V9h6z" />,
    chat: <><path d="M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-5 3v-3H3V6a2 2 0 0 1 2-2z" /><path d="M7 9h10M7 13h7" /></>,
    staff: <><circle cx="9" cy="8" r="3" /><path d="M3 20v-3a5 5 0 0 1 10 0v3M16 5a3 3 0 0 1 0 6M17 14a4 4 0 0 1 4 4v2" /></>,
    lab: <path d="M8 3h8M10 3v7L4 20h16l-6-10V3M7 15h10" />,
    send: <path d="m4 4 17 8-17 8 3-8-3-8zM7 12h14" />,
    info: <><circle cx="12" cy="12" r="9" /><path d="M12 11v6M12 7v1" /></>,
    shield: <><path d="M12 2 3 6v6c0 5 9 10 9 10s9-5 9-10V6z" /><path d="m8 12 3 3 5-6" /></>,
    play: <path d="m8 5 11 7-11 7z" />,
    calendar: <><rect x="4" y="5" width="16" height="16" rx="3" /><path d="M4 10h16M8 3v4M16 3v4m-9 8 3 3 7-7" /></>,
  };
  return <svg viewBox="0 0 24 24" aria-hidden="true">{paths[kind]}</svg>;
}

function dateLabel(value: string, timezone = "Asia/Kolkata", full = false) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  try { return new Intl.DateTimeFormat("en-IN", { timeZone: timezone, weekday: full ? "long" : "short", day: "numeric", month: "short", year: full ? "numeric" : undefined, hour: "numeric", minute: "2-digit" }).format(date); }
  catch { return value; }
}

function AppointmentFacts({ slot }: { slot: Slot | Appointment }) {
  return <dl className="appointment-facts"><dt>Appointment</dt><dd>{humanize(slot.appointment_type)}</dd><dt>When</dt><dd>{dateLabel(slot.start_at, slot.timezone, true)}</dd><dt>Timezone</dt><dd>{slot.timezone || "Asia/Kolkata"}</dd><dt>Clinician</dt><dd>{slot.provider_name}</dd><dt>Location</dt><dd>{slot.location}</dd></dl>;
}

function FeedbackForm({ token, conversationId }: { token: string; conversationId: string }) {
  const [rating, setRating] = useState<number | null>(null);
  const [comment, setComment] = useState("");
  const [status, setStatus] = useState<"ready" | "sending" | "sent">("ready");
  const [error, setError] = useState("");
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!rating || status !== "ready") return;
    setStatus("sending"); setError("");
    try { await api(`/api/conversations/${conversationId}/feedback`, { method: "POST", token, body: { rating, comment } }); setStatus("sent"); }
    catch (failure) { setError(errorText(failure)); setStatus("ready"); }
  }
  return <form className="feedback-card" onSubmit={submit}><h3>How did this conversation feel?</h3><p>Your feedback is saved for review. It does not automatically change safety or booking rules.</p>{status === "sent" ? <div className="feedback-thanks" role="status">✓ Thank you. Your feedback is recorded.</div> : <><fieldset className="rating-options" disabled={status === "sending"}><legend className="sr-only">Rate the conversation from 1, poor, to 5, great</legend>{[1, 2, 3, 4, 5].map((number) => <label className="rating-option" key={number}><input type="radio" name="rating" required value={number} checked={rating === number} onChange={() => setRating(number)} /><span>{number}{number === 1 ? " · Poor" : number === 5 ? " · Great" : ""}</span></label>)}</fieldset><label className="feedback-label">Anything we could do better? (optional)<textarea className="feedback-comment" rows={2} maxLength={1000} disabled={status === "sending"} value={comment} onChange={(event) => setComment(event.target.value)} placeholder="Share feedback about the scheduling experience…" /></label><button className="button button-secondary small" disabled={status === "sending"}>{status === "sending" ? "Saving…" : "Send feedback"}</button>{error && <div className="feedback-error" role="alert">{error}</div>}</>}</form>;
}

function ConversationActions({ conversation, token, busy, send }: { conversation: Conversation; token: string; busy: boolean; send: (text: string, action?: Action) => Promise<void> }) {
  const { status, appointment, proposal, handoff, options } = conversation;
  const booked = status === "booked" && Boolean(appointment);
  return <div id="conversation-actions">{booked && appointment ? <div className="action-card receipt-card"><span className="receipt-icon">✓</span><h3>Your appointment is booked.</h3><AppointmentFacts slot={appointment} /><div className="status-ref">Booking reference: {appointment.appointment_id}</div></div> : handoff ? <div className="action-card handoff-card"><h3>Your request is queued for staff.</h3><p>The handoff is recorded. A queued request does not mean a staff member is connected yet.</p><p>{humanize(handoff.reason)}</p><div className="status-ref">Reference: {handoff.ticket_id} · {humanize(handoff.status)}</div></div> : ["unknown_outcome", "outcome_unknown", "reconciliation_required", "booking_pending"].includes(status) ? <div className="action-card handoff-card"><h3>We’re checking the booking outcome.</h3><p>Please don’t start a second booking. The original attempt needs to be resolved first.</p></div> : proposal ? <div className="action-card"><h3>Does this appointment work for you?</h3><p>Check the details below. Nothing is booked until you confirm this exact appointment.</p><AppointmentFacts slot={proposal.slot} /><div className="action-buttons"><button className="button button-primary" disabled={busy} onClick={() => send("Yes.", { action: "confirm", proposal_id: proposal.proposal_id })}>Confirm this appointment</button><button className="button button-secondary" disabled={busy} onClick={() => send("I want to change my preferences.")}>Change my preferences</button></div></div> : options?.length ? <div className="slot-options" aria-label="Available appointments">{options.map((slot) => <div className="slot-card" key={slot.slot_id}><div><div className="slot-date">{dateLabel(slot.start_at, slot.timezone)}</div><div className="slot-info">{slot.provider_name} · {slot.location}</div></div><button className="button button-secondary" disabled={busy} onClick={() => send("Choose this appointment.", { action: "select_slot", slot_id: slot.slot_id })}>Choose this time</button></div>)}</div> : null}{(booked || handoff) && <FeedbackForm key={conversation.conversation_id} token={token} conversationId={conversation.conversation_id} />}</div>;
}

function EvaluationResults({ report, jobId, activeVersion, activate, activationBusy }: { report: EvaluationReport; jobId: string; activeVersion: string; activate: () => Promise<void>; activationBusy: boolean }) {
  const accepted = report.accepted ?? report.candidate.accepted ?? report.gates.accepted;
  const activated = activeVersion === report.candidate.version;
  function download() {
    const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = "carepath-evaluation-report.json"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function safeLangSmithUrl(url?: string) {
    if (!url) return null;
    try { const parsed = new URL(url); return parsed.protocol === "https:" && /(^|\.)smith\.langchain\.com$/.test(parsed.hostname) ? parsed.href : null; } catch { return null; }
  }
  const traceUrl = safeLangSmithUrl(report.langsmith_url);
  return <><div className="comparison-grid">{(["baseline", "candidate"] as const).map((key) => <section className={`score-card ${key === "candidate" ? "candidate" : ""}`} key={key}><div className="card-eyebrow">{key.toUpperCase()}</div><div className="score-row"><strong>{Math.round(report[key].resolution_rate * 100)}%</strong><span>verified scenario<br />resolution</span></div><div className="score-bar"><span style={{ width: `${report[key].resolution_rate * 100}%` }} /></div><p>{report[key].passed} / {report[key].total} verified resolutions · {report[key].critical_violations} critical violations</p><span className="version-tag">{report[key].version}</span></section>)}<section className="promotion-card"><div className="card-eyebrow">PROMOTION DECISION</div><div className="promotion-icon">{accepted ? "✓" : "×"}</div><h2>{accepted ? "Candidate accepted" : "Baseline retained"}</h2><p>{accepted ? "The candidate passed the declared safety and regression gates." : "The candidate did not pass all required gates. Keep the baseline."}</p><div className="gate-list">{report.gates.checks.map((gate) => <div className={`gate ${gate.passed ? "" : "bad"}`} key={gate.name} title={gate.detail}><span className="gate-symbol">{gate.passed ? "✓" : "×"}</span><span>{humanize(gate.name)}</span></div>)}</div></section></div>{accepted && <div className="activation-card"><div><strong>{activated ? "This version is active for new conversations." : "Ready for a deliberate rollout."}</strong><p>Existing conversations keep their pinned behavior. Evaluation never changes the running service by itself.</p></div><button className="button button-primary" disabled={activated || activationBusy || !jobId} onClick={activate}>{activated ? "Active for new chats" : activationBusy ? "Activating…" : "Use accepted version for new chats"}</button></div>}<section className="queue-card scenario-card"><div className="section-header"><h2>Scenario comparison</h2><div className="report-actions"><button className="text-button" onClick={download}>Download report ↓</button>{traceUrl && <a className="text-button" href={traceUrl} target="_blank" rel="noopener noreferrer">Open LangSmith ↗</a>}</div></div><div className="table-scroll"><table><thead><tr><th>Scenario</th><th>Baseline</th><th>Candidate</th><th>Observed result</th></tr></thead><tbody>{report.scenarios.map((scenario) => <tr key={scenario.id}><td>{scenario.name || humanize(scenario.id)}</td><td><span className={`tag ${scenario.baseline ? "good" : "bad"}`}>{scenario.baseline ? "Pass" : "Fail"}{scenario.total ? ` · ${scenario.baseline_passed}/${scenario.total}` : ""}</span></td><td><span className={`tag ${scenario.candidate ? "good" : "bad"}`}>{scenario.candidate ? "Pass" : "Fail"}{scenario.total ? ` · ${scenario.candidate_passed}/${scenario.total}` : ""}</span></td><td>{scenario.detail}</td></tr>)}</tbody></table></div><div className="report-note">Run: {dateLabel(report.generated_at)}. {report.dataset.repetitions} repetition(s) per scenario. {report.note || report.limitations.join(" ")}</div></section><section className="queue-card artifact-card"><div className="section-header"><h2>The structured improvement</h2><span className="pill subtle">Versioned · bounded · reviewable</span></div><p>The candidate changes a permitted recovery behavior. It cannot relax authorization, confirmation, or clinical safeguards.</p><pre>{JSON.stringify(report.improvement, null, 2)}</pre></section></>;
}

export default function Carepath() {
  const [view, setView] = useState<View>("patient");
  const [config, setConfig] = useState<Config | null>(null);
  const [patientKey, setPatientKey] = useState("patient-maya");
  const [token, setToken] = useState("");
  const staffToken = useRef("");
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(true);
  const lock = useRef(false);
  const [globalError, setGlobalError] = useState("");
  const [chatError, setChatError] = useState("");
  const scroll = useRef<HTMLDivElement>(null);
  const [handoffs, setHandoffs] = useState<Handoff[]>([]);
  const [staffError, setStaffError] = useState("");
  const [staffBusy, setStaffBusy] = useState(false);
  const [evalJob, setEvalJob] = useState<EvaluationJob | null>(null);
  const [evalBusy, setEvalBusy] = useState(false);
  const [evalError, setEvalError] = useState("");
  const [repetitions, setRepetitions] = useState<1 | 3>(1);
  const [activationBusy, setActivationBusy] = useState(false);
  const evalLock = useRef(false);
  const alive = useRef(true);

  function remember(patient: string, id: string) { try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ patient, id })); } catch { /* Storage is optional. */ } }
  async function startPatient(patient: string, restoreId?: string, signal?: AbortSignal) {
    lock.current = true; setBusy(true); setChatError(""); setPatientKey(patient); setConversation(null); setMessages([]);
    try {
      const session = await api<Session>("/api/sessions", { method: "POST", body: { patient_key: patient, role: "patient" }, signal });
      let record: Conversation;
      if (restoreId) {
        try { record = await api<Conversation>(`/api/conversations/${restoreId}`, { token: session.token, signal }); }
        catch (error) { if (error instanceof ApiError && [403, 404].includes(error.status)) record = await api<Conversation>("/api/conversations", { method: "POST", body: {}, token: session.token, signal }); else throw error; }
      } else record = await api<Conversation>("/api/conversations", { method: "POST", body: {}, token: session.token, signal });
      if (signal?.aborted) return;
      setToken(session.token); setConversation(record); setMessages(record.messages?.length ? record.messages : record.reply ? [{ role: "assistant", content: record.reply }] : []); remember(patient, record.conversation_id);
    } catch (error) { if (!(error instanceof Error && error.name === "AbortError")) setChatError(errorText(error)); }
    finally { if (!signal?.aborted) { lock.current = false; setBusy(false); } }
  }

  useEffect(() => {
    const controller = new AbortController(); alive.current = true;
    (async () => {
      try {
        const info = await api<Config>("/api/config", { signal: controller.signal });
        if (controller.signal.aborted) return;
        setConfig(info);
        let saved: { patient?: string; id?: string } | null = null;
        try { saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null"); } catch { /* Ignore malformed local storage. */ }
        const patient = info.patients.some((item) => item.id === saved?.patient) ? saved!.patient! : info.patients[0]?.id || "patient-maya";
        await startPatient(patient, patient === saved?.patient ? saved?.id : undefined, controller.signal);
      } catch (error) { if (!controller.signal.aborted) { setGlobalError(errorText(error)); setBusy(false); } }
    })();
    const readHash = () => { const hash = location.hash.slice(1); setView(["patient", "staff", "evaluation"].includes(hash) ? hash as View : "patient"); };
    readHash(); window.addEventListener("hashchange", readHash);
    return () => { controller.abort(); alive.current = false; window.removeEventListener("hashchange", readHash); };
    // Initialization uses a fresh synthetic session. Tokens stay in memory.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => { scroll.current?.scrollTo({ top: scroll.current.scrollHeight, behavior: "auto" }); }, [messages, conversation, busy]);

  async function send(text: string, action: Action = {}) {
    if (!text.trim() || lock.current || !conversation || !token) return;
    lock.current = true; setBusy(true); setChatError(""); setInput(""); setMessages((existing) => [...existing, { role: "user", content: text.trim() }]);
    try {
      const result = await api<Conversation>(`/api/conversations/${conversation.conversation_id}/messages`, { method: "POST", token, body: { text: text.trim(), turn_id: crypto.randomUUID(), ...action } });
      setConversation(result); setMessages((existing) => [...existing, { role: "assistant", content: result.reply }]);
    } catch (error) { setChatError(`${errorText(error)} The message will not be automatically repeated. Refresh the conversation to check its current state.`); }
    finally { lock.current = false; setBusy(false); }
  }
  async function refreshConversation() {
    if (lock.current || !conversation) return;
    lock.current = true; setBusy(true);
    try { const result = await api<Conversation>(`/api/conversations/${conversation.conversation_id}`, { token }); setConversation(result); setMessages(result.messages || []); setChatError(""); }
    catch (error) { setChatError(errorText(error)); }
    finally { lock.current = false; setBusy(false); }
  }
  const ensureStaff = useCallback(async () => {
    if (staffToken.current) return staffToken.current;
    const session = await api<Session>("/api/sessions", { method: "POST", body: { patient_key: "patient-maya", role: "staff" } });
    staffToken.current = session.token; return session.token;
  }, []);
  const loadHandoffs = useCallback(async () => {
    setStaffBusy(true); setStaffError("");
    try { const staff = await ensureStaff(); const data = await api<Handoff[] | { handoffs: Handoff[] }>("/api/handoffs", { token: staff }); setHandoffs(Array.isArray(data) ? data : data.handoffs); }
    catch (error) { setStaffError(errorText(error)); }
    finally { setStaffBusy(false); }
  }, [ensureStaff]);
  const loadLatest = useCallback(async () => {
    try { const staff = await ensureStaff(); const result = await api<EvaluationJob | null>("/api/evaluations/latest", { token: staff }); if (result?.report) setEvalJob(result); }
    catch (error) { if (!(error instanceof ApiError && error.status === 404)) setEvalError(errorText(error)); }
  }, [ensureStaff]);
  useEffect(() => { if (!config) return; if (view === "staff") void loadHandoffs(); if (view === "evaluation" && !evalLock.current) void loadLatest(); }, [view, config, loadHandoffs, loadLatest]);
  async function updateHandoff(id: string, status: "accepted" | "resolved") {
    setStaffBusy(true);
    try { const staff = await ensureStaff(); await api(`/api/handoffs/${id}`, { method: "PATCH", token: staff, body: { status } }); await loadHandoffs(); }
    catch (error) { setStaffError(errorText(error)); setStaffBusy(false); }
  }
  async function runEvaluation() {
    if (evalLock.current) return;
    evalLock.current = true; setEvalBusy(true); setEvalError("");
    try {
      const staff = await ensureStaff();
      const created = await api<EvaluationJob>("/api/evaluations/run", {
        method: "POST", token: staff,
        body: {
          repetitions, online: false,
          interpreter: config?.evaluation_interpreter || "demo",
          model_name: config?.evaluation_model_name || undefined,
        },
      });
      setEvalJob(created);
      for (let attempt = 0; attempt < 900 && alive.current; attempt += 1) {
        const job = await api<EvaluationJob>(`/api/evaluations/jobs/${created.job_id}`, { token: staff });
        setEvalJob(job);
        if (["completed", "complete", "succeeded", "done"].includes(job.status)) { if (!job.report) throw new Error("The evaluation finished without a report."); return; }
        if (["failed", "error", "cancelled"].includes(job.status)) throw new Error(job.error || "Evaluation did not complete. No behavior was activated.");
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
      if (alive.current) throw new Error("The evaluation is still running. Revisit the lab to load its result.");
    } catch (error) { if (alive.current) setEvalError(errorText(error)); }
    finally { evalLock.current = false; if (alive.current) setEvalBusy(false); }
  }
  async function activate() {
    if (!evalJob?.report || activationBusy) return;
    setActivationBusy(true); setEvalError("");
    try { const staff = await ensureStaff(); await api("/api/behavior/activate", { method: "POST", token: staff, body: { version: evalJob.report.candidate.version, job_id: evalJob.job_id } }); setConfig(await api<Config>("/api/config")); }
    catch (error) { setEvalError(errorText(error)); }
    finally { setActivationBusy(false); }
  }

  const patients = config?.patients || DEFAULT_PATIENTS;
  const patientName = patients.find((patient) => patient.id === patientKey)?.name || "Demo patient";
  const stage = conversation?.status === "booked" ? 3 : conversation?.proposal ? 2 : conversation?.options?.length ? 1 : 0;
  const preferences = conversation?.constraints;
  const hasPreferences = Boolean(preferences?.appointment_type || preferences?.date || preferences?.after_hour !== null && preferences?.after_hour !== undefined);
  const report = evalJob?.report;
  const hasPatientMessages = messages.some((message) => ["user", "human", "patient"].includes(message.role));
  const live = config?.mode !== undefined && !config.mode.toLowerCase().includes("demo") && config.mode !== "deterministic";
  const evaluationActor = report ? report.engine.actor === "langchain-model" ? "LangChain model" : "deterministic demo actor" : config?.evaluation_interpreter === "live" ? "LangChain model" : "deterministic demo actor";
  const hour = (value: number) => `${value % 12 || 12} ${value >= 12 ? "PM" : "AM"}`;

  return <div className="app-shell"><aside className="sidebar" aria-label="Main navigation"><a className="brand" href="#patient"><span className="brand-symbol"><Icon kind="cross" /></span><span>carepath<span className="brand-caption">A LITTLE LESS COMPLICATED</span></span></a><div className="nav-label">WORKSPACE</div><nav>{([{ id: "patient", title: "Patient chat", icon: "chat" }, { id: "staff", title: "Staff handoffs", icon: "staff" }, { id: "evaluation", title: "Evaluation lab", icon: "lab" }] as const).map((item) => <a key={item.id} href={`#${item.id}`} className={`nav-item ${view === item.id ? "active" : ""}`} aria-current={view === item.id ? "page" : undefined}><Icon kind={item.icon} /><span>{item.title}</span>{item.id === "patient" && <span className="nav-dot" />}{item.id === "staff" && handoffs.some((handoff) => handoff.status !== "resolved") && <span className="nav-count">{handoffs.filter((handoff) => handoff.status !== "resolved").length}</span>}</a>)}</nav><div className="sidebar-guide"><span className="guide-eyebrow">BUILT WITH INTENTION</span><p>Clear choices.<br />Careful confirmations.<br />A human when you need one.</p><span className="sidebar-runtime">Next.js frontend · Python agent</span></div><div className="sidebar-footer"><span className="demo-marker" /><div>Synthetic clinic<span>Local demonstration</span></div><span className="version">{config?.active_behavior_version || "v1"}</span></div></aside><div className="main-shell"><header className="topbar"><div className="breadcrumb">Carepath <span>/</span><span>{{ patient: "Patient experience", staff: "Staff workspace", evaluation: "Evaluation lab" }[view]}</span></div><div className="topbar-right"><span className={`connection ${config ? "online" : globalError ? "offline" : ""}`}><span />{config ? "Local services online" : globalError ? "Service unavailable" : "Connecting"}</span><span className="avatar">CP</span></div></header><main><div className="global-notice"><Icon kind="info" /><span>This is a local demo with fictional patients and appointments. Please don’t enter real patient information.</span><span className="notice-label">SANDBOX</span></div>{globalError && <div className="alert alert-error" role="alert">{globalError} Refresh this page after the local services are running.</div>}
    {view === "patient" && <section aria-labelledby="patient-title"><div className="page-heading"><div><div className="eyebrow">YOUR NEXT APPOINTMENT, SIMPLIFIED</div><h1 id="patient-title">A calmer path to care.</h1><p>Tell us what works for you. We’ll find a time together.</p></div><button className="button button-secondary small" disabled={busy || !config} onClick={() => startPatient(patientKey)}>＋ New conversation</button></div><div className="patient-grid"><section className="chat-card" aria-label="Appointment conversation"><div className="chat-header"><div className="assistant-mark"><Icon kind="cross" /></div><div><h2>Appointment assistant</h2><p><span className="online-dot" />Here to help you book</p></div><span className="pill">{humanize(conversation?.status || (busy ? "Connecting" : "Ready"))}</span></div><div className="chat-scroll" ref={scroll}>{!hasPatientMessages && <div className="chat-intro"><span className="intro-art"><Icon kind="calendar" /></span><h3>Let’s find your next appointment.</h3><p>{conversation?.reply || "Share the type of visit you need and a day or time that suits you. You can change your mind along the way."}</p></div>}<div role="log" aria-live="polite" aria-relevant="additions">{(hasPatientMessages ? messages : []).map((message, index) => { const user = ["user", "human", "patient"].includes(message.role); return <div className={`message ${user ? "user" : "assistant"}`} key={`${conversation?.conversation_id}-${index}`}><span className="message-avatar" aria-hidden="true">{user ? patientName.split(" ").map((part) => part[0]).slice(0, 2).join("") : "+"}</span><div className="message-body"><div className="message-bubble">{message.content}</div><div className="message-meta">{user ? "You" : "Appointment assistant"}</div></div></div>; })}</div>{!hasPatientMessages && <div className="starter-prompts">{[{ text: "Book a routine checkup", prompt: "I need routine primary care tomorrow after 3 PM." }, { text: "Explore appointment types", prompt: "What appointment types can I book?" }, { text: "Ask for a person", prompt: "I'd like to speak with a person." }].map((item) => <button key={item.text} disabled={busy || !conversation} onClick={() => send(item.prompt)}>{item.text}<span>↗</span></button>)}</div>}{conversation && <ConversationActions conversation={conversation} token={token} busy={busy} send={send} />}{busy && <div className="typing" role="status" aria-label="Assistant is responding"><span /><span /><span /></div>}</div><div className="composer-wrap">{chatError && <div className="composer-error" role="alert">{chatError} <button type="button" onClick={refreshConversation} disabled={busy || !conversation}>Refresh conversation</button></div>}<form className="composer" onSubmit={(event) => { event.preventDefault(); void send(input); }}><label className="sr-only" htmlFor="message-input">Your message</label><textarea id="message-input" rows={1} maxLength={2000} disabled={busy || !conversation} value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(input); } }} placeholder="Tell us what you’re looking for…" /><button className="send-button" type="submit" disabled={busy || !conversation || !input.trim()} aria-label="Send message"><Icon kind="send" /></button></form><div className="composer-caption"><Icon kind="shield" />Booking only happens after you confirm the exact appointment.</div></div></section><aside className="patient-context"><section className="context-card identity-card"><div className="card-eyebrow">DEMO PATIENT</div><label htmlFor="patient-select">You’re chatting as</label><div className="select-wrap"><select id="patient-select" value={patientKey} disabled={busy || !config} onChange={(event) => startPatient(event.target.value)}>{patients.map((patient) => <option value={patient.id} key={patient.id}>{patient.name}</option>)}</select></div><div className="identity-detail"><span className="check-circle">✓</span><span>Synthetic identity selected</span></div><p>Changing patients starts a separate conversation. This is mock authentication.</p></section><section className="context-card"><div className="card-eyebrow">A FEW SIMPLE STEPS</div><ol className="journey">{[{ title: "Tell us what you need", description: "Visit type and your preferences" }, { title: "Choose an appointment", description: "Options from the demo clinic" }, { title: "Check and confirm", description: "You always have the final say" }, { title: "You’re all set", description: "A verified booking receipt" }].map((step, index) => <li key={step.title} className={stage === index ? "current" : stage > index ? "completed" : ""}><span>{stage > index ? "✓" : index + 1}</span><div>{step.title}<small>{step.description}</small></div></li>)}</ol></section>{hasPreferences && <section className="context-card request-card"><div className="card-eyebrow">YOUR CURRENT REQUEST</div><dl className="request-facts">{preferences?.appointment_type && <><dt>Visit</dt><dd>{humanize(preferences.appointment_type)}</dd></>}{preferences?.date && <><dt>Day</dt><dd>{preferences.date}</dd></>}{preferences?.after_hour !== undefined && preferences.after_hour !== null && <><dt>Time</dt><dd>After {hour(preferences.after_hour)}</dd></>}{preferences?.before_hour !== undefined && preferences.before_hour !== null && <><dt>Before</dt><dd>{hour(preferences.before_hour)}</dd></>}</dl><p>You can update these preferences in the chat.</p></section>}<section className="context-card reassurance-card"><div className="reassurance-icon"><Icon kind="shield" /></div><h3>Your choices stay yours.</h3><p>We ask before booking, keep your latest preferences, and involve staff when something needs their help.</p><span className="model-label">{live ? "LangChain model · synthetic clinic" : "Deterministic demo · LangGraph workflow"}<br />Conversation behavior: {conversation?.behavior_version || config?.active_behavior_version || "v1"}</span></section></aside></div></section>}
    {view === "staff" && <section aria-labelledby="staff-title"><div className="page-heading"><div><div className="eyebrow">HUMAN HELP, WITH CONTEXT</div><h1 id="staff-title">The handoff desk.</h1><p>Review what the patient needs and pick up where the assistant paused.</p></div><button className="button button-secondary small" disabled={staffBusy} onClick={loadHandoffs}>Refresh queue ↻</button></div><div className="staff-notice"><span className="check-circle">✓</span>Demo staff access. Patient tokens cannot read this queue.</div><div className="stat-grid">{([{ status: "queued", label: "WAITING FOR STAFF", detail: "Ready for a human to review" }, { status: "accepted", label: "IN REVIEW", detail: "Being handled by the team" }, { status: "resolved", label: "RESOLVED", detail: "Completed handoffs" }] as const).map((item) => <div className="stat-card" key={item.status}><span>{item.label}</span><strong>{handoffs.filter((handoff) => handoff.status === item.status).length}</strong><small>{item.detail}</small></div>)}</div>{staffError && <div className="alert alert-error" role="alert">{staffError}</div>}<section className="queue-card"><div className="section-header"><h2>Patient handoffs</h2><span className="muted">Most recent first</span></div>{handoffs.length === 0 ? <div className="empty-state"><span className="empty-icon">✓</span><h3>{staffBusy ? "Checking the handoff queue" : "No handoffs waiting."}</h3><p>Requests appear when a patient asks for a person or the assistant needs staff assistance.</p></div> : handoffs.map((handoff) => <article className="handoff-row" key={handoff.ticket_id}><div><h3>{handoff.patient_name || patients.find((patient) => patient.id === handoff.patient_id)?.name || "Demo patient"}<span className={`tag ${handoff.status === "resolved" ? "good" : handoff.status === "accepted" ? "review" : ""}`}>{handoff.status === "accepted" ? "In review" : humanize(handoff.status)}</span></h3><p>{handoff.summary}</p><div className="handoff-meta">{handoff.ticket_id} · {humanize(handoff.destination)} · {humanize(handoff.reason)} · {dateLabel(handoff.created_at)}</div></div><div className="handoff-buttons">{handoff.status === "queued" && <button className="button button-secondary small" disabled={staffBusy} onClick={() => updateHandoff(handoff.ticket_id, "accepted")}>Start review</button>}{handoff.status !== "resolved" && <button className="button button-primary small" disabled={staffBusy} onClick={() => updateHandoff(handoff.ticket_id, "resolved")}>Mark resolved</button>}</div></article>)}</section></section>}
    {view === "evaluation" && <section aria-labelledby="evaluation-title"><div className="page-heading"><div><div className="eyebrow">LEARN FROM EVERY RUN</div><h1 id="evaluation-title">Better, with evidence.</h1><p>Test complete conversations, inspect failures, and verify a careful improvement.</p></div><div className="evaluation-controls"><label className="sr-only" htmlFor="repetitions">Repetitions per scenario</label><select id="repetitions" value={repetitions} disabled={evalBusy} onChange={(event) => setRepetitions(Number(event.target.value) as 1 | 3)}><option value={1}>1 run per scenario</option><option value={3}>3 runs per scenario</option></select><button className="button button-primary" disabled={evalBusy || !config} onClick={runEvaluation}><Icon kind="play" />{evalBusy ? "Running…" : "Run improvement loop"}</button></div></div><div className="eval-flow">{[{ title: "Run the scenarios", detail: "Same agent. Isolated fake clinic." }, { title: "Find the failure", detail: "Verify actual side effects." }, { title: "Test an improvement", detail: "Refresh and ask for fresh consent." }, { title: "Check the whole suite", detail: "Safety and regression gates." }].map((step, index) => <div key={step.title}><span className="flow-number">0{index + 1}</span><strong>{step.title}</strong><small>{step.detail}</small></div>)}</div><div className="eval-runtime"><div><span className="demo-marker" /><span>LangSmith SDK evaluation · {report?.engine.upload_results ? "cloud results enabled" : "local results; cloud upload off"} · {evaluationActor}</span></div><span className="pill">{evalBusy ? "Running" : report ? "Completed" : "Not run yet"}</span></div>{evalError && <div className="alert alert-error" role="alert">{evalError}</div>}{evalBusy && <div className="eval-progress" role="status"><div className="spinner" /><div><strong>{evalJob?.message || (evalJob?.phase ? humanize(evalJob.phase) : "Running the improvement loop")}</strong><p>Baseline and candidate runs use separate, isolated scheduling databases.</p></div></div>}{report ? <EvaluationResults report={report} jobId={evalJob!.job_id} activeVersion={config?.active_behavior_version || "v1"} activate={activate} activationBusy={activationBusy} /> : !evalBusy && <div className="empty-state eval-empty"><div className="empty-chart" aria-hidden="true"><span /><span /><span /><span /><span /></div><h2>Every improvement has to earn its place.</h2><p>Run the baseline, diagnose the slot-conflict failure, test a versioned candidate, and compare the results. Booking safety stays enforced throughout.</p><span className="pill subtle">Evaluation through LangSmith · no external publishing</span></div>}</section>}
  </main><footer className="page-footer"><span>Thoughtful scheduling. Verifiable outcomes.</span><span>Next.js · Python · LangGraph · LangChain · LangSmith</span></footer></div></div>;
}
