import type {WorkMode} from "./work-mode";

export type ChatRequester = (path: string, init?: RequestInit) => Promise<Response>;

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  error?: boolean;
}

export interface DecisionOption {
  id: string;
  label: string;
  description?: string;
  default?: boolean;
}

export interface DecisionRequest {
  id?: string;
  question?: string;
  context?: string;
  defaultOptionId?: string;
  options: DecisionOption[];
  freeText?: {placeholder?: string};
}

export interface ChatProgress {
  phase?: string;
  provider?: string;
  providerLabel?: string;
  model?: string;
  thinkingTokens?: number;
  decisionRequest?: DecisionRequest;
}

export interface ChatJob {
  ok?: boolean;
  jobId: string;
  state: string;
  progress?: ChatProgress;
  payload?: Record<string, unknown>;
  error?: string;
}

export interface StartChatInput {
  question: string;
  messages: ChatMessage[];
  workMode: WorkMode;
}

async function responsePayload(response: Response, fallback: string): Promise<ChatJob> {
  const payload = await response.json() as Partial<ChatJob>;
  if (!response.ok || typeof payload.jobId !== "string") {
    throw new Error(payload.error || fallback);
  }
  return payload as ChatJob;
}

export function chatIsActive(job: ChatJob | null): boolean {
  return Boolean(job && ["running", "waiting_for_decision", "paused"].includes(job.state));
}

export async function startChat(request: ChatRequester, input: StartChatInput): Promise<ChatJob> {
  const response = await request("/api/chat", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    cache: "no-store",
    body: JSON.stringify(input),
  });
  return responsePayload(response, "Delivery Coordinator job did not start");
}

export async function pollChatJob(request: ChatRequester, jobId: string): Promise<ChatJob> {
  const response = await request(`/api/chat/${encodeURIComponent(jobId)}`, {cache: "no-store"});
  return responsePayload(response, "Delivery Coordinator job status could not be read");
}

export async function sendFollowUp(request: ChatRequester, jobId: string, content: string): Promise<ChatJob> {
  const response = await request(`/api/chat/${encodeURIComponent(jobId)}/follow-up`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    cache: "no-store",
    body: JSON.stringify({content}),
  });
  return responsePayload(response, "Follow-up could not be applied");
}

export async function answerDecision(
  request: ChatRequester,
  jobId: string,
  optionId: string,
  freeText: string,
): Promise<ChatJob> {
  const response = await request(`/api/chat/${encodeURIComponent(jobId)}/decision`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    cache: "no-store",
    body: JSON.stringify({optionId, freeText}),
  });
  return responsePayload(response, "Decision could not be applied");
}

export function chatAnswerText(job: ChatJob): string {
  const payload = job.payload && typeof job.payload === "object" ? job.payload : {};
  for (const value of (payload as {summary?: unknown; reason?: unknown; error?: unknown}).summary
    ? [(payload as {summary?: unknown}).summary]
    : [(payload as {reason?: unknown}).reason, (payload as {error?: unknown}).error, job.error]) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  if (job.state === "cancelled") return "Delivery Coordinator job was cancelled.";
  if (job.state === "done" && job.ok !== false) return "Delivery Coordinator finished without a written summary.";
  return job.error || "Delivery Coordinator request failed.";
}
