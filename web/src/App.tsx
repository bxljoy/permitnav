import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { ApiError, askStream, SAVE_UNSURE, checkToken, rate, saveFeedback, source, withdrawFeedback, type AskResponse, type FeedbackCase, type Citation, type Source, type Stage } from "./api";
import { asPlainText, blocks, hasLead, KIND_LABEL, KIND_NOTE, locateQuote, readableDate, type Inline } from "./answer";
import { conversationId, historyFor, loadToken, loadTurns, saveToken, saveTurns, type Turn, type UserTurn } from "./conversation";
import {
  abandoned,
  addPending,
  addReport,
  clip,
  removePending,
  answerForReview,
  citedUnits,
  clearReports,
  identifiersIn,
  isReportsKey,
  loadReports,
  MAX_ANSWER_CHARS,
  MAX_COMMENT_CHARS,
  MAX_QUESTION_CHARS,
  newWithdrawCode,
  removeReport,
  type Reports,
} from "./feedback";

const EXAMPLES = [
  "What salary do I need to get a work permit?",
  "Jag har blivit uppsagd. Hur lång tid har jag på mig att hitta ett nytt jobb?",
  "I have a work permit. Can my spouse come to Sweden and work too?",
];

const SITUATION_LABEL: Record<string, string> = {
  permit: "Permit",
  permit_granted: "Permit granted",
  application_registered: "Application registered",
  employer_change: "Employer change",
  family: "Family",
  other: "Other",
};

export default function App() {
  const [token, setToken] = useState<string | null>(() => loadToken());
  const [notice, setNotice] = useState<string | null>(null);
  if (!token) {
    return (
      <TokenScreen
        message={notice}
        onToken={(t) => {
          saveToken(t);
          setNotice(null);
          setToken(t);
        }}
      />
    );
  }
  return (
    <Chat
      token={token}
      onSignedOut={(message, forget) => {
        saveToken(null);
        if (forget) {
          saveTurns([]); // signing out on purpose: the next person sees nothing,
          clearReports(); // and cannot withdraw (or learn of) what was saved for review
        }
        setNotice(message);
        setToken(null);
      }}
    />
  );
}

function TokenScreen({ message, onToken }: { message: string | null; onToken: (token: string) => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(message);
  const [checking, setChecking] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const candidate = value.trim();
    if (!candidate) return;
    setChecking(true);
    try {
      await checkToken(candidate);
      onToken(candidate);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Something went wrong. Try again.");
    } finally {
      setChecking(false);
    }
  }

  return (
    <main className="gate">
      <form className="gate-card" onSubmit={submit}>
        <h1>PermitNav</h1>
        <p className="lede">Swedish work and residence permit rules, answered from the law and Migrationsverket's pages, with sources.</p>
        <p className="muted">PermitNav is in private testing. Enter your access token to continue; it is kept in this browser only.</p>
        <label htmlFor="token">Access token</label>
        <input id="token" type="password" autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)} autoFocus />
        {error && <p className="error" role="alert">{error}</p>}
        <button type="submit" className="primary" disabled={checking || !value.trim()}>
          {checking ? "Checking…" : "Continue"}
        </button>
      </form>
    </main>
  );
}

