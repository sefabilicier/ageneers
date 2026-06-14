import { useState, useRef, useEffect } from "react";
import { Send, Bot, CornerDownLeft, RotateCcw } from "lucide-react";
import { INTAKE_STEPS } from "./pipelineConfig";

/**
 * Sequential chatbot-style intake.
 * Asks one question at a time; once all are answered, calls onSubmit
 * with the collected answers. Renders a conversation log of
 * bot prompts and user answers as chat bubbles.
 */
export default function TaskIntake({ onSubmit, disabled }) {
  const [stepIndex, setStepIndex] = useState(0);
  const [answers, setAnswers] = useState({});
  const [draft, setDraft] = useState("");
  const [history, setHistory] = useState([
    { role: "bot", text: INTAKE_STEPS[0].prompt },
  ]);
  const scrollRef = useRef(null);
  const inputRef = useRef(null);

  const currentStep = INTAKE_STEPS[stepIndex];
  const isComplete = stepIndex >= INTAKE_STEPS.length;

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [history]);

  useEffect(() => {
    if (!disabled && !isComplete) inputRef.current?.focus();
  }, [stepIndex, disabled, isComplete]);

  function commitAnswer() {
    const trimmed = draft.trim();
    if (!trimmed && !currentStep.optional) return;

    const displayValue = trimmed || (currentStep.optional ? "main (default)" : "");
    const storedValue = currentStep.multiline
      ? trimmed.split("\n").map((s) => s.trim()).filter(Boolean)
      : (trimmed || (currentStep.key === "branch" ? "main" : ""));

    setAnswers((prev) => ({ ...prev, [currentStep.key]: storedValue }));
    setHistory((prev) => [
      ...prev,
      { role: "user", text: displayValue },
    ]);
    setDraft("");

    const nextIndex = stepIndex + 1;
    if (nextIndex < INTAKE_STEPS.length) {
      setHistory((prev) => [...prev, { role: "bot", text: INTAKE_STEPS[nextIndex].prompt }]);
      setStepIndex(nextIndex);
    } else {
      setStepIndex(nextIndex); // mark complete
      setHistory((prev) => [
        ...prev,
        { role: "bot", text: "All set — review your task below and launch the pipeline when ready." },
      ]);
    }
  }

  function handleKeyDown(e) {
    if (e.key === "Enter" && !(currentStep.type === "textarea" && !e.metaKey && !e.ctrlKey)) {
      e.preventDefault();
      commitAnswer();
    }
  }

  function handleReset() {
    setStepIndex(0);
    setAnswers({});
    setDraft("");
    setHistory([{ role: "bot", text: INTAKE_STEPS[0].prompt }]);
  }

  function handleLaunch() {
    const taskId = `TASK-${Math.floor(100000 + Math.random() * 900000)}`;
    onSubmit({
      taskId,
      title: answers.title || "Untitled task",
      repository: answers.repository,
      branch: answers.branch || "main",
      requirement: answers.requirement || "",
      criteria: answers.criteria || [],
    });
  }

  return (
    <div className="intake">
      <div className="intake__header">
        <div className="intake__header-title">
          <Bot size={16} />
          <span>New Task</span>
        </div>
        <button className="intake__reset" onClick={handleReset} title="Start over">
          <RotateCcw size={13} />
        </button>
      </div>

      <div className="intake__log" ref={scrollRef}>
        {history.map((msg, i) => (
          <div key={i} className={`chat-bubble chat-bubble--${msg.role}`}>
            {msg.role === "bot" && <Bot size={13} className="chat-bubble__icon" />}
            <span className="chat-bubble__text">{msg.text}</span>
          </div>
        ))}

        {isComplete && (
          <div className="intake__summary">
            <div className="intake__summary-title">Task summary</div>
            <dl>
              <dt>Title</dt><dd>{answers.title}</dd>
              <dt>Repository</dt><dd className="mono">{answers.repository}</dd>
              <dt>Branch</dt><dd className="mono">{answers.branch || "main"}</dd>
              <dt>Requirement</dt><dd>{answers.requirement}</dd>
              <dt>Criteria</dt>
              <dd>
                <ul>
                  {(answers.criteria || []).map((c, i) => <li key={i}>{c}</li>)}
                </ul>
              </dd>
            </dl>
          </div>
        )}
      </div>

      <div className="intake__composer">
        {!isComplete ? (
          currentStep.type === "textarea" ? (
            <textarea
              ref={inputRef}
              className="intake__input intake__input--textarea"
              placeholder={currentStep.placeholder}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={handleKeyDown}
              disabled={disabled}
              rows={4}
            />
          ) : (
            <input
              ref={inputRef}
              className="intake__input"
              placeholder={currentStep.placeholder}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={handleKeyDown}
              disabled={disabled}
            />
          )
        ) : null}

        {!isComplete ? (
          <button className="intake__send" onClick={commitAnswer} disabled={disabled}>
            <Send size={14} />
          </button>
        ) : (
          <button className="intake__launch" onClick={handleLaunch} disabled={disabled}>
            {disabled ? "Running…" : "Launch pipeline"}
            {!disabled && <CornerDownLeft size={14} />}
          </button>
        )}
      </div>

      {!isComplete && currentStep.multiline && (
        <div className="intake__hint">One item per line · Enter for newline</div>
      )}
    </div>
  );
}
