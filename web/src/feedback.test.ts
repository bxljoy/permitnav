import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AskResponse } from "./api";
import {
  abandoned,
  ABANDONED_AFTER_MS,
  addPending,
  addReport,
  answerForReview,
  citedUnits,
  clearReports,
  clip,
  identifiersIn,
  loadPending,
  loadReports,
  newWithdrawCode,
  removePending,
  removeReport,
} from "./feedback";

describe("identifiersIn", () => {
  it("finds an email address or a personal number in any of the texts", () => {
    expect(identifiersIn("fråga", "svar", "mejla anna@example.com")).toEqual(["an email address"]);
    expect(identifiersIn("mejla andré@exempel.se")).toEqual(["an email address"]);
    expect(identifiersIn("mitt personnummer är 811218-9876")).toEqual(["a Swedish personal number"]);
    expect(identifiersIn("198112189876")).toEqual(["a Swedish personal number"]);
    expect(identifiersIn("samordningsnummer 701063-2391")).toEqual(["a Swedish personal number"]);
  });

  it("leaves law references, amounts, dates, organisation and phone numbers alone", () => {
    const ordinary =
      "Enligt 6 kap. 2 § (SFS 2005:716) krävs 34 470 kronor från 2026-06-01, se MIG 2015:11 och RS/084/2021. " +
      "Arbetsgivaren (org.nr 556677-8899) nås på 0701234567 eller 0703124560; ärende 811218-9875.";
    expect(identifiersIn(ordinary)).toEqual([]);
    expect(identifiersIn("född 070312-4560")).toEqual(["a Swedish personal number"]);
  });

  it("gives the same answer when asked twice", () => {
    expect(identifiersIn("811218-9876")).toEqual(identifiersIn("811218-9876"));
  });
});

describe("what the form starts from", () => {
  const response = {
    kind: "answered",
    text: "**Yes.** You can [S1][S2].",
    citations: [{ unit_id: "a" }, { unit_id: "a" }, { unit_id: "b" }],
  } as unknown as AskResponse;

  it("shows the answer as read, without bold markers", () => {
    expect(answerForReview(response)).toBe("Yes. You can [S1][S2].");
  });

  it("lists each cited unit once", () => {
    expect(citedUnits(response)).toEqual(["a", "b"]);
  });

  it("makes a withdrawal code the server accepts, different every time", () => {
    const code = newWithdrawCode();
    expect(code).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(newWithdrawCode()).not.toBe(code);
  });
});

describe("the saved-for-review record", () => {
  beforeEach(() => {
    const kept = new Map<string, string>();
    vi.stubGlobal("window", {
      localStorage: {
        getItem: (k: string) => kept.get(k) ?? null,
        setItem: (k: string, v: string) => void kept.set(k, v),
        removeItem: (k: string) => void kept.delete(k),
      },
    });
  });

  it("keeps a code another tab stored when this tab adds or removes its own", () => {
    const thisTab = loadReports(); // read before the other tab saved
    addReport({}, "answer-1", "code-1"); // the other tab
    const after = addReport(thisTab, "answer-2", "code-2");
    expect(after).toEqual({ "answer-1": "code-1", "answer-2": "code-2" });
    expect(removeReport(thisTab, "answer-2")).toEqual({ "answer-1": "code-1" });
    expect(loadReports()).toEqual({ "answer-1": "code-1" });
  });

  it("forgets everything when the conversation is deleted", () => {
    addReport({}, "answer-1", "code-1");
    expect(clearReports()).toEqual({});
    expect(loadReports()).toEqual({});
  });

  it("remembers an unconfirmed save from before it is sent until it is confirmed or withdrawn", () => {
    addPending("code-1", 1_000);
    addPending("code-1", 5_000); // a retry keeps the first time
    addPending("code-2", 1_000 + ABANDONED_AFTER_MS);
    expect(loadPending()).toEqual({ "code-1": 1_000, "code-2": 1_000 + ABANDONED_AFTER_MS });
    // a form may still be waiting for code-2's reply: only the old one is given up on
    expect(abandoned(1_000 + ABANDONED_AFTER_MS)).toEqual(["code-1"]);
    removePending("code-1");
    expect(loadPending()).toEqual({ "code-2": 1_000 + ABANDONED_AFTER_MS });
    clearReports(); // deleting the conversation does not forget what must still be withdrawn
    expect(Object.keys(loadPending())).toEqual(["code-2"]);
  });
});

describe("clip", () => {
  it("never ends in half an emoji", () => {
    expect(clip("ab🙂", 3)).toBe("ab");
    expect(clip("ab🙂", 4)).toBe("ab🙂");
    expect(clip("abcd", 3)).toBe("abc");
  });
});