function Chat({ token, onSignedOut }: { token: string; onSignedOut: (message: string, forget?: boolean) => void }) {
  const [turns, setTurns] = useState<Turn[]>(() => loadTurns());
  const [pending, setPending] = useState<{ question: string; started: number; stages: Stage[]; followOn: boolean } | null>(null);
  const inFlight = useRef<AbortController | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<{ citation: Citation; asOf: string } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [draftBack, setDraftBack] = useState<string | null>(null); // a question that failed, back in the box
  const [asOf, setAsOf] = useState(""); // "Rules as of": used by the box and the examples alike
  const generation = useRef(0); // bumped by Delete: a late answer to a deleted conversation is dropped
  const end = useRef<HTMLDivElement>(null);
  const [reports, setReports] = useState<Reports>(() => loadReports()); // answers saved for review
  const alive = useRef(true);
  const [recorded, setRecorded] = useState(false); // this token's questions are kept with their text
  useEffect(() => {
    alive.current = true;
    checkToken(token).then(
      (r) => alive.current && setRecorded(r.recorded === true),
      () => {}, // a wrong token shows on the first question
    );
    const onStorage = (e: StorageEvent) => {
      if (isReportsKey(e.key)) setReports(loadReports()); // another tab saved or withdrew
    };
    // Saves from an earlier visit that were never confirmed (the tab closed, the network
    // was down when Cancel was pressed): nothing shows them, so they are withdrawn.
    const giveUp = () => abandoned().forEach((code) => void discard(code));
    giveUp();
    window.addEventListener("storage", onStorage);
    window.addEventListener("online", giveUp);
    return () => {
      alive.current = false;
      window.removeEventListener("storage", onStorage);
      window.removeEventListener("online", giveUp);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once per sign-in
  }, []);

  /** Withdraw a save that was sent and will not be kept. Its code stays in the browser
   * until the server confirms (also when it never stored it), so a failure is retried. */
  async function discard(code: string) {
    try {
      await withdrawFeedback(token, code);
      removePending(code);
    } catch {
      // unreachable or signed out: tried again on the next visit or when back online
    }
  }

  /** Save an answer for review. If the conversation is gone by the time the server has
   * replied (deleted, or signed out), nobody could withdraw the row any more: remove it. */
  async function report(answerAt: string, feedback: FeedbackCase, code: string) {
    const mine = generation.current;
    addPending(code); // before it leaves: the code must outlive a reload or a lost reply
    await saveFeedback(token, feedback, code);
    if (generation.current !== mine || !alive.current) return discard(code);
    setReports((r) => addReport(r, answerAt, code));
    removePending(code); // kept with the answer now
  }

  async function withdraw(answerAt: string, code: string) {
    await withdrawFeedback(token, code);
    setReports((r) => removeReport(r, answerAt));
  }

  // Block bodies: an effect must return nothing or a cleanup function. scrollIntoView returns a
  // Promise in current browsers, which React would later call as a cleanup and crash.
  useEffect(() => {
    saveTurns(turns);
  }, [turns]);
  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns.length, pending?.started]); // once per question, not on every stage

  async function send(question: string) {
    const date = asOf || null;
    const history = historyFor(turns);
    const asked: Turn = { role: "user", text: question, asOf: date, at: new Date().toISOString() };
    const mine = generation.current;
    setTurns((t) => [...t, asked]);
    setError(null);
    setPending({ question, started: Date.now(), stages: [], followOn: history.length > 0 });
    const controller = new AbortController();
    inFlight.current = controller;
    try {
      const onStage = (stage: Stage) => {
        if (generation.current === mine) setPending((p) => (p ? { ...p, stages: [...p.stages, stage] } : p));
      };
      const response = await askStream(token, question, date, history, conversationId(), onStage, controller.signal);
      if (generation.current !== mine) return; // the conversation was deleted meanwhile
      setRecorded(response.recorded === true); // also when the check at sign-in did not get through
      setTurns((t) => [...t, { role: "assistant", response, at: new Date().toISOString() }]);
    } catch (e) {
      if (generation.current !== mine) return;
      // The question was not answered: take it out of the conversation (it would otherwise be
      // saved and sent as history later) and give it back to edit.
      setTurns((t) => t.slice(0, -1));
      setDraftBack(question);
      if (e instanceof ApiError && e.signedOut) {
        saveTurns(turns); // what was answered before; the next sign-in continues it
        return onSignedOut(e.message);
      }
      setError(e instanceof ApiError ? e.message : "Something went wrong. Try again.");
    } finally {
      if (generation.current === mine) setPending(null);
    }
  }

  /** A click under an answer. Kept with the answer only once the server has it. */
  async function rateAnswer(answerAt: string, traceId: string | null | undefined, helpful: boolean) {
    const mine = generation.current;
    try {
      if (!(await rate(token, traceId, helpful))) return; // not counted: the buttons stay
    } catch (e) {
      if (e instanceof ApiError && e.signedOut) onSignedOut(e.message);
      return; // not saved: the buttons stay, to try again
    }
    if (generation.current !== mine) return;
    setTurns((t) => t.map((turn) => (turn.role === "assistant" && turn.at === answerAt ? { ...turn, rated: helpful ? "helpful" : "not_helpful" } : turn)));
  }

  function deleteConversation() {
    generation.current += 1;
    inFlight.current?.abort(); // stop waiting for an answer nobody will see
    setTurns([]);
    setReports(clearReports());
    setOpen(null);
    setPending(null);
    setConfirmDelete(false);
  }

  return (
    <div className={open ? "app with-panel" : "app"}>
      <header className="bar">
        <div className="brand">
          <span className="mark" aria-hidden="true">P</span>
          <span>PermitNav</span>
        </div>
        <div className="bar-actions">
          {turns.length > 0 && !confirmDelete && (
            <button type="button" className="quiet" onClick={() => setConfirmDelete(true)} aria-label="Delete conversation">
              <span className="wide-only">Delete conversation</span>
              <span className="narrow-only">Delete</span>
            </button>
          )}
          {confirmDelete && (
            <span className="confirm" role="group" aria-label="Confirm delete">
              Delete it from this browser?
              {Object.keys(reports).length > 0 && " What you saved for review can then no longer be withdrawn from here."}
              <button type="button" className="danger" onClick={deleteConversation}>
                Delete
              </button>
              <button type="button" className="quiet" onClick={() => setConfirmDelete(false)}>
                Keep
              </button>
            </span>
          )}
          <button
            type="button"
            className="quiet"
            title={
              Object.keys(reports).length > 0
                ? "Also deletes this conversation from this browser. What you saved for review can then no longer be withdrawn from here."
                : "Also deletes this conversation from this browser"
            }
            onClick={() => onSignedOut("Signed out, and this conversation was deleted from this browser.", true)}
          >
            Sign out
          </button>
        </div>
      </header>

      <main className="thread" aria-live="polite">
        <p className="ai-notice">
          You are talking to an AI. PermitNav answers from Swedish law and Migrationsverket's pages and shows its sources.
          It is not legal advice; check with Migrationsverket before you act.
        </p>
        {recorded && (
          <p className="ai-notice recorded">
            You are signed in with the maintainer's token: your questions and the answers are recorded in full, to improve PermitNav.
          </p>
        )}
        {turns.length === 0 && !pending && <Empty onPick={send} />}
        {turns.map((turn, i) =>
          turn.role === "user" ? (
            <Question key={i} text={turn.text} asOf={turn.asOf} />
          ) : (
            <Answer
              key={i}
              response={turn.response}
              at={turn.at}
              question={turns[i - 1]?.role === "user" ? (turns[i - 1] as UserTurn).text : ""}
              reported={reports[turn.at]}
              rated={turn.rated}
              onRate={(helpful) => rateAnswer(turn.at, turn.response.trace_id, helpful)}
              onReport={(feedback, code) => report(turn.at, feedback, code)}
              onWithdraw={(code) => withdraw(turn.at, code)}
              onDiscard={discard}
              onSignedOut={onSignedOut}
              onOpen={(c) => setOpen({ citation: c, asOf: turn.response.as_of })} active={open?.citation} />
          ),
        )}
        {pending && <Pending started={pending.started} stages={pending.stages} followOn={pending.followOn} />}
        {error && <p className="error" role="alert">{error}</p>}
        <div ref={end} />
      </main>

      <Composer disabled={!!pending} onSend={send} asOf={asOf} onAsOf={setAsOf} restore={draftBack} onRestored={() => setDraftBack(null)} />

      {open && <SourcePanel key={`${open.citation.marker}-${open.citation.version_id ?? open.citation.unit_id}`} token={token} citation={open.citation} onClose={() => setOpen(null)} onSignedOut={onSignedOut} />}
    </div>
  );
}

