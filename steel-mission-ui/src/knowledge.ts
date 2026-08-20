import type {AccessLevel} from "./capabilities";
import type {ApiRequester} from "./organizations";

export interface KnowledgeSources {
  repositories: unknown[];
  documents: unknown[];
}

export interface KnowledgeRegistry {
  ok?: boolean;
  canManageGeneralKnowledge?: boolean;
  roles?: Array<Record<string, unknown>>;
  capabilities?: Array<Record<string, unknown>>;
  foundations?: Array<Record<string, unknown>>;
  knowledgeDomains?: Array<Record<string, unknown>>;
  generalKnowledge?: KnowledgeSources;
  effectiveKnowledge?: KnowledgeSources;
  knowledgeQuality?: Record<string, unknown>;
  activeOrganization?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface KnowledgeUpload {
  label: string;
  sourceKind: "files" | "folder";
  organizationId?: string;
  profile?: string;
  files: Array<Record<string, unknown>>;
}

export function knowledgeManagementAvailable(accessLevel: unknown): accessLevel is "owner" | "admin" {
  return accessLevel === "owner" || accessLevel === "admin";
}

function assertManager(accessLevel: AccessLevel): asserts accessLevel is "owner" | "admin" {
  if (!knowledgeManagementAvailable(accessLevel)) throw new Error("owner or admin access is required to manage knowledge");
}

async function checked(response: Response, fallback: string): Promise<Record<string, unknown>> {
  const result = await response.json() as Record<string, unknown> & {ok?: boolean; error?: string};
  if (!response.ok || !result.ok) throw new Error(String(result.error || fallback));
  return result;
}

export async function loadKnowledgeRegistry(request: ApiRequester, accessLevel: AccessLevel): Promise<KnowledgeRegistry> {
  const response = await request(`/api/${accessLevel}/knowledge`, {cache: "no-store"});
  return checked(response, "Knowledge registry is unavailable") as Promise<KnowledgeRegistry>;
}

export async function saveKnowledgeSources(
  request: ApiRequester,
  accessLevel: AccessLevel,
  sources: KnowledgeSources,
): Promise<KnowledgeSources> {
  assertManager(accessLevel);
  const response = await request(`/api/${accessLevel}/knowledge`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(sources),
  });
  const result = await checked(response, "Knowledge sources could not be saved");
  return result.payload as KnowledgeSources;
}

export async function previewPreparedSnapshot(
  request: ApiRequester,
  accessLevel: AccessLevel,
): Promise<Record<string, unknown>> {
  assertManager(accessLevel);
  const response = await request(`/api/${accessLevel}/knowledge/prepared`, {cache: "no-store"});
  const result = await checked(response, "Prepared snapshot is unavailable");
  return result.payload as Record<string, unknown>;
}

export async function uploadKnowledge(
  request: ApiRequester,
  accessLevel: AccessLevel,
  payload: KnowledgeUpload,
): Promise<Record<string, unknown>> {
  assertManager(accessLevel);
  const response = await request(`/api/${accessLevel}/knowledge/upload`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload),
  });
  const result = await checked(response, "Knowledge upload failed");
  return result.payload as Record<string, unknown>;
}
