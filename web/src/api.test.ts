import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, askStream, DAILY_LIMIT, messageFor, parseEvents, rate } from "./api";

const ANSWER = { kind: "answered", text: "Svar.", citations: [], credits: [], as_of: "2026-11-24", notice: "",
  understood_as: null, situation: {} };

function streamOf(chunks: string[], status = 200): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(new TextEncoder().encode(c));
      controller.close();
    },
  });
  return new Response(body, { status, headers: { "Content-Type": "text/event-stream" } });
}

afterEach(() => vi.unstubAllGlobals());

describe("server-sent events", () => {
  it("accepts CRLF line breaks and 'data:' without a space", () => {
    const { events } = parseEvents('event:stage\r\ndata:{"stage":"writing"}\r\n\r\n');
    expect(events).toEqual([{ event: "stage", data: { stage: "writing" } }]);
  });

  it("keeps an unfinished event for the next chunk", () => {
    const { events, rest } = parseEvents('event: stage\ndata: {"stage":"searching"}\n\nevent: ans');
    expect(events).toEqual([{ event: "stage", data: { stage: "searching" } }]);
    expect(rest).toBe("event: ans");
  });

  it("reports each stage and returns the answer, however the stream is split", async () => {
    const whole = `event: stage\ndata: {"stage":"searching"}\n\nevent: stage\ndata: {"stage":"writing"}\n\n` +
      `event: answer\ndata: ${JSON.stringify(ANSWER)}\n\n`;
    const chunks = [whole.slice(0, 17), whole.slice(17, 60), whole.slice(60)]; // mid-event splits
    vi.stubGlobal("fetch", vi.fn(async () => streamOf(chunks)));
    const stages: string[] = [];
    const answer = await askStream("t", "q?", null, [], "c0nversation-id-0001", (s) => stages.push(s));
    expect(stages).toEqual(["searching", "writing"]);
    expect(answer.text).toBe("Svar.");
  });

  it("turns an error event, or a stream without an answer, into a message", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => streamOf(['event: error\ndata: {"status":502,"message":"x"}\n\n'])));
    await expect(askStream("t", "q?", null, [], "c0nversation-id-0001", () => {})).rejects.toBeInstanceOf(ApiError);
    vi.stubGlobal("fetch", vi.fn(async () => streamOf(['event: stage\ndata: {"stage":"searching"}\n\n'])));
    await expect(askStream("t", "q?", null, [], "c0nversation-id-0001", () => {})).rejects.toThrow("could not be prepared");
    vi.stubGlobal("fetch", vi.fn(async () => streamOf([], 401)));
    await expect(askStream("t", "q?", null, [], "c0nversation-id-0001", () => {})).rejects.toMatchObject({ signedOut: true });
  });

  it("says the daily limit was reached, whether it comes as a status or as an event", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => streamOf(['event: error\ndata: {"status":429,"message":"x"}\n\n'])));
    await expect(askStream("t", "q?", null, [], "c0nversation-id-0001", () => {})).rejects.toThrow(DAILY_LIMIT);
    vi.stubGlobal("fetch", vi.fn(async () => streamOf([], 429)));
    await expect(askStream("t", "q?", null, [], "c0nversation-id-0001", () => {})).rejects.toThrow(DAILY_LIMIT);
    expect(messageFor(502)).not.toBe(DAILY_LIMIT);
  });

  it("sends the conversation id with a question, and a rating as a click without text", async () => {
    const whole = `event: answer\ndata: ${JSON.stringify(ANSWER)}\n\n`;
    const fetched = vi.fn(async (_url: string, _init?: RequestInit) => streamOf([whole]));
    vi.stubGlobal("fetch", fetched);
    await askStream("t", "q?", null, ["earlier"], "c0nversation-id-0001", () => {});
    expect(JSON.parse(String(fetched.mock.calls[0][1]?.body))).toEqual({ question: "q?", as_of: null, history: ["earlier"], conversation: "c0nversation-id-0001" });

    const posted = vi.fn(async (_url: string, _init?: RequestInit) => new Response(JSON.stringify({ saved: true }), { status: 200 }));
    vi.stubGlobal("fetch", posted);
    expect(await rate("t", "0123456789abcdef0123456789abcdef", false)).toBe(true);
    expect(posted.mock.calls[0][0]).toBe("/rating");
    expect(JSON.parse(String(posted.mock.calls[0][1]?.body))).toEqual({ helpful: false, trace_id: "0123456789abcdef0123456789abcdef" });
    await rate("t", undefined, true); // an answer from before ratings: counted, tied to nothing
    expect(JSON.parse(String(posted.mock.calls[1][1]?.body))).toEqual({ helpful: true, trace_id: null });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ saved: false }), { status: 200 })));
    expect(await rate("t", null, true)).toBe(false); // not counted: the page keeps its buttons
  });
});
