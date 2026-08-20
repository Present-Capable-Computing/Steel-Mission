import type {AccessLevel} from "./capabilities";
import type {ApiRequester} from "./organizations";

export interface MutationRecord {
  mutationId: string;
  action?: string;
  status?: string;
  actorRole?: string;
  producedAt?: string;
  targetPath?: string;
  beforeHash?: string;
  afterHash?: string;
  changed?: boolean;
  details?: Record<string, unknown>;
  [key: string]: unknown;
}

export function auditPanelAvailable(accessLevel: unknown): accessLevel is "owner" | "admin" {
  return accessLevel === "owner" || accessLevel === "admin";
}

export async function loadMutationLedger(request: ApiRequester, accessLevel: AccessLevel): Promise<MutationRecord[]> {
  if (!auditPanelAvailable(accessLevel)) throw new Error("owner or admin access is required to view the mutation ledger");
  const response = await request(`/api/${accessLevel}/mutations`, {cache: "no-store"});
  const result = await response.json() as {ok?: boolean; error?: string; mutations?: unknown[]};
  if (!response.ok || !result.ok) throw new Error(result.error || "Mutation ledger is unavailable");
  return Array.isArray(result.mutations)
    ? result.mutations.filter((item): item is MutationRecord => Boolean(item) && typeof item === "object" && typeof (item as MutationRecord).mutationId === "string")
    : [];
}
