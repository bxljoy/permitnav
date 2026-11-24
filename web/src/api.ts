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

export function checkToken(token: string): Promise<{ ok: boolean }> {
  return call(token, "/auth-check");
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
  onStage: (stage: Stage) => void,
  signal?: AbortSignal,
): Promise<AskResponse> {
  let response: Response;
  try {
    response = await fetch("/ask/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ question, as_of: asOf, history }),
      signal,
    });
  } catch (e) {
    if (signal?.aborted) throw e;
    throw new ApiError("Could not reach PermitNav. Check your connection and try again.");
  }
  if (response.status === 401) throw new ApiError("The access token was not accepted. Enter it again.", true);
  if (response.status === 422) throw new ApiError(QUESTION.invalid);
  if (response.status === 404) throw new ApiError(QUESTION.missing);
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
        throw new ApiError(status === 422 ? QUESTION.invalid : status === 404 ? QUESTION.missing : QUESTION.failed);
      }
    }
    if (done) throw new ApiError(QUESTION.failed); // ended without an answer
  }
}
