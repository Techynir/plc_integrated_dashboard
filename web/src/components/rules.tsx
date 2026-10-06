import { Chip } from "./console";

export interface Explain {
  what: string;
  why: string;
  next: string;
}

/** One finding under a graph: what happened, why (with numbers), and the next action. */
export interface Insight extends Explain {
  level: "now" | "warn" | "info" | "ok";
}

/** Times a process rule was active (shaded on the moisture chart). */
export interface FindingSpan {
  start: number; // epoch seconds
  end: number | null;
}

/** "Why" and "Next action" lines under an alarm raised by a process rule. */
export function ExplainLines({ e }: { e: Explain }) {
  return (
    <dl className="explain">
      <dt>Why</dt>
      <dd>{e.why}</dd>
      <dt>Next action</dt>
      <dd>{e.next}</dd>
    </dl>
  );
}

/** Insights shown inside a graph's panel, under the graph. */
export function Insights({ items }: { items?: Insight[] }) {
  if (!items?.length) return null;
  return (
    <div className="insights">
      {items.map((i, k) => (
        <div key={k} className={`insight ${i.level}`}>
          {i.level === "now" && <Chip cls="warn">Now</Chip>}
          <dl className="explain">
            <dt>What</dt>
            <dd>
              <b>{i.what}</b>
            </dd>
            <dt>Why</dt>
            <dd>{i.why}</dd>
            <dt>Next action</dt>
            <dd>{i.next}</dd>
          </dl>
        </div>
      ))}
    </div>
  );
}
