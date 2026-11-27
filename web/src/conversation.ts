// The conversation lives in this browser only (phase 6): it survives closing the tab on this
// device, it is sent back as `history` for follow-on questions, and the server stores none of it
// (except an answer the person chooses to save for review: see feedback.ts).

import type { AskResponse } from "./api";

export interface UserTurn {
  role: "user";
  text: string;
  asOf: string | null; // the date the person asked about; null = today
  at: string; // when it was asked (ISO)
}

export interface AssistantTurn {
  role: "assistant";
  response: AskResponse;
  at: string;
  rated?: "helpful" | "not_helpful"; // the person's click under the answer, once made
}

export type Turn = UserTurn | AssistantTurn;

/** Earlier questions the API uses for a follow-on: only the last few are read (intake). */
export const MAX_HISTORY = 6;

const STORED_CONVERSATION = "permitnav.conversation.v1";
const STORED_ACCESS = "permitnav.token";
const STORED_ID = "permitnav.conversation.id";

let idInMemory: string | null = null; // when storage is blocked: lasts until the tab closes

/** A random id for this conversation, made on its first question and forgotten when the
 * conversation is deleted. The server uses it only to show a conversation's questions
 * together in its technical records; it is tied to no person and says nothing about one. */
export function conversationId(): string {
  const valid = (id: string | null): id is string => !!id && /^[A-Za-z0-9_-]{16,64}$/.test(id);
  const store = storage();
  // With storage, the stored id is the only one: when another tab has deleted the
  // conversation, this tab must not bring the old id back from its own memory.
  let id: string | null = idInMemory;
  if (store) {
    try {
      id = store.getItem(STORED_ID);
    } catch {
      // unreadable: the one in memory
    }
  }
  if (!valid(id)) {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    id = btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  idInMemory = id;
  try {
    store?.setItem(STORED_ID, id);
  } catch {
    // full or blocked: kept in memory
  }
  return id;
}

/** The user's earlier questions in this conversation, oldest first, at most MAX_HISTORY. */
export function historyFor(turns: Turn[]): string[] {
  return turns
    .filter((t): t is UserTurn => t.role === "user")
    .map((t) => t.text)
    .slice(-MAX_HISTORY);
}

function storage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null; // blocked (private window, site data off): the page works, it just forgets
  }
}

export function loadTurns(): Turn[] {
  try {
    const raw = storage()?.getItem(STORED_CONVERSATION);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return answered(Array.isArray(parsed) ? parsed.filter(isTurn) : []);
  } catch {
    return [];
  }
}

/** Without a trailing question that never got its answer (the tab closed while waiting, or
 * the request failed): it would otherwise be sent as history with every later question. */
export function answered(turns: Turn[]): Turn[] {
  let end = turns.length;
  while (end > 0 && turns[end - 1].role === "user") end--;
  return turns.slice(0, end);
}

export function saveTurns(turns: Turn[]): void {
  try {
    if (!turns.length) idInMemory = null; // an empty conversation: the next one gets a new id
    if (turns.length) storage()?.setItem(STORED_CONVERSATION, JSON.stringify(turns));
    else {
      storage()?.removeItem(STORED_CONVERSATION);
      storage()?.removeItem(STORED_ID);
    }
  } catch {
    // full or blocked: keep the conversation in memory only
  }
}

export function loadToken(): string | null {
  try {
    return storage()?.getItem(STORED_ACCESS) ?? null;
  } catch {
    return null;
  }
}

export function saveToken(token: string | null): void {
  try {
    if (token) storage()?.setItem(STORED_ACCESS, token);
    else storage()?.removeItem(STORED_ACCESS);
  } catch {
    // blocked: the token lasts until the tab closes
  }
}

function isTurn(value: unknown): value is Turn {
  if (typeof value !== "object" || value === null) return false;
  const t = value as Record<string, unknown>;
  if (t.role === "user") return typeof t.text === "string";
  if (t.role === "assistant") {
    const r = t.response as Record<string, unknown> | undefined;
    return typeof r === "object" && r !== null && typeof r.text === "string" && Array.isArray(r.citations);
  }
  return false;
}
