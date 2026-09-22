import type { CSSProperties, ReactNode } from "react";
import type { Tone } from "../lib/format";

/** Presentational primitives only: no fetching, no state, no client-side hooks. */

const TONE_COLORS: Record<Tone, string> = {
  plain: "#e5e7eb",
  good: "#34d399",
  bad: "#f87171",
  warn: "#fbbf24",
};

const muted: CSSProperties = { color: "#7c8aa5", fontStyle: "italic" };

export function Panel({ title, endpoint, children }: { title: string; endpoint: string; children: ReactNode }) {
  return (
    <section style={{ background: "#111c31", border: "1px solid #1f2a44", borderRadius: 10, padding: "18px 20px", marginTop: 18 }}>
      <h2 style={{ margin: 0, fontSize: 15, letterSpacing: 0.6, textTransform: "uppercase", color: "#93a4c3" }}>{title}</h2>
      <p style={{ margin: "4px 0 14px", fontSize: 12, color: "#64748b", fontFamily: "ui-monospace, SFMono-Regular, Consolas, monospace" }}>{endpoint}</p>
      {children}
    </section>
  );
}

export function Notice({ tone, title, children }: { tone: Tone; title: string; children?: ReactNode }) {
  const border = TONE_COLORS[tone];
  return (
    <div style={{ border: `1px solid ${border}`, borderLeft: `5px solid ${border}`, borderRadius: 8, padding: "12px 16px", margin: "12px 0", background: "#0e1729" }}>
      <p style={{ margin: 0, fontWeight: 700, color: border }}>{title}</p>
      {children ? <div style={{ margin: "6px 0 0", fontSize: 13, color: "#c7d2e4" }}>{children}</div> : null}
    </div>
  );
}

/** Shown in place of a section's data when its request failed or was never made. */
export function Unavailable({ error }: { error: string }) {
  return (
    <Notice tone="warn" title="No data for this section">
      {error}
    </Notice>
  );
}

export function StatGrid({ children }: { children: ReactNode }) {
  return <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))", gap: 12 }}>{children}</div>;
}

export function Stat({
  label,
  value,
  fallback = "not reported",
  hint,
  tone = "plain",
}: {
  label: string;
  value: string | null;
  fallback?: string | null;
  hint?: string | null;
  tone?: Tone;
}) {
  return (
    <div style={{ background: "#0e1729", border: "1px solid #1b2540", borderRadius: 8, padding: "12px 14px" }}>
      <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.5, color: "#7c8aa5" }}>{label}</div>
      <div style={{ marginTop: 6, fontSize: 20, fontWeight: 600, color: value === null ? "#7c8aa5" : TONE_COLORS[tone] }}>
        {value === null ? <span style={{ ...muted, fontSize: 13, fontWeight: 400 }}>{fallback ?? "not reported"}</span> : value}
      </div>
      {hint ? <div style={{ marginTop: 6, fontSize: 12, color: "#64748b" }}>{hint}</div> : null}
    </div>
  );
}

export function Fields({ children }: { children: ReactNode }) {
  return <dl style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: "6px 24px", margin: 0 }}>{children}</dl>;
}

export function Field({ label, value, fallback = "not reported" }: { label: string; value: string | null; fallback?: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 12, borderBottom: "1px solid #1b2540", padding: "6px 0" }}>
      <dt style={{ color: "#93a4c3", fontSize: 13 }}>{label}</dt>
      <dd style={{ margin: 0, fontSize: 13, textAlign: "right", color: value === null ? "#7c8aa5" : "#e5e7eb" }}>
        {value === null ? <span style={muted}>{fallback}</span> : value}
      </dd>
    </div>
  );
}

export function Table({ head, rows, empty, emptyHint }: { head: string[]; rows: ReactNode[][]; empty: string; emptyHint?: string | null }) {
  if (rows.length === 0) {
    return (
      <div>
        <p style={{ ...muted, margin: 0 }}>{empty}</p>
        {emptyHint ? <p style={{ margin: "6px 0 0", fontSize: 12, color: "#64748b" }}>{emptyHint}</p> : null}
      </div>
    );
  }
  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 13 }}>
        <thead>
          <tr>
            {head.map((column) => (
              <th
                key={column}
                style={{ textAlign: "left", padding: "8px 10px", borderBottom: "1px solid #2a3550", color: "#93a4c3", fontSize: 11, textTransform: "uppercase", letterSpacing: 0.5, whiteSpace: "nowrap" }}
              >
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              {row.map((cell, column) => (
                <td key={column} style={{ padding: "8px 10px", borderBottom: "1px solid #1b2540", verticalAlign: "top" }}>
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Raw API strings rendered verbatim, for values the API does not deliver in structured form. */
export function CodeList({ items, empty }: { items: string[]; empty: string }) {
  if (items.length === 0) return <p style={{ ...muted, margin: 0 }}>{empty}</p>;
  return (
    <ul style={{ margin: 0, paddingLeft: 18, fontFamily: "ui-monospace, SFMono-Regular, Consolas, monospace", fontSize: 12, color: "#c7d2e4", wordBreak: "break-word" }}>
      {items.map((item, index) => (
        <li key={index} style={{ margin: "4px 0" }}>
          {item}
        </li>
      ))}
    </ul>
  );
}

export function SourceNote({ children }: { children: ReactNode }) {
  return <p style={{ margin: "12px 0 0", fontSize: 12, color: "#64748b" }}>{children}</p>;
}

export function Badge({ label, tone, pulse, accent }: { label: string; tone: Tone; pulse?: string; accent?: string }) {
  const color = accent ?? TONE_COLORS[tone];
  const background = accent ?? (tone === "plain" ? "#1f2a44" : color);
  const text = accent === undefined && tone === "plain" ? "#e5e7eb" : "#0b1220";
  return (
    <span
      className={pulse}
      style={{ display: "inline-block", background, color: text, border: `1px solid ${color}`, borderRadius: 6, padding: "5px 10px", fontWeight: 700, fontSize: 12, letterSpacing: 0.8, textTransform: "uppercase" }}
    >
      {label}
    </span>
  );
}
