/** Formatting helpers. Every one of them returns `null` when the API did not report a value, so
 * the caller renders an explicit "not reported" state instead of a plausible-looking zero. */

export type Tone = "plain" | "good" | "bad" | "warn";

function grouped(fixed: string): string {
  const [whole, fraction] = fixed.split(".");
  const sign = whole.startsWith("-") ? "-" : "";
  const digits = sign === "" ? whole : whole.slice(1);
  const separated = digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return fraction === undefined ? sign + separated : `${sign}${separated}.${fraction}`;
}

/** A number as reported, with 2 decimals and thousands separators. */
export function amount(value: number | null, digits = 2, suffix?: string | null): string | null {
  if (value === null) return null;
  const rendered = grouped(value.toFixed(digits));
  return suffix ? `${rendered} ${suffix}` : rendered;
}

export function integer(value: number | null): string | null {
  return value === null ? null : grouped(value.toFixed(0));
}

/** A number exactly as the API reported it, without inventing a precision the API did not send. */
export function rawNumber(value: number | null): string | null {
  return value === null ? null : String(value);
}

/** A 0..1 ratio reported by the API, shown as a percentage. */
export function fractionPercent(value: number | null, digits = 2): string | null {
  return value === null ? null : `${(value * 100).toFixed(digits)}%`;
}

/** A value the API already reports as a percentage (risk limits, margin level). */
export function percentValue(value: number | null, digits = 2): string | null {
  return value === null ? null : `${value.toFixed(digits)}%`;
}

export function plural(value: number | null, singular: string, pluralForm: string): string | null {
  if (value === null) return null;
  return `${grouped(value.toFixed(0))} ${value === 1 ? singular : pluralForm}`;
}

export function booleanLabel(value: boolean | null, whenTrue: string, whenFalse: string): string | null {
  return value === null ? null : value ? whenTrue : whenFalse;
}

/** An ISO timestamp from the API (always UTC) rendered as an explicit UTC stamp. */
export function utcStamp(value: string | null): string | null {
  if (value === null) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed.toISOString().replace("T", " ").replace(/\.\d+Z$/, " UTC").replace("Z", " UTC");
}

export function pnlTone(value: number | null): Tone {
  if (value === null || value === 0) return "plain";
  return value > 0 ? "good" : "bad";
}
