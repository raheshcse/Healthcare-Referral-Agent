import { useEffect, useRef, useState } from "react";
import { ClipboardList, FileSearch, MessageSquarePlus, SendHorizontal, Stethoscope } from "lucide-react";

import { ApiError, runClinicalReview, sendChatMessage } from "../api.js";
import {
  MalformedResponseError,
  classifyFailure,
  interpretChatResponse,
  presentClinicalReview,
} from "../lib/outcomes.js";
import { useServiceHealth } from "../lib/useServiceHealth.js";
import {
  OutcomeCard,
  RequestStatus,
  ServiceErrorCard,
  ServiceStatusPill,
  SessionReferrals,
  ConversationContextCard,
} from "./components.jsx";
import "./clinician.css";

const EXAMPLES = [
  "Review Aisha Wiegand and determine whether a cardiology referral is appropriate.",
  "Create a cardiology referral for Aisha Wiegand because she needs a cardiology assessment.",
  "Refer Aisha Wiegand to Neurology for recurrent migraines.",
  "Show me patient information for Aisha Wiegand.",
];

const WELCOME = {
  id: "welcome",
  role: "assistant",
  type: "message",
  text:
    "Hello. I'm the Healthcare Referral Assistant. Tell me which patient to refer, the receiving department and the reason for referral.",
  welcome: true,
};

let nextId = 1;
const newId = () => `m${nextId++}`;