function Empty({ onPick }: { onPick: (q: string) => void }) {
  return (
    <section className="empty">
      <h1>Ask about Swedish work and residence permits</h1>
      <p className="muted">In English or Swedish. Answers cite the law and Migrationsverket's pages, as they apply on the date you choose.</p>
      <div className="examples">
        {EXAMPLES.map((q) => (
          <button key={q} type="button" className="example" onClick={() => onPick(q)}>
            {q}
          </button>
        ))}
      </div>
    </section>
  );
}

function Question({ text, asOf }: { text: string; asOf: string | null }) {
  return (
    <div className="question">
      <p>{text}</p>
      {asOf && <span className="as-of">Rules as of {readableDate(asOf)}</span>}
    </div>
  );
}

interface AnswerProps {
  response: AskResponse;
  at: string;
  question: string; // what was asked, for the feedback form
  reported?: string; // the withdrawal code, when this answer was saved for review
  rated?: "helpful" | "not_helpful";
  onRate: (helpful: boolean) => Promise<void>;
  onReport: (feedback: FeedbackCase, code: string) => Promise<void>;
  onWithdraw: (code: string) => Promise<void>;
  onDiscard: (code: string) => Promise<void>;
  onSignedOut: (message: string) => void;
  onOpen: (c: Citation) => void;
  active?: Citation;
}

