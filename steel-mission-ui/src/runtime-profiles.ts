import type {AccessLevel} from "./capabilities";
import type {ApiRequester} from "./organizations";

export type RuntimeProfile = Record<string, unknown> & {id: string; label?: string};

export interface RuntimeProfileRegistry {
  profiles: RuntimeProfile[];
  [key: string]: unknown;
}

export interface RuntimeProfilesPayload {
  activeProfile: string;
  registry: RuntimeProfileRegistry;
  modelRoles: {roles?: Array<Record<string, unknown>>; [key: string]: unknown};
}

export function runtimeProfileManagementAvailable(accessLevel: unknown): accessLevel is "owner" | "admin" {
  return accessLevel === "owner" || accessLevel === "admin";
}

function assertManager(accessLevel: AccessLevel): asserts accessLevel is "owner" | "admin" {
  if (!runtimeProfileManagementAvailable(accessLevel)) throw new Error("owner or admin access is required to manage runtime profiles");
}

async function checked(response: Response, fallback: string): Promise<Record<string, unknown>> {
  const result = await response.json() as Record<string, unknown> & {ok?: boolean; error?: string};
  if (!response.ok || !result.ok) throw new Error(String(result.error || fallback));
  return result;
}

export async function loadRuntimeProfiles(request: ApiRequester): Promise<RuntimeProfilesPayload> {
  const response = await request("/api/runtime-profiles", {cache: "no-store"});
  const result = await checked(response, "Runtime profiles are unavailable");
  const registry = result.registry && typeof result.registry === "object" ? result.registry as RuntimeProfileRegistry : {profiles: []};
  if (!Array.isArray(registry.profiles)) throw new Error("Runtime profile registry is unavailable");
  return {
    activeProfile: String(result.activeProfile || ""),
    registry,
    modelRoles: result.modelRoles && typeof result.modelRoles === "object" ? result.modelRoles as RuntimeProfilesPayload["modelRoles"] : {roles: []},
  };
}

async function managerPost(
  request: ApiRequester,
  accessLevel: AccessLevel,
  path: string,
  body: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  assertManager(accessLevel);
  const response = await request(path, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  return checked(response, "Runtime profile request failed");
}

export function validateRuntimeProfile(request: ApiRequester, accessLevel: AccessLevel, profile: RuntimeProfile) {
  return managerPost(request, accessLevel, "/api/runtime-profiles/validate", {profile});
}

export function saveRuntimeProfile(request: ApiRequester, accessLevel: AccessLevel, profile: RuntimeProfile) {
  return managerPost(request, accessLevel, "/api/runtime-profiles/save", {profile});
}

export function cloneRuntimeProfile(
  request: ApiRequester,
  accessLevel: AccessLevel,
  sourceId: string,
  newId: string,
  label: string,
) {
  return managerPost(request, accessLevel, "/api/runtime-profiles/clone", {sourceId, newId, label});
}
