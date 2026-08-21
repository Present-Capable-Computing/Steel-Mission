export type MissionProgressRequester = (path: string, init?: RequestInit) => Promise<Response>;

export interface AgentSessionStatus {
  sessionId: string;
  issue: {
    repository: string;
    number: number;
    url: string;
  };
  stage: "plan" | "develop-and-commit" | "review-loop" | "final-review-and-merge";
  state: "idle" | "working" | "waiting-on-person" | "succeeded" | "failed" | "cancelled" | "budget-exhausted";
  worker: {
    id: string;
    provider: string;
    model: string;
    role: string;
  };
  machineAccount: string;
  budgetLimit: {elapsedSeconds: number; turns: number};
  budgetSpent: {elapsedSeconds: number; turns: number};
  lastEvent: {summary: string};
  pendingDecision?: {question: string; url?: string};
}

export interface MissionProgressError {
  line: number;
  message: string;
}

export interface MissionProgress {
  sessions: AgentSessionStatus[];
  errors: MissionProgressError[];
  unavailable?: string;
}

const STAGE_LABELS: Record<AgentSessionStatus["stage"], string> = {
  "plan": "Plan",
  "develop-and-commit": "Develop and commit",
  "review-loop": "Review loop",
  "final-review-and-merge": "Final review and merge",
};

const STATE_LABELS: Record<AgentSessionStatus["state"], string> = {
  "idle": "Idle",
  "working": "Working",
  "waiting-on-person": "Waiting on Person",
  "succeeded": "Succeeded",
  "failed": "Failed",
  "cancelled": "Cancelled",
  "budget-exhausted": "Budget exhausted",
};

function isObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object";
}

function isSession(value: unknown): value is AgentSessionStatus {
  if (!isObject(value) || !isObject(value.issue) || !isObject(value.worker)) return false;
  if (!isObject(value.budgetLimit) || !isObject(value.budgetSpent) || !isObject(value.lastEvent)) return false;
  return (
    typeof value.sessionId === "string"
    && typeof value.issue.repository === "string"
    && typeof value.issue.number === "number"
    && typeof value.issue.url === "string"
    && typeof value.stage === "string" && value.stage in STAGE_LABELS
    && typeof value.state === "string" && value.state in STATE_LABELS
    && typeof value.worker.id === "string"
    && typeof value.worker.provider === "string"
    && typeof value.worker.model === "string"
    && typeof value.worker.role === "string"
    && typeof value.machineAccount === "string"
    && typeof value.budgetLimit.elapsedSeconds === "number"
    && typeof value.budgetLimit.turns === "number"
    && typeof value.budgetSpent.elapsedSeconds === "number"
    && typeof value.budgetSpent.turns === "number"
    && typeof value.lastEvent.summary === "string"
  );
}

function isFeedError(value: unknown): value is MissionProgressError {
  return isObject(value) && typeof value.line === "number" && typeof value.message === "string";
}

export async function loadMissionProgress(request: MissionProgressRequester): Promise<MissionProgress> {
  const response = await request("/api/agent-sessions", {cache: "no-store"});
  const payload = await response.json() as Record<string, unknown> & {ok?: boolean; error?: string};
  if (!response.ok || !payload.ok) {
    throw new Error(String(payload.error || "Mission progress is unavailable"));
  }
  const sessions = Array.isArray(payload.sessions) ? payload.sessions.filter(isSession) : [];
  const errors = Array.isArray(payload.errors) ? payload.errors.filter(isFeedError) : [];
  const omitted = Array.isArray(payload.sessions) ? payload.sessions.length - sessions.length : 0;
  return {
    sessions,
    errors: omitted
      ? [...errors, {line: 0, message: `${omitted} malformed API session record${omitted === 1 ? " was" : "s were"} rejected`}]
      : errors,
  };
}

export function unavailableMissionProgress(error: unknown): MissionProgress {
  return {
    sessions: [],
    errors: [],
    unavailable: error instanceof Error ? error.message : "Mission progress is unavailable",
  };
}

export function MissionProgressPanel({progress}: {progress: MissionProgress | null}) {
  return (
    <section
      id="missionProgressPanel"
      class="mission-progress"
      aria-labelledby="missionProgressTitle"
      aria-live="polite"
    >
      <header>
        <div>
          <p class="eyebrow">Mission pipeline</p>
          <h2 id="missionProgressTitle">Live progress</h2>
        </div>
        <p>Read-only status from the canonical agent-session feed.</p>
      </header>
      {!progress && <p class="mission-progress-empty">Loading live sessions…</p>}
      {progress?.unavailable && <p role="alert" class="mission-progress-error">{progress.unavailable}</p>}
      {progress?.errors.map((error) => (
        <p key={`${error.line}:${error.message}`} role="alert" class="mission-progress-error">
          {error.line > 0 ? `Feed line ${error.line} rejected` : "Feed record rejected"}: {error.message}
        </p>
      ))}
      {progress && !progress.unavailable && progress.sessions.length === 0 && (
        <p class="mission-progress-empty">No live mission sessions.</p>
      )}
      {Boolean(progress?.sessions.length) && (
        <div class="mission-progress-grid">
          {progress?.sessions.map((session) => (
            <article key={session.sessionId} data-session-state={session.state}>
              <header>
                <a href={session.issue.url}>
                  {session.issue.repository} #{session.issue.number}
                </a>
                <span>{STATE_LABELS[session.state]}</span>
              </header>
              <dl>
                <div><dt>Stage</dt><dd>{STAGE_LABELS[session.stage]}</dd></div>
                <div><dt>Worker</dt><dd>{session.machineAccount} · {session.worker.id}</dd></div>
                <div>
                  <dt>Budget</dt>
                  <dd>
                    {session.budgetSpent.turns} / {session.budgetLimit.turns} turns · {" "}
                    {session.budgetSpent.elapsedSeconds} / {session.budgetLimit.elapsedSeconds} seconds
                  </dd>
                </div>
              </dl>
              <p class="mission-progress-event">{session.lastEvent.summary}</p>
              {session.state === "waiting-on-person" && session.pendingDecision && (
                <p class="mission-progress-decision">
                  <strong>Waiting on Person</strong>
                  <span>{session.pendingDecision.question}</span>
                  {session.pendingDecision.url && <a href={session.pendingDecision.url}>View decision</a>}
                </p>
              )}
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
