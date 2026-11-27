import { afterEach, describe, expect, it } from "vitest";
import { answered, conversationId, historyFor, loadToken, loadTurns, MAX_HISTORY, saveToken, saveTurns, type Turn } from "./conversation";

function fakeStorage(): Storage {
  const data = new Map<string, string>();
  return {
    get length() { return data.size; },
    clear: () => data.clear(),
    getItem: (k) => data.get(k) ?? null,
    key: (i) => [...data.keys()][i] ?? null,
    removeItem: (k) => void data.delete(k),
    setItem: (k, v) => void data.set(k, v),
  };
}

const user = (text: string): Turn => ({ role: "user", text, asOf: null, at: "2026-11-24T10:00:00Z" });

afterEach(() => {
  delete (globalThis as { window?: unknown }).window;
});

describe("conversation memory", () => {
  it("sends only the user's earlier questions, the last few", () => {
    const turns = Array.from({ length: 9 }, (_, i) => user(`q${i}`));
    expect(historyFor(turns)).toEqual(["q3", "q4", "q5", "q6", "q7", "q8"]);
    expect(historyFor(turns)).toHaveLength(MAX_HISTORY);
  });

  it("round-trips through browser storage and forgets on delete", () => {
    (globalThis as { window?: unknown }).window = { localStorage: fakeStorage() };
    const pair: Turn[] = [user("Lönekrav?"), { role: "assistant", at: "x", response: { kind: "answered",
      text: "Svar.", citations: [], credits: [], as_of: "2026-11-24", notice: "", understood_as: null,
      situation: {} } }];
    saveTurns(pair);
    expect(loadTurns()).toEqual(pair);
    saveTurns([]);
    expect(loadTurns()).toEqual([]);
    saveToken("t");
    expect(loadToken()).toBe("t");
    saveToken(null);
    expect(loadToken()).toBeNull();
  });

  it("drops a trailing question that never got its answer", () => {
    const reply: Turn = {
      role: "assistant", at: "x",
      response: { kind: "answered", text: "Svar.", citations: [], credits: [], as_of: "2026-11-24",
                  notice: "", understood_as: null, situation: {} },
    };
    expect(answered([user("q1"), reply, user("q2")])).toEqual([user("q1"), reply]);
    (globalThis as { window?: unknown }).window = { localStorage: fakeStorage() };
    saveTurns([user("q1"), reply, user("lost on reload")]);
    expect(historyFor(loadTurns())).toEqual(["q1"]); // never sent as history later
  });

  it("survives broken or blocked storage", () => {
    const storage = fakeStorage();
    storage.setItem("permitnav.conversation.v1", "{not json");
    (globalThis as { window?: unknown }).window = { localStorage: storage };
    expect(loadTurns()).toEqual([]);
    const ok: Turn = { role: "assistant", at: "x", response: { kind: "answered", text: "ok", citations: [],
      credits: [], as_of: "2026-11-24", notice: "", understood_as: null, situation: {} } };
    storage.setItem("permitnav.conversation.v1", JSON.stringify([{ role: "user", text: 3 }, ok]));
    expect(loadTurns()).toEqual([ok]);
    (globalThis as { window?: unknown }).window = {
      get localStorage(): Storage { throw new Error("blocked"); },
    };
    expect(loadTurns()).toEqual([]);
    expect(() => saveTurns([user("x")])).not.toThrow();
  });

  it("keeps one random id per conversation and makes a new one after a delete", () => {
    (globalThis as { window?: unknown }).window = { localStorage: fakeStorage() };
    const first = conversationId();
    expect(first).toMatch(/^[A-Za-z0-9_-]{22}$/);
    saveTurns([user("q"), { role: "assistant", response: { text: "a", citations: [] } as never, at: "t" }]);
    expect(conversationId()).toBe(first); // the same conversation, also after a reload
    saveTurns([]); // deleted, or signed out
    expect(conversationId()).not.toBe(first);
  });

  it("does not bring a deleted conversation's id back from another tab's memory", () => {
    const shared = fakeStorage();
    (globalThis as { window?: unknown }).window = { localStorage: shared };
    const first = conversationId(); // this tab now also remembers it
    shared.removeItem("permitnav.conversation.id"); // another tab deleted the conversation
    expect(conversationId()).not.toBe(first);
  });

  it("still gives a conversation an id when storage is blocked", () => {
    (globalThis as { window?: unknown }).window = { get localStorage(): Storage { throw new Error("blocked"); } };
    const first = conversationId();
    expect(conversationId()).toBe(first);
    saveTurns([]);
    expect(conversationId()).not.toBe(first);
  });
});
