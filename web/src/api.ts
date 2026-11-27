// The PermitNav API, as the server returns it (src/permitnav/api.py).

export type Kind = "answered" | "follow_up" | "not_settled" | "out_of_scope" | "failed";

export interface Citation {
  marker: string;
  unit_id: string;
  heading: string;
  url: string;
  authority: string;
  dates: string;
  quote: string;
  translation: string;
  version_id?: number | null; // the exact wording cited (absent in conversations saved before)
}

export interface AskResponse {
  kind: Kind;
  text: string;
  citations: Citation[];
  credits: string[];
  as_of: string;
  notice: string;
  understood_as: string | null;
  situation: Record<string, string>;
  trace_id?: string | null; // the answer's technical record, when there is one (for a rating)
  recorded?: boolean; // this question was kept with its text (the maintainer's own)
}

export interface Source {
  unit_id: string;
  heading: string;
  authority: string;
  url: string;
  dates: string;
  text: string;
}

/** A failed request, with a message a person can act on. `signedOut`: the token was refused. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly signedOut = false,
  ) {
    super(message);
  }
}

/** What to tell the person when a request fails, per endpoint. */
interface Messages {
  invalid: string; // 422
  missing: string; // 404
  failed: string; // anything else
}

const QUESTION: Messages = {
  invalid: "The question could not be sent: it is empty or too long (2,000 characters at most).",
  missing: "PermitNav could not find what it needed. Try again.",
  failed: "The answer could not be prepared just now. Try again in a moment.",
};
const SOURCE: Messages = {
  invalid: "This source cannot be opened from here.",
  missing: "This source is no longer available.",
  failed: "The source could not be loaded just now. Try again, or open the original.",
};

const WITHDRAW: Messages = {
  invalid: "This submission cannot be withdrawn from here.",
  missing: "gone",
  failed: "It could not be withdrawn just now. Try again in a moment.",
};

/** The site's daily spend cap was reached (the server's 429): trying again today won't help. */
export const DAILY_LIMIT = "PermitNav has reached its limit for today. Try again tomorrow.";

/** What to tell the person when a question fails with this status. */
export function messageFor(status: number | undefined): string {
  if (status === 422) return QUESTION.invalid;
  if (status === 404) return QUESTION.missing;
  if (status === 429) return DAILY_LIMIT;
  return QUESTION.failed;
}

async function call<T>(
  token: string,
  path: string,
  init: RequestInit = {},
  messages: Messages = QUESTION,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers: { ...(init.headers ?? {}), Authorization: `Bearer ${token}` },
    });
  } catch {
    throw new ApiError("Could not reach PermitNav. Check your connection and try again.");
  }
  if (response.status === 401) {
    throw new ApiError("The access token was not accepted. Enter it again.", true);
  }
  if (response.status === 422) throw new ApiError(messages.invalid);
  if (response.status === 404) throw new ApiError(messages.missing);
  if (!response.ok) throw new ApiError(messages.failed);
  return (await response.json()) as T;
}

/** `recorded`: questions sent with this token are kept with their text (the maintainer's own). */
export function checkToken(token: string): Promise<{ ok: boolean; recorded?: boolean }> {
  return call(token, "/auth-check");
}

/** A click under an answer: helpful or not. Only the click is sent, never any text.
 * Resolves with whether the server counted it. */
export async function rate(token: string, traceId: string | null | undefined, helpful: boolean): Promise<boolean> {
  const reply = await call<{ saved?: boolean }>(token, "/rating", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ helpful, trace_id: traceId ?? null }),
  });
  return reply.saved === true;
}

/** The exact wording an answer cited. A POST body, so which sources a person opens never
 * appears in a URL or a server's access log. */
export function source(token: string, versionId: number): Promise<Source> {
  return call(
    token,
    "/source",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version_id: versionId }),
    },
    SOURCE,
  );
}

export interface FeedbackCase {
  question: string;
  answer: string;
  comment: string;
  kind: Kind;
  as_of: string;
  cited: string[];
}

/** A failed save whose outcome is not known: the request may have reached the server. */
export const SAVE_UNSURE =
  "PermitNav did not confirm the save, so it may or may not be stored. Save again to be sure (it is stored only once), or cancel and it will be removed as soon as PermitNav can be reached.";

