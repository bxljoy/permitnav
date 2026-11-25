import { describe, expect, it } from "vitest";
import type { AskResponse, Citation } from "./api";
import { asPlainText, blocks, hasLead, inline, locateQuote, readableDate } from "./answer";

const cite = (marker: string): Citation => ({
  marker, unit_id: `sfs:2005:716/6kap/${marker}`, heading: `6 kap. ${marker}`, url: "https://x",
  authority: "law", dates: "applies from 2026-06-01", quote: "lönen", translation: "the salary",
});

describe("dates", () => {
  it("shows a date the way people read it, whatever the time zone", () => {
    expect(readableDate("2026-05-01")).toBe("1 May 2026");
    expect(readableDate("2026-11-24")).toBe("24 November 2026");
    expect(readableDate("not a date")).toBe("not a date");
  });
});

describe("answer text", () => {
  it("splits out markers that have a citation and leaves the rest as text", () => {
    expect(inline("At least 90 percent [S1], see [S9].", new Set(["S1"]))).toEqual([
      { type: "text", text: "At least 90 percent " },
      { type: "marker", marker: "S1" },
      { type: "text", text: ", see [S9]." },
    ]);
  });

  it("renders the model's **bold** and drops stray asterisks", () => {
    expect(inline("**Arbete:** Ja [S1]. **", new Set(["S1"]))).toEqual([
      { type: "strong", text: "Arbete:" },
      { type: "text", text: " Ja " },
      { type: "marker", marker: "S1" },
      { type: "text", text: ". " },
    ]);
  });

  it("splits an intro line from the bullets straight after it", () => {
    const out = blocks("You need:\n- an offer [S1]\n- a salary [S2]\nThat is all.", [cite("S1"), cite("S2")]);
    expect(out.map((b) => b.type)).toEqual(["paragraph", "list", "paragraph"]);
    const list = out[1];
    expect(list.type === "list" && list.items.length).toBe(2);
  });

  it("keeps paragraphs and bullet lists", () => {
    const out = blocks("First [S1].\n\n- one [S2]\n- two\n\nLast.", [cite("S1"), cite("S2")]);
    expect(out.map((b) => b.type)).toEqual(["paragraph", "list", "paragraph"]);
    const list = out[1];
    expect(list.type === "list" && list.items.length).toBe(2);
  });

  it("copies the answer with its sources, the rules' date and the notice", () => {
    const response: AskResponse = {
      kind: "answered", text: "At least 90 percent [S1].", citations: [cite("S1")], credits: [],
      as_of: "2026-11-24", notice: "Not legal advice.", understood_as: null, situation: {},
    };
    const text = asPlainText(response, "2026-11-25T10:00:00Z");
    expect(text).toContain("[S1] 6 kap. S1 (applies from 2026-06-01) https://x");
    expect(text).toContain('"lönen"');
    expect(text).toContain("Rules as of 2026-11-24; answered 2026-11-25");
    expect(text.trim().endsWith("Not legal advice.")).toBe(true);
  });
});

describe("the cited passage in a source", () => {
  it("is found across line breaks and extra spaces", () => {
    const text = "Arbetstillstånd får ges om\n\n1. lönen  minst uppgår (till) 90 procent.\nSlut.";
    expect(locateQuote(text, "lönen minst uppgår (till) 90 procent.")).toEqual([
      "Arbetstillstånd får ges om\n\n1. ", "lönen  minst uppgår (till) 90 procent.", "\nSlut.",
    ]);
  });
  it("is found whatever the letter case, as the server accepts it", () => {
    expect(locateQuote("… om arbetstillstånd får ges till …", "Arbetstillstånd får ges")?.[1]).toBe("arbetstillstånd får ges");
    expect(locateQuote("ÄNDRING ska göras", "ändring ska")?.[1]).toBe("ÄNDRING ska");
  });
  it("is not marked when it occurs more than once", () => {
    expect(locateQuote("ska ansökan avslås. … Då ska ansökan avslås.", "ska ansökan avslås")).toBeNull();
  });
  it("is null when the text does not contain it", () => {
    expect(locateQuote("annan text", "lönen")).toBeNull();
    expect(locateQuote("text", "  ")).toBeNull();
  });
});

describe("the conclusion", () => {
  const c = [cite("S1")];
  it("is set off only when it has the shape of one", () => {
    expect(hasLead("answered", blocks("Yes, if the salary is enough [S1].\n\nDetails.", c))).toBe(true);
    expect(hasLead("not_settled", blocks("The sources I have do not cover this.\n\nDetails.", c))).toBe(true);
    expect(hasLead("answered", blocks("x".repeat(400) + " [S1].\n\nDetails.", c))).toBe(false); // an old, long opening
    expect(hasLead("answered", blocks("You need:\n- a\n- b", c))).toBe(false); // an intro line before a list
    expect(hasLead("answered", blocks("Only one paragraph [S1].", c))).toBe(false);
    expect(hasLead("follow_up", blocks("Which permit?\n\nIt matters because…", c))).toBe(false);
  });
  it("copies bold labels without the asterisks", () => {
    const response = { kind: "answered" as const, text: "Yes [S1].\n\n**What to do:** Apply.", citations: c, credits: [],
      as_of: "2026-11-25", notice: "n", understood_as: null, situation: {} };
    expect(asPlainText(response, "2026-11-25T10:00:00Z")).toContain("What to do: Apply.");
    expect(asPlainText(response, "2026-11-25T10:00:00Z")).not.toContain("**");
  });
});
