/**
 * API client for the ageneers FastAPI backend.
 *
 * Configure the backend URL via VITE_API_BASE_URL env var.
 * Defaults to http://localhost:8000 for local dev.
 *
 * If API_KEY is set in the backend's .env, pass it via VITE_API_KEY
 * and it will be sent as the X-API-Key header.
 */

const BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";
const API_KEY  = import.meta.env.VITE_API_KEY || "";

function headers(extra = {}) {
  const h = { "Content-Type": "application/json", ...extra };
  if (API_KEY) h["X-API-Key"] = API_KEY;
  return h;
}

/**
 * Submit a new task to the pipeline.
 * Returns { taskId, traceId, status, message }
 */
export async function createTask({ taskId, title, repository, branch, requirement, criteria }) {
  const description = [
    `Repository: ${repository}`,
    `Branch: ${branch || "main"}`,
    "",
    "Requirement:",
    requirement,
    "",
    "Acceptance Criteria:",
    ...criteria.filter(Boolean).map((c) => `- ${c}`),
  ].join("\n");

  const res = await fetch(`${BASE_URL}/api/tasks`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ taskId, title, description }),
  });

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || body.error || `Request failed (${res.status})`);
  }
  return res.json();
}

/** Poll the timeline for a given trace. Returns 202 + status="running" while in progress. */
export async function getTimeline(traceId) {
  const res = await fetch(`${BASE_URL}/api/tasks/${traceId}/timeline`, {
    headers: headers(),
  });
  return res.json();
}

/** Full execution report once the pipeline finishes. */
export async function getReport(traceId) {
  const res = await fetch(`${BASE_URL}/api/tasks/${traceId}/report`, {
    headers: headers(),
  });
  if (!res.ok) return null;
  return res.json();
}

/** Recent task list (newest first). */
export async function getRecentTasks(limit = 20) {
  const res = await fetch(`${BASE_URL}/api/tasks?limit=${limit}`, {
    headers: headers(),
  });
  if (!res.ok) return { tasks: [] };
  return res.json();
}

/** Aggregated pipeline + LLM metrics. */
export async function getMetrics() {
  const res = await fetch(`${BASE_URL}/api/metrics`, { headers: headers() });
  if (!res.ok) return null;
  return res.json();
}
