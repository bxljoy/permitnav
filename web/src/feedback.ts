// Feedback with consent (phase 6, slice 3): nothing is sent until the person saves the form,
// and the form shows everything that would be saved. The server applies the same checks
// (src/permitnav/feedback.py); these run first so the person sees the problem while typing.

import type { AskResponse } from "./api";

export const MAX_QUESTION_CHARS = 2000;
export const MAX_ANSWER_CHARS = 8000;
export const MAX_COMMENT_CHARS = 2000;

const EMAIL = /[\p{L}\p{N}_.+-]+@[\p{L}\p{N}_-]+\.[\p{L}\p{N}_-]+/u;
const PERSONAL_NUMBER = /(?<![0-9])((?:19|20)?)([0-9]{6})([-+]?)([0-9]{4})(?![0-9])/g;

/** A real personnummer or samordningsnummer: a date of birth (day +60 for a coordination
 * number) and a correct check digit; not an organisation number or any ten digits, nor
 * ten bare digits starting 07, which is how a mobile number is written. */
function isPersonalNumber(century: string, born: string, separator: string, last: string): boolean {
  if (!century && !separator && born.startsWith("07")) return false;
  const month = Number(born.slice(2, 4));
  const day = Number(born.slice(4, 6));
  if (!(month >= 1 && month <= 12 && ((day >= 1 && day <= 31) || (day >= 61 && day <= 91)))) return false;
  let total = 0;
  [...(born + last)].forEach((digit, position) => {
    const value = Number(digit) * (position % 2 === 0 ? 2 : 1);
    total += value > 9 ? value - 9 : value;
  });
  return total % 10 === 0;
}

/** Kinds of identifying data in the texts; the server refuses a submission that has any. */
export function identifiersIn(...texts: string[]): string[] {
  const found: string[] = [];
  if (texts.some((t) => EMAIL.test(t))) found.push("an email address");
  if (texts.some((t) => [...t.matchAll(PERSONAL_NUMBER)].some((m) => isPersonalNumber(m[1], m[2], m[3], m[4])))) {
    found.push("a Swedish personal number");
  }
  return found;
}

/** The answer as the form first shows it: the text the person read, without bold markers. */
export function answerForReview(response: AskResponse): string {
  return clip(response.text.replace(/\*\*/g, ""), MAX_ANSWER_CHARS);
}

/** At most `max` characters, never ending in half of a two-part character (an emoji):
 * the server refuses text with a lone half. */
export function clip(text: string, max: number): string {
  if (text.length <= max) return text;
  const last = text.charCodeAt(max - 1);
  return text.slice(0, last >= 0xd800 && last <= 0xdbff ? max - 1 : max);
}

/** The units an answer cited, once each. */
export function citedUnits(response: AskResponse): string[] {
  return [...new Set(response.citations.map((c) => c.unit_id))].slice(0, 40);
}

/** The code that withdraws a submission. Made here, before anything is sent, so the person
 * can undo a save even when the server's reply never arrived; the server keeps its hash. */
export function newWithdrawCode(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** Which answers the person saved for review (answer time → withdrawal code). Kept apart
 * from the conversation and changed one entry at a time, so another tab saving its copy of
 * the conversation cannot lose a code. */
export type Reports = Record<string, string>;

const STORED_REPORTS = "permitnav.reports.v1";

export function loadReports(): Reports {
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(STORED_REPORTS) ?? "{}");
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return {};
    return Object.fromEntries(Object.entries(parsed).filter(([, code]) => typeof code === "string")) as Reports;
  } catch {
    return {};
  }
}

function store(reports: Reports): Reports {
  try {
    if (Object.keys(reports).length) window.localStorage.setItem(STORED_REPORTS, JSON.stringify(reports));
    else window.localStorage.removeItem(STORED_REPORTS);
  } catch {
    // blocked or full: the code lasts until the tab closes
  }
  return reports;
}

/** Add one, on top of what is stored now (another tab may have added its own meanwhile). */
export function addReport(current: Reports, answerAt: string, code: string): Reports {
  return store({ ...current, ...loadReports(), [answerAt]: code });
}

export function removeReport(current: Reports, answerAt: string): Reports {
  const { [answerAt]: _gone, ...rest } = { ...current, ...loadReports() };
  return store(rest);
}

export function clearReports(): Reports {
  return store({});
}

export function isReportsKey(key: string | null): boolean {
  return key === null || key === STORED_REPORTS; // null: the whole storage was cleared
}

/** Codes of saves that were sent and neither confirmed nor withdrawn yet (code → when it
 * was first sent). Written before the request leaves, so a reload or a closed tab cannot
 * leave a stored row that nobody holds the code for: what is still here later is withdrawn. */
export type Pending = Record<string, number>;

const STORED_PENDING = "permitnav.reports.unconfirmed.v1";

export function loadPending(): Pending {
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(STORED_PENDING) ?? "{}");
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return {};
    return Object.fromEntries(Object.entries(parsed).filter(([, at]) => typeof at === "number")) as Pending;
  } catch {
    return {};
  }
}

function storePending(pending: Pending): void {
  try {
    if (Object.keys(pending).length) window.localStorage.setItem(STORED_PENDING, JSON.stringify(pending));
    else window.localStorage.removeItem(STORED_PENDING);
  } catch {
    // blocked or full: the form still withdraws on Cancel while the tab is open
  }
}

export function addPending(code: string, now = Date.now()): void {
  const pending = loadPending();
  if (!(code in pending)) storePending({ ...pending, [code]: now });
}

export function removePending(code: string): void {
  const { [code]: _gone, ...rest } = loadPending();
  storePending(rest);
}

/** Unconfirmed saves old enough that no open form can still be waiting for their reply. */
export const ABANDONED_AFTER_MS = 2 * 60 * 1000;

export function abandoned(now = Date.now()): string[] {
  return Object.entries(loadPending())
    .filter(([, at]) => now - at >= ABANDONED_AFTER_MS)
    .map(([code]) => code);
}
