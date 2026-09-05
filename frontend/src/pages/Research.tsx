import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { usePolling } from "../components/usePolling";
import { formatTimestamp } from "../components/formats";

export function Research() {
  const [params] = useSearchParams();
  const eventId = params.get("event_id") ?? undefined;
  return <ResearchList key={eventId} eventId={eventId} />;
}

function ResearchList({ eventId }: { eventId: string | undefined }) {
  const { data, error, loading, refresh } = usePolling(() => api.research(eventId), 15_000);
  return <section>
    <div className="page-heading"><div><h1>Research</h1><p>Event-driven analysis and evidence. Research is advisory only.</p></div>
      <button onClick={refresh}>Refresh</button></div>
    {error && <p role="alert" className="error">{error}</p>}
    {loading && !data && <p>Loading research…</p>}
    {data?.length === 0 && <div className="card"><h2>No research runs yet</h2><p>Classified candidates enter research once their company and listing are resolved. Budget pauses and provider failures appear here when a run is queued.</p></div>}
    {data && data.length > 0 && <div className="card table-wrap"><table><thead><tr>
      <th>Company / listing</th><th>Trigger</th><th>Status</th><th>Action</th><th>As of</th><th>Cost</th>
    </tr></thead><tbody>{data.map(run => <tr key={run.id}>
      <td><Link to={`/research/${run.id}`}>{run.packet?.company.name ?? run.id}</Link><br />
        <small>{run.packet?.company.symbol} · {run.packet?.company.exchange ?? "Exchange unknown"}</small></td>
      <td>{run.packet?.title ?? "Event unavailable"}</td><td>{run.status}{run.error && <p>{run.error}</p>}</td>
      <td>{run.decision?.action ?? "—"}</td><td>{formatTimestamp(run.as_of)}</td><td>${run.estimated_cost_usd ?? "0"}</td>
    </tr>)}</tbody></table></div>}
  </section>;
}

function EvidenceLink({ url }: { url: string | null }) {
  if (!url || !/^https?:\/\//i.test(url)) return null;
  return <a href={url} target="_blank" rel="noopener noreferrer nofollow">Read source</a>;
}

export function ResearchDetail() {
  const { runId } = useParams();
  return <ResearchRecord key={runId} runId={runId ?? ""} />;
}

function ResearchRecord({ runId }: { runId: string }) {
  const { data: run, error, refresh } = usePolling(() => api.researchRun(runId), 15_000);
  if (!run) return <section><Link to="/research">← Research</Link><p role={error ? "alert" : undefined}>{error ?? "Loading research…"}</p></section>;
  const packet = run.packet;
  const decision = run.decision;
  return <section>
    <Link to="/research">← Research</Link>
    <div className="page-heading"><div><h1>{packet?.company.name ?? "Research run"}</h1><p>{run.status} · Advisory research</p></div><button onClick={refresh}>Refresh</button></div>
    {error && <p role="alert">{error}</p>}
    {run.error && <div className="card" role="status"><h2>{run.error_class}</h2><p>{run.error}</p><p>Completed or interrupted paid runs are retained. They are not automatically repeated.</p></div>}
    {packet && <div className="card"><h2>{packet.title}</h2><p>{packet.summary}</p>
      {run.event_id && <Link to={`/events/${run.event_id}`}>View triggering event</Link>}
      <p>{packet.company.symbol} · {packet.company.exchange ?? "Unknown exchange"} · {packet.company.currency ?? "Unknown currency"}</p>
      <p>ISIN: {packet.company.isin ?? "Unavailable"} · Broker listing: {packet.company.broker_ticker}</p>
      <p>Event: {formatTimestamp(packet.event_time)} · Analysis: {formatTimestamp(run.as_of)}</p>
      <p>{packet.impact_path} impact · {packet.relationship}</p><p>{packet.classifier_rationale}</p>
    </div>}
    {packet?.degradation.map((item, index) => <div className="card" key={index}><strong>{item.provider}: {item.error_class}</strong><p>{item.detail}</p></div>)}
    {decision && <>
      <div className="card"><h2>{decision.action} · {Math.round(decision.confidence * 100)}% confidence</h2><p>Horizon: {decision.horizon}. Confidence is a model ranking, not a calibrated probability.</p><p className="research-prose">{decision.thesis}</p></div>
      <div className="card"><h2>Bull case</h2><p className="research-prose">{decision.bull_case}</p><h2>Bear case</h2><p className="research-prose">{decision.bear_case}</p></div>
      {([['Catalysts', decision.catalysts], ['Risks', decision.risks], ['Invalidation conditions', decision.invalidation_conditions]] as const).map(([title, items]) =>
        <div className="card" key={title}><h2>{title}</h2>{items.length ? <ul>{items.map((item, index) => <li key={index}>{item}</li>)}</ul> : <p>None stated.</p>}</div>)}
    </>}
    {Object.entries(run.reports).map(([role, report]) => <details className="card" key={role}><summary>{role === 'manager' ? 'Research-manager synthesis' : `${role} analysis`}</summary><p className="research-prose">{report}</p></details>)}
    <div className="card"><h2>Evidence</h2>{packet?.evidence.map(item => <details key={item.source_id}>
      <summary>{item.publisher ?? "Unknown publisher"} · {item.published_at ? formatTimestamp(item.published_at) : "Publication time unknown"}{decision?.evidence_ids.includes(item.source_id) ? " · Cited" : ""}</summary>
      <p className="mono">{item.source_id}</p><EvidenceLink url={item.url} />{item.text_truncated && <p>Excerpt shown; the complete source is retained with the event.</p>}<p className="research-prose">{item.text}</p>
    </details>)}</div>
    {packet?.market_context.map((item, index) => <details className="card" key={index}><summary>{item.provider} · {item.kind} · research context</summary><pre className="research-prose">{item.text}</pre></details>)}
    <details className="card"><summary>Versions and model usage · ${run.estimated_cost_usd ?? "0"}</summary>
      <p>Quick: {run.quick_model} · Deep: {run.deep_model}</p><p>Prompt: {run.prompt_version}</p>
      <p className="mono">TradingAgents: {run.tradingagents_version}</p><p className="mono">Configuration: {run.config_version}</p>
      <p>Started: {run.started_at ? formatTimestamp(run.started_at) : "Pending"} · Completed: {run.completed_at ? formatTimestamp(run.completed_at) : "Pending"}</p>
      <div className="table-wrap"><table><thead><tr><th>Role / model</th><th>Input / output</th><th>Cache hit / miss</th><th>Latency</th><th>Cost</th><th>Result / request ID</th></tr></thead>
        <tbody>{run.calls.map(call => <tr key={call.id}><td>{call.purpose}<br />{call.provider} · {call.model}</td>
          <td>{call.input_tokens ?? 0} / {call.output_tokens ?? 0}</td><td>{call.cached_input_tokens ?? 0} / {call.cache_miss_input_tokens ?? 0}</td>
          <td>{call.latency_ms ?? 0} ms</td><td>${call.estimated_cost_usd ?? "0"}</td><td>{call.error_class ?? call.finish_reason}<br /><small>{call.provider_request_id}</small></td></tr>)}</tbody></table></div>
    </details>
  </section>;
}
