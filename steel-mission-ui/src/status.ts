export type StatusRequester = (path: string, init?: RequestInit) => Promise<Response>;

export interface ProviderStatus {
  id: "claude" | "codex" | "glimmer";
  label: string;
  connection: string;
  activity: "idle" | "working";
  jobCount: number;
  thinkingTokens?: number;
}

export interface ConsoleStatus {
  server: string;
  authority: string;
  providers: ProviderStatus[];
  error?: string;
}

const PROVIDERS: Array<Pick<ProviderStatus, "id" | "label">> = [
  {id: "claude", label: "Claude"},
  {id: "codex", label: "Codex"},
  {id: "glimmer", label: "Glimmer"},
];

async function checked(response: Response, fallback: string): Promise<Record<string, unknown>> {
  const payload = await response.json() as Record<string, unknown> & {ok?: boolean; error?: string};
  if (!response.ok || !payload.ok) throw new Error(String(payload.error || fallback));
  return payload;
}

function providerStatus(value: unknown, expected: Pick<ProviderStatus, "id" | "label">): ProviderStatus {
  const provider = value && typeof value === "object" ? value as Record<string, unknown> : {};
  const usage = provider.tokenUsage && typeof provider.tokenUsage === "object"
    ? provider.tokenUsage as Record<string, unknown>
    : {};
  const thinkingTokens = typeof usage.thinkingTokens === "number" ? usage.thinkingTokens : undefined;
  return {
    id: expected.id,
    label: typeof provider.label === "string" ? provider.label : expected.label,
    connection: typeof provider.connection === "string" ? provider.connection : "unavailable",
    activity: provider.activity === "working" ? "working" : "idle",
    jobCount: typeof provider.jobCount === "number" ? provider.jobCount : 0,
    ...(thinkingTokens === undefined ? {} : {thinkingTokens}),
  };
}

export async function loadConsoleStatus(request: StatusRequester): Promise<ConsoleStatus> {
  const [healthResponse, readinessResponse] = await Promise.all([
    request("/api/health", {cache: "no-store"}),
    request("/api/control-plane/readiness", {cache: "no-store"}),
  ]);
  const health = await checked(healthResponse, "Server health is unavailable");
  const readiness = await checked(readinessResponse, "Authority readiness is unavailable");
  const reportedProviders = Array.isArray(health.providers) ? health.providers : [];
  const providers = PROVIDERS.map((expected) => providerStatus(
    reportedProviders.find((provider) => (
      Boolean(provider) && typeof provider === "object" && (provider as Record<string, unknown>).id === expected.id
    )),
    expected,
  ));
  const authority = readiness.meetsProductionTarget === true
    ? "Production ready"
    : readiness.meetsAlphaTarget === true
      ? "Alpha ready"
      : "Not ready";
  return {server: "Connected", authority, providers};
}

export function unavailableConsoleStatus(error: unknown): ConsoleStatus {
  return {
    server: "Unavailable",
    authority: "Unavailable",
    providers: PROVIDERS.map((provider) => ({
      ...provider,
      connection: "unavailable",
      activity: "idle",
      jobCount: 0,
    })),
    error: error instanceof Error ? error.message : "Live status is unavailable",
  };
}
