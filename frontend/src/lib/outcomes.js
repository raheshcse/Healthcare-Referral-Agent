export const TONES = { success: "success", warning: "warning", danger: "danger", neutral: "neutral" };
export const STAGES = [{ key: "request", label: "Request received" }, { key: "patient", label: "Patient resolved" }, { key: "validation", label: "Details validated" }, { key: "complete", label: "Referral created" }];
export const ACTION_STAGES = STAGES;
export const REVIEW_STAGES = [{ key: "request", label: "Request received" }, { key: "patient", label: "Patient resolved" }, { key: "analysis", label: "Record reviewed" }, { key: "complete", label: "Review completed" }];
const object = (value) => value && typeof value === "object" && !Array.isArray(value);
function present(result, type = "referral") {
  if (!object(result)) throw new MalformedResponseError();
  const success = result.success === true;
  return { type, success, status: result.status || "INTERNAL_ERROR", message: result.message || "The request could not be completed.", referral: result.referral || null, patient: result.patient || null, candidates: Array.isArray(result.candidates) ? result.candidates : [], stages: [], tone: success ? TONES.success : TONES.danger };
}
export function presentWorkflowResult(result) { return present(result); }
export function presentActionResult(result) { return present(result, "action"); }
export function presentClinicalReview(result) { return present(result, "review"); }
export class MalformedResponseError extends Error { constructor() { super("Malformed service response"); this.name = "MalformedResponseError"; } }
export function interpretChatResponse(body) {
  if (!object(body)) throw new MalformedResponseError();
  const outcomes = Array.isArray(body.workflow_results) ? body.workflow_results.map(presentWorkflowResult) : [];
  const reviews = Array.isArray(body.clinical_reviews) ? body.clinical_reviews.map(presentClinicalReview) : [];
  const actions = Array.isArray(body.action_results) ? body.action_results.map(presentActionResult) : [];
  return { kind: outcomes.length || reviews.length || actions.length ? "referral" : "message", text: body.reply || "", outcomes: [...outcomes, ...actions], reviews, conversationId: body.conversation_id || null, context: presentConversationContext(body.context) };
}
export function presentConversationContext(context) { return object(context) ? context : null; }
export const SERVICE_ERRORS = { NETWORK: "The service could not be reached.", TIMEOUT: "The request timed out.", LLM_UNAVAILABLE: "The AI service is unavailable.", MALFORMED_RESPONSE: "The service returned an unexpected response.", UNEXPECTED: "An unexpected error occurred." };
export function classifyFailure({ network = false, timeout = false, errorCode } = {}) { if (network) return "NETWORK"; if (timeout) return "TIMEOUT"; if (errorCode === "LLM_UNAVAILABLE") return "LLM_UNAVAILABLE"; return "UNEXPECTED"; }
