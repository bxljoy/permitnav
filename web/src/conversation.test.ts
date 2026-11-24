import { afterEach, describe, expect, it } from "vitest";
import { answered, historyFor, loadToken, loadTurns, MAX_HISTORY, saveToken, saveTurns, type Turn } from "./conversation";

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
});
