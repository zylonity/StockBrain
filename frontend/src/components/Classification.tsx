import type {
  CompanyImpact,
  EventSummary,
  ImpactDirection,
  LlmCall,
  LlmUsage,
} from "../api/types";
import { formatScore, formatTimestamp } from "./formats";

const DIRECTION_LABEL: Record<ImpactDirection, string> = {
  POSITIVE: "▲ positive",
  NEGATIVE: "▼ negative",
  MIXED: "◆ mixed",
  UNKNOWN: "· unknown",
};

/** A 0–1 model score, rendered as a bar. Explicitly not a probability. */
export function ScoreBar({ value, label }: { value: number | null; label: string }) {
  const pct = value === null ? 0 : Math.round(Math.max(0, Math.min(1, value)) * 100);
  return (
    <div className="score">
      <div className="score-head">
        <span>{label}</span>
        <span className="mono">{formatScore(value)}</span>
      </div>
      <div className="score-track" role="presentation">
        <div className="score-fill" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

export function DirectionBadge({ direction }: { direction: ImpactDirection }) {
  return (
    <span className={`badge badge-dir-${direction}`}>{DIRECTION_LABEL[direction]}</span>
  );
}

export function CompanyImpactTable({ companies }: { companies: CompanyImpact[] }) {
  if (companies.length === 0) {
    return (
      <div className="placeholder">
        No affected companies identified for this event.
      </div>
    );
  }
  return (
    <>
      <table>
        <thead>
          <tr>
            <th>Company</th>
            <th>Ticker hint</th>
            <th>Path</th>
            <th>Direction</th>
            <th>Materiality</th>
            <th>Confidence</th>
          </tr>
        </thead>
        <tbody>
          {companies.map((company) => (
            <tr key={company.id}>
              <td>
                {company.company_name_hint}
                {company.relationship_type && (
                  <div className="detail">{company.relationship_type}</div>
                )}
              </td>
              <td className="mono">
                {company.ticker_hint ?? <span className="faint">unresolved</span>}
                {company.exchange_hint && (
                  <div className="detail">{company.exchange_hint}</div>
                )}
              </td>
              <td>
                <span className={`badge badge-path-${company.impact_path}`}>
                  {company.impact_path}
                </span>
              </td>
              <td>
                <DirectionBadge direction={company.direction} />
              </td>
              <td className="mono">{formatScore(company.materiality_score)}</td>
              <td className="mono">{formatScore(company.confidence)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="metric-note" style={{ marginTop: 10 }}>
        Ticker hints are the classifier's suggestion, not a resolved instrument.
        Matching a company to a tradable broker instrument is a separate,
        verified step before any order can be sized.
      </p>
    </>
  );
}

export function ClassificationPanel({ event }: { event: EventSummary }) {
  if (event.classifier_error) {
    return (
      <div className="card">
        <h2>Classification</h2>
        <div className="error" style={{ marginBottom: 0 }}>
          Classification failed: {event.classifier_error}
        </div>
      </div>
    );
  }

  if (!event.classified_at) {
    return (
      <div className="card">
        <h2>Classification</h2>
        <div className="placeholder">
          Awaiting classification. Events are classified out of band by a
          background worker.
        </div>
      </div>
    );
  }

  return (
    <div className="card">
      <h2>Classification</h2>
      <ScoreBar value={event.importance_score} label="Importance" />
      <ScoreBar value={event.novelty_score} label="Novelty" />
      <ScoreBar value={event.confidence_score} label="Confidence" />
      <dl className="kv" style={{ marginTop: 12 }}>
        <dt>Relevant to equities</dt>
        <dd>{event.relevant_to_public_equities ? "yes" : "no"}</dd>
        <dt>Needs corroboration</dt>
        <dd>{event.needs_corroboration ? "yes" : "no"}</dd>
        <dt>Candidate score</dt>
        <dd>{formatScore(event.candidate_score)}</dd>
        <dt>Classified</dt>
        <dd>{formatTimestamp(event.classified_at)}</dd>
        <dt>Model</dt>
        <dd>{event.classifier_model ?? "—"}</dd>
        <dt>Prompt</dt>
        <dd>{event.classifier_prompt_version ?? "—"}</dd>
      </dl>
      <p className="metric-note">
        Scores rank where to spend further analysis. They are not calibrated
        probabilities and are not trading signals.
      </p>
    </div>
  );
}

export function LlmUsagePanel({
  usage,
  calls,
}: {
  usage: LlmUsage;
  calls: LlmCall[];
}) {
  const cost = Number(usage.estimated_cost_usd || 0);
  return (
    <div className="card">
      <h2>Model usage</h2>
      <div className="metric">${cost.toFixed(4)}</div>
      <div className="metric-note">
        estimated · {usage.calls} call{usage.calls === 1 ? "" : "s"} ·{" "}
        {usage.input_tokens.toLocaleString()} in ({usage.cached_input_tokens.toLocaleString()}{" "}
        cached) / {usage.output_tokens.toLocaleString()} out
      </div>
      {calls.length > 0 && (
        <table style={{ marginTop: 12 }}>
          <thead>
            <tr>
              <th>Purpose</th>
              <th>Model</th>
              <th>Prompt</th>
              <th>Result</th>
              <th>Latency</th>
            </tr>
          </thead>
          <tbody>
            {calls.map((call) => (
              <tr key={call.id}>
                <td className="mono">{call.purpose}</td>
                <td className="mono detail">{call.model}</td>
                <td className="mono detail">{call.prompt_version ?? "—"}</td>
                <td>
                  {call.succeeded ? (
                    <span className="badge badge-ok">
                      ok{call.used ? " · used" : ""}
                    </span>
                  ) : (
                    <span className="badge badge-bad" title={call.error ?? undefined}>
                      {call.error_class ?? "failed"}
                    </span>
                  )}
                  {call.retry_count > 0 && (
                    <span className="faint mono"> ·{call.retry_count} retries</span>
                  )}
                </td>
                <td className="mono detail">
                  {call.latency_ms === null ? "—" : `${call.latency_ms}ms`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="metric-note">
        Cost is estimated from recorded token counts and configured rates, for
        telemetry only. Hidden model reasoning is never stored or displayed.
      </p>
    </div>
  );
}
