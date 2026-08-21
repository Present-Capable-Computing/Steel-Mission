import {useEffect, useRef, useState} from "preact/hooks";

import {
  answerDecision,
  chatAnswerText,
  chatIsActive,
  chatTokenUsageLabel,
  pollChatJob,
  sendFollowUp,
  startChat,
  type ChatJob,
  type ChatMessage,
  type ChatRequester,
} from "./chat";
import type {WorkMode} from "./work-mode";


interface ChatPanelProps {
  request: ChatRequester;
  workMode: WorkMode;
  profile: string;
}

const POLL_INTERVAL_MS = 1200;

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

export function ChatPanel({request, workMode, profile}: ChatPanelProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [job, setJob] = useState<ChatJob | null>(null);
  const [decisionOption, setDecisionOption] = useState("");
  const [decisionContext, setDecisionContext] = useState("");
  const mounted = useRef(true);

  useEffect(() => () => {
    mounted.current = false;
  }, []);

  const finishJob = (completed: ChatJob) => {
    const failed = completed.state === "error" || completed.ok === false;
    setMessages((current) => [...current, {
      role: "assistant",
      content: chatAnswerText(completed),
      error: failed,
    }]);
    // Retain the terminal progress record as the status strip for the last
    // invocation. A new question replaces it with the next running job.
    setJob(completed);
  };

  const watchJob = async (jobId: string) => {
    try {
      while (mounted.current) {
        await delay(POLL_INTERVAL_MS);
        if (!mounted.current) return;
        const next = await pollChatJob(request, jobId);
        if (!mounted.current) return;
        setJob(next);
        const requestState = next.progress?.decisionRequest;
        if (requestState) {
          const defaultOption = requestState.defaultOptionId || requestState.options.find((option) => option.default)?.id || "";
          setDecisionOption((current) => current || defaultOption);
        }
        if (!chatIsActive(next)) {
          finishJob(next);
          return;
        }
      }
    } catch (error: unknown) {
      if (!mounted.current) return;
      setMessages((current) => [...current, {
        role: "assistant",
        content: error instanceof Error ? error.message : "Delivery Coordinator job status could not be read.",
        error: true,
      }]);
      setJob(null);
    }
  };

  const submit = async (event: Event) => {
    event.preventDefault();
    const content = draft.trim();
    if (!content) return;
    setDraft("");
    setMessages((current) => [...current, {role: "user", content}]);
    if (chatIsActive(job) && job) {
      try {
        const updated = await sendFollowUp(request, job.jobId, content);
        setJob((current) => current ? {...current, ...updated} : updated);
      } catch (error: unknown) {
        setMessages((current) => [...current, {
          role: "assistant",
          content: error instanceof Error ? error.message : "Follow-up could not be applied.",
          error: true,
        }]);
      }
      return;
    }
    try {
      const started = await startChat(request, {
        question: content,
        messages,
        workMode,
        ...(profile ? {profile} : {}),
      });
      setJob(started);
      void watchJob(started.jobId);
    } catch (error: unknown) {
      setMessages((current) => [...current, {
        role: "assistant",
        content: error instanceof Error ? error.message : "Delivery Coordinator job did not start.",
        error: true,
      }]);
      setJob(null);
    }
  };

  const submitDecision = async (event: Event) => {
    event.preventDefault();
    if (!job) return;
    try {
      const updated = await answerDecision(request, job.jobId, decisionOption, decisionContext);
      setJob((current) => current ? {...current, ...updated} : updated);
      setDecisionContext("");
    } catch (error: unknown) {
      setMessages((current) => [...current, {
        role: "assistant",
        content: error instanceof Error ? error.message : "Decision could not be applied.",
        error: true,
      }]);
    }
  };

  const active = chatIsActive(job);
  const decision = active ? job?.progress?.decisionRequest : undefined;
  const progressLabel = job?.progress?.phase || (job?.state === "paused"
    ? "Delivery Coordinator is paused."
    : "Delivery Coordinator is checking the worker-visible state.");

  return (
    <section class="chat-panel" aria-labelledby="chatTitle">
      <header>
        <p class="eyebrow">Delivery Coordinator</p>
        <h1 id="chatTitle">Ask Steel Mission</h1>
        <p>Ask a question, add context while the answer is running, or respond when the coordinator needs a decision.</p>
      </header>
      <div id="chatConversation" class="chat-conversation" aria-live="polite">
        {messages.length === 0 && !job && (
          <p class="chat-empty">Ask what is unverified, what needs attention, or what should happen next.</p>
        )}
        {messages.map((message, index) => (
          <article key={`${message.role}-${index}`} class={`chat-message ${message.role}${message.error ? " error" : ""}`}>
            <strong>{message.role === "user" ? "You" : "Delivery Coordinator"}</strong>
            <p>{message.content}</p>
          </article>
        ))}
        {job && (
          <article class="chat-message assistant chat-progress" data-job-state={job.state}>
            <strong>Delivery Coordinator</strong>
            <p>{progressLabel}</p>
            <small>{chatTokenUsageLabel(job.progress)}</small>
            {decision && (
              <form class="decision-request" onSubmit={submitDecision}>
                <fieldset>
                  <legend>{decision.question || "Choose how the Delivery Coordinator should continue."}</legend>
                  {decision.context && <p>{decision.context}</p>}
                  {decision.options.map((option) => (
                    <label key={option.id}>
                      <input
                        type="radio"
                        name={`decision-${decision.id || job.jobId}`}
                        value={option.id}
                        checked={decisionOption === option.id}
                        onChange={() => setDecisionOption(option.id)}
                      />
                      <span><strong>{option.label}</strong>{option.description && <small>{option.description}</small>}</span>
                    </label>
                  ))}
                </fieldset>
                <textarea
                  value={decisionContext}
                  placeholder={decision.freeText?.placeholder || "Add context or a different preference…"}
                  onInput={(event) => setDecisionContext(event.currentTarget.value)}
                />
                <button type="submit">Continue</button>
              </form>
            )}
          </article>
        )}
      </div>
      <form id="chatComposer" class="chat-composer" onSubmit={submit}>
        <label for="chatQuestion">{active ? "Add a follow-up" : "Message Delivery Coordinator"}</label>
        <textarea
          id="chatQuestion"
          value={draft}
          placeholder={active ? "Add context or redirect the running answer…" : "Where are we, what is unverified, and what needs my attention?"}
          onInput={(event) => setDraft(event.currentTarget.value)}
          required
        />
        <button id="chatSend" type="submit">{active ? "Send follow-up" : "Ask Delivery Coordinator"}</button>
      </form>
    </section>
  );
}