/** Save what the person confirmed in the form, under a withdrawal code made in the browser.
 * Only ever called from the form's Save button. Saving again with the same code replaces. */
export async function saveFeedback(token: string, feedback: FeedbackCase, code: string): Promise<void> {
  let response: Response;
  try {
    response = await fetch("/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ consent: true, withdraw_code: code, ...feedback }),
    });
  } catch {
    throw new ApiError(SAVE_UNSURE);
  }
  if (response.status === 401) throw new ApiError("The access token was not accepted. Enter it again.", true);
  if (response.status === 422) {
    // refused, so not stored: the server says what to remove, or that the queue is full
    const detail = await response.json().then((b: { detail?: unknown }) => b.detail, () => null);
    throw new ApiError(typeof detail === "string" ? detail : "This could not be saved as written. Check the text and the date, and try again.");
  }
  if (!response.ok) throw new ApiError(SAVE_UNSURE); // a proxy error can follow a completed save
}

/** Undo a submission. Resolves also when there is nothing to undo (never stored, or
 * already reviewed and deleted). */
export async function withdrawFeedback(token: string, code: string): Promise<void> {
  const init = { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ withdraw_code: code }) };
  try {
    await call(token, "/feedback/withdraw", init, WITHDRAW);
  } catch (e) {
    if (e instanceof ApiError && e.message === WITHDRAW.missing) return;
    throw e;
  }
}

export type Stage = "understanding" | "searching" | "checking" | "writing";

/** Server-sent events, parsed from a fetch body (EventSource cannot POST or send a token). */
export function parseEvents(buffer: string): { events: { event: string; data: unknown }[]; rest: string } {
  const events: { event: string; data: unknown }[] = [];
  const blocks = buffer.replace(/\r\n?/g, "\n").split("\n\n"); // the standard allows CRLF and CR
  const rest = blocks.pop() ?? "";
  for (const block of blocks) {
    let event = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      const colon = line.indexOf(":");
      if (colon <= 0) continue;
      const field = line.slice(0, colon);
      const value = line.slice(colon + 1).replace(/^ /, ""); // one optional space after ':'
      if (field === "event") event = value;
      else if (field === "data") data.push(value);
    }
    if (data.length) {
      try {
        events.push({ event, data: JSON.parse(data.join("\n")) });
      } catch {
        // a malformed event is skipped; the answer or error event decides the outcome
      }
    }
  }
  return { events, rest };
}

/** /ask/stream: calls `onStage` as the answer is prepared, resolves with the answer. */
export async function askStream(
  token: string,
  question: string,
  asOf: string | null,
  history: string[],
  conversation: string,
  onStage: (stage: Stage) => void,
  signal?: AbortSignal,
): Promise<AskResponse> {
  let response: Response;
  try {
    response = await fetch("/ask/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ question, as_of: asOf, history, conversation }),
      signal,
    });
  } catch (e) {
    if (signal?.aborted) throw e;
    throw new ApiError("Could not reach PermitNav. Check your connection and try again.");
  }
  if (response.status === 401) throw new ApiError("The access token was not accepted. Enter it again.", true);
  if (response.status === 422) throw new ApiError(QUESTION.invalid);
  if (response.status === 404) throw new ApiError(QUESTION.missing);
  if (response.status === 429) throw new ApiError(DAILY_LIMIT);
  if (!response.ok || !response.body) throw new ApiError(QUESTION.failed);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (value) buffer += decoder.decode(value, { stream: true });
    const parsed = parseEvents(done ? buffer + "\n\n" : buffer);
    buffer = parsed.rest;
    for (const { event, data } of parsed.events) {
      if (event === "stage") onStage((data as { stage: Stage }).stage);
      else if (event === "answer") {
        void reader.cancel().catch(() => {}); // done: release the connection
        return data as AskResponse;
      } else if (event === "error") {
        const status = (data as { status?: number }).status;
        throw new ApiError(messageFor(status));
      }
    }
    if (done) throw new ApiError(QUESTION.failed); // ended without an answer
  }
}
