// Turning an answer's text into blocks with clickable source markers, and into plain text to copy.

import type { AskResponse, Citation, Kind } from "./api";

export type Inline =
  | { type: "text"; text: string }
  | { type: "strong"; text: string }
  | { type: "marker"; marker: string };
export type Block = { type: "paragraph"; parts: Inline[] } | { type: "list"; items: Inline[][] };

const MARKER = /\[(S\d+)\]/g;
const STRONG = /\*\*(.+?)\*\*/g;

/** Plain text with the model's **bold** marked up; stray asterisk pairs are dropped. */
function emphasis(text: string): Inline[] {
  const parts: Inline[] = [];
  let last = 0;
  for (const match of text.matchAll(STRONG)) {
    const at = match.index ?? 0;
    if (at > last) parts.push({ type: "text", text: text.slice(last, at) });
    parts.push({ type: "strong", text: match[1] });
    last = at + match[0].length;
  }
  if (last < text.length) parts.push({ type: "text", text: text.slice(last).replace(/\*\*/g, "") });
  return parts;
}
const BULLET = /^\s*(?:[-•*]|\d+[.)])\s+/;

/** Text with [S1]-style markers split out; a marker without a citation stays as text. */
export function inline(text: string, known: Set<string>): Inline[] {
  const parts: Inline[] = [];
  let last = 0;
  for (const match of text.matchAll(MARKER)) {
    const at = match.index ?? 0;
    if (!known.has(match[1])) continue;
    if (at > last) parts.push(...emphasis(text.slice(last, at)));
    parts.push({ type: "marker", marker: match[1] });
    last = at + match[0].length;
  }
  if (last < text.length) parts.push(...emphasis(text.slice(last)));
  return parts;
}

/** Paragraphs (blank lines) and bullet lists ("- ", "1. ") of an answer. */
export function blocks(text: string, citations: Citation[]): Block[] {
  const known = new Set(citations.map((c) => c.marker));
  const out: Block[] = [];
  for (const chunk of text.split(/\n\s*\n/)) {
    // Runs of bullet lines become lists, runs of other lines paragraphs: an intro line
    // straight before its bullets ("You need:\n- …") is both.
    let run: string[] = [];
    let bullets = false;
    const flush = () => {
      if (!run.length) return;
      if (bullets) out.push({ type: "list", items: run.map((l) => inline(l.replace(BULLET, ""), known)) });
      else out.push({ type: "paragraph", parts: inline(run.join(" "), known) });
      run = [];
    };
    for (const line of chunk.split("\n").filter((l) => l.trim())) {
      const isBullet = BULLET.test(line);
      if (run.length && isBullet !== bullets) flush();
      bullets = isBullet;
      run.push(line);
    }
    flush();
  }
  return out;
}

export const KIND_LABEL: Record<Kind, string | null> = {
  answered: null,
  follow_up: "A question back",
  not_settled: "The sources don't settle this",
  out_of_scope: "Outside what PermitNav covers",
  failed: "No answer",
};

/** The answer with its sources and dates, for copying or saving. */
export function asPlainText(response: AskResponse, answeredAt: string): string {
  const lines = [response.text.trim().replace(/\*\*/g, ""), ""]; // bold labels: plain text has none
  if (response.citations.length) {
    lines.push("Sources:");
    for (const c of response.citations) {
      lines.push(`[${c.marker}] ${c.heading} (${c.dates}) ${c.url}`);
      if (c.quote) lines.push(`    "${c.quote}"`);
    }
    lines.push("");
  }
  lines.push(`Rules as of ${response.as_of}; answered ${answeredAt.slice(0, 10)} by PermitNav.`);
  lines.push(response.notice);
  return lines.join("\n");
}

/** "2026-05-01" -> "1 May 2026": how people read a date; the ISO form stays in the API. */
export function readableDate(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  if (!y || !m || !d) return iso;
  return new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" })
    .format(new Date(Date.UTC(y, m - 1, d)));
}

/** The cited quote inside a source's full text, as [before, quote, after], whatever the line
 * breaks, spacing and letter case (the server accepts quotes on the same terms); null when it
 * is not there, or is there more than once: marking the first of several could show a passage
 * the answer did not rely on. */
export function locateQuote(text: string, quote: string): [string, string, string] | null {
  const words = quote.trim().split(/\s+/).filter(Boolean);
  if (!words.length) return null;
  const escaped = words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const matches = [...text.matchAll(new RegExp(escaped.join("\\s+"), "giu"))];
  if (matches.length !== 1) return null;
  const [match] = matches;
  const at = match.index ?? 0;
  return [text.slice(0, at), match[0], text.slice(at + match[0].length)];
}

/** The longest first paragraph still shown as the conclusion (the eval's LEAD_MAX_CHARS). */
export const LEAD_MAX_CHARS = 350;

/** Whether an answer opens with a conclusion worth setting off: a short first paragraph with
 * details after it. An older or non-conforming answer keeps the plain look. */
export function hasLead(kind: Kind, parsed: Block[]): boolean {
  if (kind !== "answered" && kind !== "not_settled") return false;
  const [first, second] = parsed;
  if (!first || !second || first.type !== "paragraph" || second.type === "list") return false;
  const length = first.parts.reduce((n, p) => n + (p.type === "marker" ? p.marker.length + 2 : p.text.length), 0);
  return length <= LEAD_MAX_CHARS;
}
