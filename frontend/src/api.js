// Single place for HTTP. Set VITE_API_URL in production (Vercel env var
// pointing at the Railway backend); defaults to local dev.
const BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";

async function req(path, opts = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

export const api = {
  listRuns: () => req("/api/runs"),
  getRun: (id) => req(`/api/runs/${id}`),
  getLead: (id) => req(`/api/leads/${id}`),
  createRun: (body) => req("/api/runs", { method: "POST", body: JSON.stringify(body) }),
  patchLead: (id, body) => req(`/api/leads/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  stats: () => req("/api/stats"),
  exportUrl: (runId) => `${BASE}/api/runs/${runId}/export.csv`,
};
