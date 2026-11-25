import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, askStream, DAILY_LIMIT, messageFor, parseEvents } from "./api";

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
    const answer = await askStream("t", "q?", null, [], (s) => stages.push(s));
    expect(stages).toEqual(["searching", "writing"]);
    expect(answer.text).toBe("Svar.");
  });

  it("turns an error event, or a stream without an answer, into a message", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => streamOf(['event: error\ndata: {"status":502,"message":"x"}\n\n'])));
    await expect(askStream("t", "q?", null, [], () => {})).rejects.toBeInstanceOf(ApiError);
    vi.stubGlobal("fetch", vi.fn(async () => streamOf(['event: stage\ndata: {"stage":"searching"}\n\n'])));
    await expect(askStream("t", "q?", null, [], () => {})).rejects.toThrow("could not be prepared");
    vi.stubGlobal("fetch", vi.fn(async () => streamOf([], 401)));
    await expect(askStream("t", "q?", null, [], () => {})).rejects.toMatchObject({ signedOut: true });
  });

  it("says the daily limit was reached, whether it comes as a status or as an event", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => streamOf(['event: error\ndata: {"status":429,"message":"x"}\n\n'])));
    await expect(askStream("t", "q?", null, [], () => {})).rejects.toThrow(DAILY_LIMIT);
    vi.stubGlobal("fetch", vi.fn(async () => streamOf([], 429)));
    await expect(askStream("t", "q?", null, [], () => {})).rejects.toThrow(DAILY_LIMIT);
    expect(messageFor(502)).not.toBe(DAILY_LIMIT);
  });
});