function idempotencyKey() {
  if (globalThis.crypto?.randomUUID) return `review-${globalThis.crypto.randomUUID()}`;
  return `review-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
}

function errorKind(error) {
  if (error instanceof ApiError) return error.kind;
  if (error instanceof MalformedResponseError) return "MALFORMED_RESPONSE";
  return "UNEXPECTED";
}

export default function ClinicianApp() {
  const [messages, setMessages] = useState([WELCOME]);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState(false);
  const [pendingKind, setPendingKind] = useState("chat");
  const [mode, setMode] = useState("chat");
  const [reviewName, setReviewName] = useState("");
  const [reviewDepartment, setReviewDepartment] = useState("");
  const [latest, setLatest] = useState(null);
  const [sessionReferrals, setSessionReferrals] = useState([]);
  // Phase 3: the server keeps multi-turn context per conversation id.
  const [conversationId, setConversationId] = useState(null);
  const [conversationContext, setConversationContext] = useState(null);
  const health = useServiceHealth();
  const listRef = useRef(null);

  useEffect(() => {
    const list = listRef.current;
    if (list) list.scrollTop = list.scrollHeight;
  }, [messages, pending]);

  async function submit(text) {
    const trimmed = text.trim();
    if (!trimmed || pending) return;

    setMessages((current) => [...current, { id: newId(), role: "user", text: trimmed }]);
    setDraft("");
    setPendingKind(/\breview\b/i.test(trimmed) ? "review" : "chat");
    setPending(true);

    let entry;
    try {
      const body = await sendChatMessage(trimmed, conversationId);
      const view = interpretChatResponse(body);
      if (view.conversationId) setConversationId(view.conversationId);
      setConversationContext(view.context);

      entry =
        view.kind === "referral"
          ? { id: newId(), role: "assistant", type: "referral", reviews: view.reviews, outcomes: view.outcomes }
          : { id: newId(), role: "assistant", type: "message", text: view.text };
    } catch (error) {
      entry = { id: newId(), role: "assistant", type: "error", kind: errorKind(error) };
    } finally {
      setPending(false);
    }

    record(trimmed, entry);
  }

  async function submitReview() {
    const name = reviewName.trim();
    const department = reviewDepartment.trim();
    if (!name || pending) return;

    const label = `Review ${name}${department ? ` for a ${department} referral` : ""}`;
    setMessages((current) => [...current, { id: newId(), role: "user", text: label }]);
    setPendingKind("review");
    setPending(true);

    let entry;
    try {
      const { status, data } = await runClinicalReview({
        patient_name: name,
        department: department || null,
        idempotency_key: idempotencyKey(),
      });

      if (data && typeof data === "object" && typeof data.workflow_id === "string" && typeof data.status === "string") {
        entry = { id: newId(), role: "assistant", type: "referral", reviews: [presentClinicalReview(data)], outcomes: [] };
      } else {
        entry = { id: newId(), role: "assistant", type: "error", kind: classifyFailure({ status, errorCode: data?.error }) };
      }
    } catch (error) {
      entry = { id: newId(), role: "assistant", type: "error", kind: errorKind(error) };
    } finally {
      setPending(false);
    }

    record(label, entry);
  }

  function record(request, entry) {
    if (entry.type === "referral") {
      const items = [...(entry.reviews || []), ...(entry.outcomes || [])];
      setSessionReferrals((current) => [
        ...items.map((outcome) => ({ id: newId(), request, outcome })),
        ...current,
      ]);
    }
    setMessages((current) => [...current, entry]);
    setLatest(entry);
  }

  function newConversation() {
    if (pending) return;
    setConversationId(null);
    setConversationContext(null);
    setMessages([WELCOME]);
    setLatest(null);
    setDraft("");
  }

  function onKeyDown(event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit(draft);
    }
  }

  const hasConversation = messages.length > 1;

  return (
    <div className="clin-shell">
      <header className="clin-header">
        <div className="clin-brand">
          <div className="clin-brand-mark" aria-hidden="true">
            <Stethoscope size={22} />
          </div>
          <div>
            <div className="clin-brand-name">Healthcare Referral Agent</div>
            <div className="clin-brand-sub">Healthcare Referral Assistant</div>
          </div>
        </div>
        <div className="clin-header-actions">
          <button
            type="button"
            className="clin-new-conversation"
            onClick={newConversation}
            disabled={pending || !hasConversation}
          >
            <MessageSquarePlus size={16} aria-hidden="true" />
            <span>New conversation</span>
          </button>
          <ServiceStatusPill health={health} />
        </div>
      </header>

      <main className="clin-main">
        <section className="clin-chat" aria-label="Referral assistant conversation">
          <div className="clin-chat-list" ref={listRef} aria-live="polite">
            {messages.map((message) => (
              <ChatMessage key={message.id} message={message} />
            ))}

            {pending && (
              <div className="clin-row">
                <div className="clin-bubble clin-bubble-assistant clin-pending">
                  <span className="clin-dots" aria-hidden="true">
                    <span />
                    <span />
                    <span />
                  </span>
                  {pendingKind === "review" ? "Reviewing the patient record…" : "Processing your request…"}
                </div>
              </div>
            )}

            {!hasConversation && !pending && (
              <div className="clin-examples">
                <div className="clin-examples-title">Try a request</div>
                {EXAMPLES.map((example) => (
                  <button key={example} type="button" onClick={() => submit(example)}>
                    {example}
                  </button>
                ))}
              </div>
            )}
          </div>

          <div className="clin-tabs" role="tablist" aria-label="Request type">
            <button type="button" role="tab" aria-selected={mode === "chat"} onClick={() => setMode("chat")}>
              Assistant
            </button>
            <button type="button" role="tab" aria-selected={mode === "review"} onClick={() => setMode("review")}>
              Patient review
            </button>
          </div>

          {mode === "chat" ? (
            <form
              className="clin-composer"
              onSubmit={(event) => {
                event.preventDefault();
                submit(draft);
              }}
            >
              <label htmlFor="clin-input" className="visually-hidden">
                Referral request
              </label>
              <textarea
                id="clin-input"
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={onKeyDown}
                placeholder="e.g. Refer Aisha Wiegand to Cardiology for persistent chest pain"
                rows={2}
                maxLength={4000}
                disabled={pending}
              />
              <button type="submit" disabled={pending || !draft.trim()}>
                <SendHorizontal size={17} aria-hidden="true" />
                <span>{pending ? "Sending" : "Send"}</span>
              </button>
            </form>
          ) : (
            <form
              className="clin-review-form"
              onSubmit={(event) => {
                event.preventDefault();
                submitReview();
              }}
            >
              <label>
                Patient name
                <input
                  id="clin-review-name"
                  value={reviewName}
                  onChange={(event) => setReviewName(event.target.value)}
                  placeholder="e.g. Aisha Wiegand"
                  maxLength={200}
                  disabled={pending}
                />
              </label>
              <label>
                Department (optional)
                <input
                  id="clin-review-department"
                  value={reviewDepartment}
                  onChange={(event) => setReviewDepartment(event.target.value)}
                  placeholder="e.g. Cardiology"
                  maxLength={100}
                  disabled={pending}
                />
              </label>
              <button type="submit" disabled={pending || !reviewName.trim()}>
                <FileSearch size={17} aria-hidden="true" />
                <span>{pending ? "Reviewing" : "Run review"}</span>
              </button>
            </form>
          )}
        </section>

        <aside className="clin-side">
          <ConversationContextCard context={conversationContext} />
          <RequestStatus latest={latest} pending={pending} pendingKind={pendingKind} />

          <section className="clin-card">
            <h2 className="clin-card-title">
              <ClipboardList size={17} aria-hidden="true" />
              Referral result
            </h2>
            {latest?.type === "referral" ? (
              [...(latest.reviews || []), ...(latest.outcomes || [])].map((outcome, index) => (
                <OutcomeCard key={index} outcome={outcome} compact />
              ))
            ) : latest?.type === "error" ? (
              <ServiceErrorCard kind={latest.kind} compact />
            ) : (
              <p className="clin-muted">
                {latest
                  ? "Nothing has been submitted from the last message. No referral was created or changed."
                  : "Referral results will appear here."}
              </p>
            )}
          </section>

          <SessionReferrals items={sessionReferrals} />
        </aside>
      </main>

      <footer className="clin-footer">
        Demonstration system using synthetic patient data (Synthea). Not for clinical use.
      </footer>
    </div>
  );
}

function ChatMessage({ message }) {
  if (message.role === "user") {
    return (
      <div className="clin-row clin-row-user">
        <div className="clin-bubble clin-bubble-user">{message.text}</div>
      </div>
    );
  }

  return (
    <div className="clin-row">
      <div className="clin-bubble clin-bubble-assistant">
        <div className="clin-bubble-label">Referral Assistant</div>

        {message.type === "referral" &&
          [...(message.reviews || []), ...(message.outcomes || [])].map((outcome, index) => (
            <OutcomeCard key={index} outcome={outcome} />
          ))}

        {message.type === "error" && <ServiceErrorCard kind={message.kind} />}

        {message.type === "message" && (
          <>
            <p className="clin-text">{message.text}</p>
            {!message.welcome && (
              <p className="clin-note">No referral or change was made from this message.</p>
            )}
          </>
        )}
      </div>
    </div>
  );
}
