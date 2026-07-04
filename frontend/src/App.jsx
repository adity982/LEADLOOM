import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

/* ------------------------------------------------ header with global stats */
function Masthead() {
  const [stats, setStats] = useState(null);
  useEffect(() => {
    api.stats().then(setStats).catch(() => {});
    const t = setInterval(() => api.stats().then(setStats).catch(() => {}), 8000);
    return () => clearInterval(t);
  }, []);
  return (
    <header className="masthead">
      <h1>LeadLoom</h1>
      <span className="tag">grounded outreach agent</span>
      {stats && (
        <div className="stats">
          <span><b>{stats.total_leads}</b> leads</span>
          <span><b>${stats.avg_cost_per_lead}</b>/lead</span>
          <span><b>{stats.drafts_with_unsupported_claims_pct}%</b> drafts flagged</span>
          <span><b>{stats.rate_limit_hits}</b> 429s</span>
        </div>
      )}
    </header>
  );
}

/* ----------------------------------------------------------- new run form */
function NewRun({ onCreated }) {
  const [label, setLabel] = useState("");
  const [icp, setIcp] = useState("");
  const [csvText, setCsvText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const submit = async () => {
    setBusy(true); setErr("");
    try {
      // Accept either full CSV (with headers) or a bare list of domains.
      const looksLikeCsv = csvText.includes(",") && /company|domain|website|url/i.test(csvText.split("\n")[0]);
      const body = looksLikeCsv
        ? { label, icp_description: icp, csv_text: csvText }
        : { label, icp_description: icp,
            leads: csvText.split(/[\n,]/).map(s => s.trim()).filter(Boolean)
                          .map(d => ({ domain: d })) };
      const { run_id } = await api.createRun(body);
      setCsvText("");
      onCreated(run_id);
    } catch (e) { setErr(e.message); }
    setBusy(false);
  };

  const onFile = (e) => {
    const f = e.target.files?.[0];
    if (!f) return;
    const r = new FileReader();
    r.onload = () => setCsvText(String(r.result));
    r.readAsText(f);
  };

  return (
    <div className="card">
      <h2>New run</h2>
      <div className="field">
        <label htmlFor="nr-label">Label</label>
        <input id="nr-label" value={label} onChange={e => setLabel(e.target.value)} placeholder="job-hunt batch 3" />
      </div>
      <div className="field">
        <label htmlFor="nr-icp">Ideal customer profile (optional)</label>
        <input id="nr-icp" value={icp} onChange={e => setIcp(e.target.value)} placeholder="B2B SaaS, 10–200 people, has SDR team" />
      </div>
      <div className="field">
        <label htmlFor="nr-leads">Leads — paste domains, or CSV with headers</label>
        <textarea id="nr-leads" value={csvText} onChange={e => setCsvText(e.target.value)}
          placeholder={"stripe.com\nlinear.app\n\nor:\ncompany,domain,contact,role\nAcme,acme.com,Priya,Head of Sales"} />
        <input type="file" accept=".csv,text/csv" onChange={onFile} aria-label="Upload CSV" />
      </div>
      <button className="btn" onClick={submit} disabled={busy || !csvText.trim()}>
        {busy ? "Starting…" : "Start run"}
      </button>
      {err && <div className="error-note">{err}</div>}
    </div>
  );
}

/* ------------------------------------------------------------- runs list */
function RunsList({ activeId, onSelect, refreshKey }) {
  const [runs, setRuns] = useState([]);
  useEffect(() => { api.listRuns().then(setRuns).catch(() => {}); }, [refreshKey]);
  return (
    <div className="card">
      <h2>Runs</h2>
      {runs.length === 0 && <div className="empty">No runs yet. Start one above — a pasted list of domains is enough.</div>}
      {runs.map(r => (
        <div key={r.id} className="run-row" onClick={() => onSelect(r.id)}
             style={activeId === r.id ? { background: "var(--paper)" } : {}}>
          <span className="rid">#{r.id}</span>
          <span className="rlabel">{r.label || "untitled"}</span>
          <span className="rmeta">{r.completed_leads}/{r.total_leads} · ${r.total_cost_usd}</span>
          <span className={`pill ${r.status === "done" ? "done" : "working"}`}>{r.status}</span>
        </div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------ run detail */
const WORKING = ["queued", "researching", "reasoning", "scoring", "drafting", "verifying"];

function RunDetail({ runId, onOpenLead }) {
  const [run, setRun] = useState(null);
  const timer = useRef(null);

  const load = useCallback(() => {
    if (!runId) return;
    api.getRun(runId).then(setRun).catch(() => {});
  }, [runId]);

  useEffect(() => {
    load();
    timer.current = setInterval(load, 2500);
    return () => clearInterval(timer.current);
  }, [load]);

  useEffect(() => {
    if (run?.status === "done" && timer.current) clearInterval(timer.current);
  }, [run?.status]);

  if (!runId) return <div className="card"><div className="empty">Select a run to review its leads.</div></div>;
  if (!run) return <div className="card"><div className="empty">Loading…</div></div>;

  const pct = run.total_leads ? Math.round(100 * run.progress / run.total_leads) : 0;
  return (
    <div className="card">
      <h2>Run #{run.id} — {run.label || "untitled"}</h2>
      <div className="progress" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
        <div style={{ width: `${pct}%` }} />
      </div>
      <div style={{ display: "flex", gap: 10, marginBottom: 12 }}>
        <span className="num">{run.progress}/{run.total_leads} processed · ${run.total_cost_usd} total</span>
        <a className="btn ghost small" style={{ marginLeft: "auto", textDecoration: "none" }}
           href={api.exportUrl(run.id)}>Download CSV</a>
      </div>
      <table>
        <thead>
          <tr>
            <th>Company</th><th>Contact</th><th>Status</th>
            <th>ICP</th><th>Conf</th><th>Claims</th><th>Cost</th>
          </tr>
        </thead>
        <tbody>
          {run.leads.map(l => (
            <tr key={l.id} className="lead-row" onClick={() => l.status === "done" && onOpenLead(l.id)}>
              <td>{l.company_name}{l.cache_hit ? <span className="num" title="research cache hit"> ⟳</span> : ""}</td>
              <td>{l.contact_name || "—"}</td>
              <td><span className={`pill ${l.status === "done" ? "done" : l.status === "failed" ? "failed" : "working"}`}>{l.status}</span></td>
              <td className="num">{l.status === "done" ? l.icp_score : "·"}</td>
              <td className="num">{l.status === "done" ? l.confidence_score : "·"}</td>
              <td>
                {l.status !== "done" ? "·" :
                 l.fallback_used ? <span className="pill">template</span> :
                 l.unsupported_claims > 0
                   ? <span className="flag bad">{l.unsupported_claims} unsupported</span>
                   : <span className="flag ok">grounded</span>}
              </td>
              <td className="num">${l.cost_usd}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ------------------------------------------------------------ lead drawer */
function LeadDrawer({ leadId, onClose }) {
  const [lead, setLead] = useState(null);
  const [draft, setDraft] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (!leadId) return;
    setLead(null); setSaved(false);
    api.getLead(leadId).then(d => { setLead(d); setDraft(d.draft_edited || d.draft); });
  }, [leadId]);

  if (!leadId) return null;

  const save = async () => {
    await api.patchLead(leadId, { draft_edited: draft });
    setSaved(true); setTimeout(() => setSaved(false), 2000);
  };
  const toggleApprove = async () => {
    await api.patchLead(leadId, { approved: !lead.approved });
    setLead({ ...lead, approved: !lead.approved });
  };

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside className="drawer" aria-label="Lead detail">
        <button className="close" onClick={onClose} aria-label="Close">✕</button>
        {!lead ? <div className="empty">Loading…</div> : (
          <>
            <h3>{lead.company_name}</h3>
            <div className="sub">
              {lead.domain} · {lead.contact_name || "no contact"} {lead.contact_role && `(${lead.contact_role})`}
              {" "}· ICP {lead.icp_score} · confidence {lead.confidence_score} · ${lead.cost_usd}
            </div>

            {lead.angle && (
              <>
                <div className="section-label">Angle</div>
                <div>{lead.angle}</div>
                {lead.pain_hypothesis && <div style={{ color: "var(--ink-soft)", marginTop: 4 }}>{lead.pain_hypothesis}</div>}
              </>
            )}

            <div className="section-label">Draft {lead.fallback_used && "— segment template (no grounded hook found)"}</div>
            <textarea className="draft-box" value={draft} onChange={e => setDraft(e.target.value)} aria-label="Draft email" />
            <div className="drawer-actions">
              <button className="btn small" onClick={save}>Save edits</button>
              <button className="btn ghost small" onClick={toggleApprove}>
                {lead.approved ? "Unapprove" : "Approve for sending"}
              </button>
              {saved && <span className="saved-note">saved</span>}
            </div>

            {lead.verification.length > 0 && (
              <>
                <div className="section-label">Claim check — every factual claim vs the fact list</div>
                {lead.verification.map((v, i) => (
                  <div key={i} className={`claim ${v.supported ? "ok" : "bad"}`}>
                    <span className="verdict">{v.supported ? "grounded" : "unsupported"}</span>
                    <span>{v.claim}{v.supported && v.supporting_fact != null && ` — fact [${v.supporting_fact}]`}</span>
                  </div>
                ))}
              </>
            )}

            <div className="section-label">Facts ({lead.facts.length}) — the only material the draft was allowed to use</div>
            {lead.facts.length === 0 && <div className="empty">No grounded facts were found — the template fallback was used instead of inventing a hook.</div>}
            {lead.facts.map((f, i) => (
              <div key={i} className="fact">
                <div className="fclaim">[{i}] {f.claim}</div>
                <div className="fsrc"><a href={f.source_url} target="_blank" rel="noreferrer">{f.source_url}</a></div>
                {f.snippet && <div className="fsnippet">“{f.snippet.slice(0, 180)}…”</div>}
              </div>
            ))}

            <div className="section-label">Cost breakdown</div>
            {lead.llm_calls.map((c, i) => (
              <div key={i} className="cost-row">
                <span>{c.purpose} · {c.model.split("-").slice(1, 3).join("-")}</span>
                <span>{c.input_tokens}→{c.output_tokens} tok · ${c.cost_usd.toFixed(5)}</span>
              </div>
            ))}
          </>
        )}
      </aside>
    </>
  );
}

/* ------------------------------------------------------------------- app */
export default function App() {
  const [activeRun, setActiveRun] = useState(null);
  const [openLead, setOpenLead] = useState(null);
  const [refreshKey, setRefreshKey] = useState(0);

  return (
    <div className="shell">
      <Masthead />
      <div className="grid">
        <div>
          <NewRun onCreated={(id) => { setActiveRun(id); setRefreshKey(k => k + 1); }} />
          <div style={{ height: 18 }} />
          <RunsList activeId={activeRun} onSelect={setActiveRun} refreshKey={refreshKey} />
        </div>
        <RunDetail runId={activeRun} onOpenLead={setOpenLead} />
      </div>
      <LeadDrawer leadId={openLead} onClose={() => setOpenLead(null)} />
    </div>
  );
}