function Answer({ response, at, question, reported, rated, onRate, onReport, onWithdraw, onDiscard, onSignedOut, onOpen, active }: AnswerProps) {
  const [reporting, setReporting] = useState(false);
  const parsed = useMemo(() => blocks(response.text, response.citations), [response]);
  const byMarker = useMemo(() => new Map(response.citations.map((c) => [c.marker, c])), [response]);
  const [copied, setCopied] = useState(false);
  const [rating, setRating] = useState(false); // a click is on its way
  const label = KIND_LABEL[response.kind];
  const situation = Object.entries(response.situation ?? {});
  const lead = hasLead(response.kind, parsed); // rule 9's conclusion, when it has that shape

  function render(parts: Inline[]) {
    return parts.map((p, i) => {
      if (p.type === "text") return <span key={i}>{p.text}</span>;
      if (p.type === "strong") return <strong key={i}>{p.text}</strong>;
      const c = byMarker.get(p.marker);
      if (!c) return null;
      return (
        <button key={i} type="button" className={active === c ? "marker on" : "marker"} onClick={() => onOpen(c)} aria-label={`Source ${p.marker}: ${c.heading}`}>
          {p.marker}
        </button>
      );
    });
  }

  async function copy() {
    const text = asPlainText(response, at);
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      window.prompt?.("Copy the answer:", text);
    }
  }

  return (
    <article className={`answer kind-${response.kind}`}>
      {response.understood_as && (
        <p className="understood">
          <span className="label">Understood as</span> {response.understood_as}
        </p>
      )}
      {label && <span className="kind">{label}</span>}
      {KIND_NOTE[response.kind] && <p className="kind-note">{KIND_NOTE[response.kind]}</p>}
      <div className="answer-text">
        {parsed.map((b, i) =>
          b.type === "paragraph" ? (
            <p key={i} className={i === 0 && lead ? "lead" : undefined}>{render(b.parts)}</p>
          ) : (
            <ul key={i}>
              {b.items.map((item, j) => (
                <li key={j}>{render(item)}</li>
              ))}
            </ul>
          ),
        )}
      </div>
      {response.citations.length > 0 && (
        <ol className="sources">
          {response.citations.map((c) => (
            <li key={c.marker}>
              <button type="button" className={active === c ? "source on" : "source"} onClick={() => onOpen(c)}>
                <span className="source-marker">{c.marker}</span>
                <span className="source-heading">{c.heading}</span>
                <span className="source-dates">{c.dates}</span>
              </button>
            </li>
          ))}
        </ol>
      )}
      {situation.length > 0 && (
        <details className="situation">
          <summary>Your situation, as understood</summary>
          <dl>
            {situation.map(([k, v]) => (
              <div key={k}>
                <dt>{SITUATION_LABEL[k] ?? k}</dt>
                <dd>{v}</dd>
              </div>
            ))}
          </dl>
          <p className="muted">From what you wrote in this conversation. If something is wrong, say so in your next question.</p>
        </details>
      )}
      <footer className="answer-foot">
        <span>Rules as of {readableDate(response.as_of)}</span>
        {response.credits.length > 0 && <span>{response.credits.join(" · ")}</span>}
        <button type="button" className="quiet small" onClick={copy}>
          {copied ? "Copied" : "Copy answer"}
        </button>
        {response.kind !== "failed" && !reported && !reporting && (
          <button type="button" className="quiet small" onClick={() => setReporting(true)}>
            Report a problem
          </button>
        )}
        {response.kind !== "failed" && (
          <span className="rate" role="group" aria-label="Was this answer helpful?">
            {rated ? (
              <span>{rated === "helpful" ? "Marked helpful." : "Marked not helpful."} Only your click was saved.</span>
            ) : (
              <>
                <span title="Saves only your click, with this answer's technical record. No question or answer text is saved.">Helpful?</span>
                {[true, false].map((helpful) => (
                  <button
                    key={String(helpful)}
                    type="button"
                    className="quiet small"
                    disabled={rating}
                    title="Saves only your click. No question or answer text is saved."
                    onClick={async () => {
                      setRating(true);
                      await onRate(helpful);
                      setRating(false);
                    }}
                  >
                    {helpful ? "Yes" : "No"}
                  </button>
                ))}
              </>
            )}
          </span>
        )}
      </footer>
      {reporting && !reported && (
        <FeedbackForm
          question={question}
          response={response}
          onSave={onReport}
          onDiscard={onDiscard}
          onDone={() => setReporting(false)}
          onSignedOut={onSignedOut}
        />
      )}
      {reported && <FeedbackSaved onWithdraw={() => onWithdraw(reported)} onSignedOut={onSignedOut} />}
    </article>
  );
}

