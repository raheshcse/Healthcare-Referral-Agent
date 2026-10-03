/**
 * FastAPI client. The frontend talks to the backend only through these
 * functions; it never calls the language model or the database.
 *
 * By default requests go to "/api", which the Vite dev/preview server
 * proxies to the FastAPI backend (see vite.config.js). Set VITE_API_URL
 * to call a backend on another origin directly.
 */

import axios from "axios";

import { classifyFailure } from "./lib/outcomes.js";

const CHAT_TIMEOUT_MS = 120_000; // allows time for tool calls and OpenAI responses
const DEFAULT_TIMEOUT_MS = 15_000;

const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL || "/api",
  headers: { "Content-Type": "application/json" },
  timeout: DEFAULT_TIMEOUT_MS,
});

/** Error carrying a classified failure kind (see SERVICE_ERRORS). */
export class ApiError extends Error {
  constructor(kind, { status = null, errorCode = null } = {}) {
    super(kind);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.errorCode = errorCode;
  }
}

function toApiError(error) {
  const response = error?.response;
  const kind = classifyFailure({
    network: !response && error?.code !== "ECONNABORTED",
    timeout: error?.code === "ECONNABORTED" || error?.code === "ETIMEDOUT",
    status: response?.status,
    errorCode: response?.data?.error,
  });
  return new ApiError(kind, {
    status: response?.status ?? null,
    errorCode: response?.data?.error ?? null,
  });
}

async function request(config) {
  try {
    const response = await api.request(config);
    return response.data;
  } catch (error) {
    throw toApiError(error);
  }
}

/**
 * Request helper for endpoints whose non-2xx responses still carry a
 * meaningful, structured body (e.g. 404 DECISION_NOT_FOUND).
 */
async function requestWithBody(config) {
  try {
    const response = await api.request({ ...config, validateStatus: () => true });
    return { status: response.status, data: response.data };
  } catch (error) {
    throw toApiError(error);
  }
}

export const getHealth = () => request({ method: "get", url: "/health" });

// Phase 3: conversation_id keeps multi-turn context on the server. Omit it
// to start a new conversation; the response returns the id to reuse.
export const sendChatMessage = (message, conversationId = null) =>
  request({
    method: "post",
    url: "/chat",
    data: conversationId ? { message, conversation_id: conversationId } : { message },
    timeout: CHAT_TIMEOUT_MS,
  });

export const listGovernanceDecisions = (limit = 25) =>
  request({ method: "get", url: "/governance", params: { limit } });

export const getGovernanceDecision = (decisionId) =>
  requestWithBody({ method: "get", url: `/governance/${encodeURIComponent(decisionId)}` });

// Phase 2: governed clinical review workflow. Non-2xx responses still carry
// the structured workflow result (e.g. 403 REVIEW_REQUIRED), so the body
// is returned together with the HTTP status.
export const runClinicalReview = (body) =>
  requestWithBody({ method: "post", url: "/clinical-workflows", data: body, timeout: CHAT_TIMEOUT_MS });

export const listClinicalWorkflows = (limit = 25) =>
  request({ method: "get", url: "/clinical-workflows", params: { limit } });

export const getClinicalWorkflow = (workflowId) =>
  requestWithBody({ method: "get", url: `/clinical-workflows/${encodeURIComponent(workflowId)}` });
