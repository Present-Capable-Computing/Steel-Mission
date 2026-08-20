import type {AccessLevel} from "./capabilities";
import type {ApiRequester} from "./organizations";

export type ModelRole = Record<string, unknown> & {id: string; title?: string};
export type ModelDescriptor = Record<string, unknown> & {id: string; provider?: string};

export interface ModelRoleRegistry {
  roles: ModelRole[];
  models: ModelDescriptor[];
  [key: string]: unknown;
}

export function modelRoleManagementAvailable(accessLevel: unknown): accessLevel is "owner" | "admin" {
  return accessLevel === "owner" || accessLevel === "admin";
}

function assertManager(accessLevel: AccessLevel): asserts accessLevel is "owner" | "admin" {
  if (!modelRoleManagementAvailable(accessLevel)) throw new Error("owner or admin access is required to manage model roles");
}

async function checked(response: Response, fallback: string): Promise<Record<string, unknown>> {
  const result = await response.json() as Record<string, unknown> & {ok?: boolean; error?: string};
  if (!response.ok || !result.ok) throw new Error(String(result.error || fallback));
  return result;
}

function asRegistry(value: unknown): ModelRoleRegistry {
  const source = value && typeof value === "object" ? value as Record<string, unknown> : {};
  const roles = Array.isArray(source.roles) ? source.roles.filter((item): item is ModelRole => Boolean(item) && typeof item === "object" && typeof (item as ModelRole).id === "string") : [];
  const models = Array.isArray(source.models) ? source.models.filter((item): item is ModelDescriptor => Boolean(item) && typeof item === "object" && typeof (item as ModelDescriptor).id === "string") : [];
  return {...source, roles, models};
}

export async function loadModelRoles(request: ApiRequester): Promise<ModelRoleRegistry> {
  const response = await request("/api/model-roles", {cache: "no-store"});
  const result = await checked(response, "Model roles are unavailable");
  return asRegistry(result.registry);
}

export async function saveModelRole(request: ApiRequester, accessLevel: AccessLevel, role: ModelRole): Promise<ModelRoleRegistry> {
  assertManager(accessLevel);
  const response = await request("/api/model-roles/save", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({role}),
  });
  const result = await checked(response, "Model role could not be saved");
  return asRegistry(result.payload);
}