interface FeedbackFormProps {
  question: string;
  response: AskResponse;
  onSave: (feedback: FeedbackCase, code: string) => Promise<void>;
  onDiscard: (code: string) => Promise<void>; // withdraw a save that was sent and not kept
  onDone: () => void; // saved or cancelled: close the form
  onSignedOut: (message: string) => void;
}

/** Shows everything that would be saved; nothing is sent before "Save for review". */
function FeedbackForm({ question, response, onSave, onDiscard, onDone, onSignedOut }: FeedbackFormProps) {
  const [asked, setAsked] = useState(() => clip(question, MAX_QUESTION_CHARS));
  const [answer, setAnswer] = useState(() => answerForReview(response));
  const [comment, setComment] = useState("");
  const [asOf, setAsOf] = useState(response.as_of);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [code] = useState(newWithdrawCode); // one per form: saving again replaces, never adds
  // "A save with this code may be stored": set when one is sent, cleared only when one is
  // confirmed. A later refusal says nothing about an earlier attempt whose reply was lost.
  const sent = useRef(false);
  const lastSave = useRef<Promise<unknown>>(Promise.resolve());
  const found = identifiersIn(asked, answer, comment);
  const dated = /^\d{4}-\d{2}-\d{2}$/.test(asOf);
  const ready = asked.trim().length > 0 && answer.trim().length > 0 && dated && found.length === 0 && !saving;
  const kind = response.kind === "failed" ? "answered" : response.kind;

  // Leaving the form (Cancel, or the conversation deleted) with a save that may have been
  // stored: withdraw it, once the save itself has settled so the two cannot cross.
  const discard = useRef(onDiscard);
  discard.current = onDiscard;
  useEffect(
    () => () => {
      if (sent.current) void lastSave.current.then(() => discard.current(code));
    },
    [code],
  );

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!ready) return;
    setSaving(true);
    setError(null);
    sent.current = true;
    const saved = onSave({ question: asked, answer, comment, kind, as_of: asOf, cited: citedUnits(response) }, code);
    lastSave.current = saved.catch(() => {});
    try {
      await saved;
      sent.current = false; // stored, and its code is kept with the answer
      onDone();
    } catch (err) {
      if (err instanceof ApiError && err.signedOut) return onSignedOut(err.message);
      setError(err instanceof ApiError ? err.message : SAVE_UNSURE);
      setSaving(false);
    }
  }

  return (
    <form className="feedback" onSubmit={save} aria-label="Report a problem with this answer">
      <h3>Save this question and answer to help improve the assistant?</h3>
      <p className="muted">
        Nothing has been sent. If you save, what is shown below is stored for the maintainer to review. Nothing else: not
        who you are, and not the rest of this conversation. Edit out anything personal first: no names, case numbers, or
        dates that could identify you.
      </p>
      <label>
        <span>The question you asked</span>
        <textarea value={asked} maxLength={MAX_QUESTION_CHARS} rows={3} onChange={(e) => setAsked(e.target.value)} />
      </label>
      <label>
        <span>The answer you got</span>
        <textarea value={answer} maxLength={MAX_ANSWER_CHARS} rows={6} onChange={(e) => setAnswer(e.target.value)} />
      </label>
      <label>
        <span>What was wrong? (optional)</span>
        <textarea value={comment} maxLength={MAX_COMMENT_CHARS} rows={2} onChange={(e) => setComment(e.target.value)} />
      </label>
      <div className="feedback-also">
        <span className="muted">Also saved with it:</span>
        <label className="inline">
          <span>The date the rules were asked for</span>
          <input type="date" value={asOf} required min="1900-01-01" max="9999-12-31" onChange={(e) => setAsOf(e.target.value)} />
        </label>
        {asOf !== todayIso() && (
          <button type="button" className="quiet small" onClick={() => setAsOf(todayIso())}>
            Use today's date instead
          </button>
        )}
        <span className="muted">Type of answer: {KIND_LABEL[response.kind] ?? "An answer"}</span>
        {response.citations.length > 0 && (
          <span className="muted">Sources it cited: {[...new Set(response.citations.map((c) => c.heading))].join("; ")}</span>
        )}
      </div>
      {found.length > 0 && (
        <p className="error" role="alert">
          The text contains {found.join(" and ")}. Remove it before saving.
        </p>
      )}
      {error && <p className="error" role="alert">{error}</p>}
      <div className="feedback-actions">
        <button type="submit" className="primary" disabled={!ready}>
          {saving ? "Saving…" : "Save for review"}
        </button>
        <button type="button" className="quiet" onClick={onDone} disabled={saving}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function todayIso(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

function FeedbackSaved({ onWithdraw, onSignedOut }: { onWithdraw: () => Promise<void>; onSignedOut: (message: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function withdraw() {
    setBusy(true);
    setError(null);
    try {
      await onWithdraw();
    } catch (err) {
      if (err instanceof ApiError && err.signedOut) return onSignedOut(err.message);
      setError(err instanceof ApiError ? err.message : "It could not be withdrawn. Try again.");
      setBusy(false);
    }
  }

  return (
    <p className="feedback-saved" role="status">
      Saved for review. Thank you.{" "}
      <button type="button" className="quiet small" onClick={withdraw} disabled={busy}>
        {busy ? "Withdrawing…" : "Withdraw it"}
      </button>
      <span className="muted"> You can withdraw it from here until you delete this conversation or sign out.</span>
      {error && <span className="error"> {error}</span>}
    </p>
  );
}

const STAGE_LABEL: Record<Stage, string> = {
  understanding: "Reading your earlier questions",
  searching: "Searching the law and Migrationsverket's pages",
  checking: "Checking that the sources are enough",
  writing: "Writing the answer with its sources",
};

function Pending({ started, stages, followOn }: { started: number; stages: Stage[]; followOn: boolean }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  // The steps this answer will take: the ones heard so far, then the ones still to come.
  const planned: Stage[] = [...(followOn ? (["understanding"] as Stage[]) : []), "searching", "writing"];
  const steps = [...stages, ...planned.filter((s) => !stages.includes(s))];
  const current = stages[stages.length - 1];
  const seconds = Math.floor((now - started) / 1000);
  return (
    <div className="pending">
      {/* Only the current step is announced; the list and the seconds are for the eye. */}
      <span className="sr-only" role="status" aria-live="polite">
        {current ? STAGE_LABEL[current] : "Preparing the answer"}
      </span>
      <ol className="steps" aria-hidden="true">
        {steps.map((step) => {
          const state = step === current ? "now" : stages.includes(step) ? "done" : "next";
          return (
            <li key={step} className={`step ${state}`}>
              <span className="step-mark" aria-hidden="true">{state === "done" ? "✓" : ""}</span>
              {STAGE_LABEL[step]}
              {state === "now" && "…"}
            </li>
          );
        })}
      </ol>
      <span className="elapsed" aria-hidden="true">{seconds} s</span>
    </div>
  );
}

function Composer({ disabled, onSend, asOf, onAsOf, restore, onRestored }: { disabled: boolean; onSend: (q: string) => void; asOf: string; onAsOf: (d: string) => void; restore: string | null; onRestored: () => void }) {
  const [text, setText] = useState("");
  const box = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    if (restore !== null) {
      setText(restore);
      onRestored();
    }
  }, [restore, onRestored]);
  useEffect(() => {
    // grow with the text, up to a third of the screen; then scroll inside
    const el = box.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, window.innerHeight * 0.33)}px`;
  }, [text]);
  const tooLong = text.length > 2000;

  function submit(event?: React.FormEvent) {
    event?.preventDefault();
    const q = text.trim();
    if (!q || disabled || tooLong) return;
    onSend(q);
    setText("");
  }

  return (
    <form className="composer" onSubmit={submit}>
      <div className="composer-box">
        <textarea
          ref={box}
          id="question"
          aria-label="Your question"
          rows={1}
          placeholder="Ask a question, or follow up…"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            // Not while an input method is composing (Chinese, Japanese, Korean…): that Enter commits text.
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing && e.keyCode !== 229) {
              e.preventDefault();
              submit();
            }
          }}
        />
        <div className="composer-row">
          <DateChip asOf={asOf} onAsOf={onAsOf} />
          {tooLong && <span className="error small">{text.length.toLocaleString("en")} / 2,000</span>}
          <button type="submit" className="send" disabled={disabled || !text.trim() || tooLong} aria-label="Ask" title="Ask (Enter)">
            <svg viewBox="0 0 24 24" aria-hidden="true" width="18" height="18">
              <path d="M12 19V5M5 12l7-7 7 7" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
        </div>
      </div>
      <p className="composer-hint">Enter to ask · Shift+Enter for a new line</p>
    </form>
  );
}

/** Today in the person's own time zone, as the date input writes it (YYYY-MM-DD). */
function today(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** "Rules as of": today unless the person picks a date; a set date stays visible until cleared. */
function DateChip({ asOf, onAsOf }: { asOf: string; onAsOf: (d: string) => void }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const outside = (e: MouseEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    const escape = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  return (
    <div className="date-chip-wrap" ref={root}>
      <button type="button" className={asOf ? "date-chip set" : "date-chip"} aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen((o) => !o)}>
        <svg viewBox="0 0 24 24" aria-hidden="true" width="15" height="15">
          <rect x="3.5" y="5" width="17" height="15" rx="2.5" fill="none" stroke="currentColor" strokeWidth="1.8" />
          <path d="M3.5 10h17M8 3v4M16 3v4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        </svg>
        Rules as of {asOf ? readableDate(asOf) : "today"}
      </button>
      {asOf && (
        <button type="button" className="date-clear" aria-label="Back to today's rules" title="Back to today" onClick={() => onAsOf("")}>
          ×
        </button>
      )}
      {open && (
        <div className="date-pop" role="dialog" aria-label="Rules as of">
          <p>Answers use the law and Migrationsverket's pages as they applied on this date. Leave it on today unless your case depends on an earlier or a coming date, for example when your permit was granted.</p>
          <label htmlFor="as-of">Date</label>
          <input id="as-of" type="date" value={asOf || today()} min="2023-01-01" onChange={(e) => onAsOf(e.target.value === today() ? "" : e.target.value)} />
          <div className="date-pop-actions">
            {asOf && (
              <button type="button" className="quiet" onClick={() => { onAsOf(""); setOpen(false); }}>
                Back to today
              </button>
            )}
            <button type="button" className="primary small" onClick={() => setOpen(false)}>
              Done
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/** A source's full text with the cited passage marked. No automatic scroll: the panel opens
 * on its heading, the quote and its translation; a button goes to the passage. */
function FullText({ source: full, quote }: { source: Source; quote: string }) {
  const parts = useMemo(() => (quote ? locateQuote(full.text, quote) : null), [full.text, quote]);
  const mark = useRef<HTMLElement>(null);
  return (
    <>
      {parts && (
        <button type="button" className="quiet small jump" onClick={() => mark.current?.scrollIntoView({ block: "center", behavior: "smooth" })}>
          Go to the cited passage ↓
        </button>
      )}
      <div className="full-text" lang={full.unit_id.startsWith("web:en") ? "en" : "sv"}>
        {parts ? (
          <>
            {parts[0]}
            <mark ref={mark}>{parts[1]}</mark>
            {parts[2]}
          </>
        ) : (
          full.text
        )}
      </div>
    </>
  );
}

function SourcePanel({ token, citation, onClose, onSignedOut }: { token: string; citation: Citation; onClose: () => void; onSignedOut: (m: string) => void }) {
  const [full, setFull] = useState<Source | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    setFull(null);
    setError(null);
    if (!citation.version_id) {
      setError("The full text is not available for answers saved before this version; open the original.");
      return;
    }
    source(token, citation.version_id).then(
      (s) => live && setFull(s),
      (e) => {
        if (!live) return;
        if (e instanceof ApiError && e.signedOut) onSignedOut(e.message);
        else setError(e instanceof ApiError ? e.message : "The source could not be loaded.");
      },
    );
    return () => {
      live = false;
    };
  }, [token, citation.version_id, onSignedOut]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <aside className="panel" aria-label={`Source ${citation.marker}`}>
      <header className="panel-head">
        <span className="source-marker">{citation.marker}</span>
        <h2>{citation.heading}</h2>
        <button type="button" className="quiet" onClick={onClose} aria-label="Close source">
          Close
        </button>
      </header>
      <p className="panel-dates">{citation.dates}</p>
      {citation.quote && (
        <blockquote lang="sv">
          {citation.quote}
          {citation.translation && <span className="translation">Unofficial translation: {citation.translation}</span>}
        </blockquote>
      )}
      <h3>Full text of the cited wording</h3>
      {error && <p className="error">{error}</p>}
      {!full && !error && <p className="muted">Loading…</p>}
      {full && <FullText source={full} quote={citation.quote} />}
      <a className="external" href={citation.url} target="_blank" rel="noreferrer noopener">
        Open the original
      </a>
    </aside>
  );
}
