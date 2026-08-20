import type {AccessLevel} from "./capabilities";
import type {ApiRequester} from "./organizations";

export interface ManagedUser {
  id: string;
  name: string;
  email?: string;
  role: AccessLevel;
  status: "active" | "disabled";
  assignedCapabilities: string[];
  organizationIds?: string[];
  identitySubjects?: string[];
  externalIdentities?: Record<string, string[]>;
  [key: string]: unknown;
}

export interface UserRegistry {
  schemaVersion?: number;
  users: ManagedUser[];
  [key: string]: unknown;
}

export function userPanelAvailable(accessLevel: unknown): accessLevel is "owner" | "admin" {
  return accessLevel === "owner" || accessLevel === "admin";
}

function assertManager(accessLevel: AccessLevel): asserts accessLevel is "owner" | "admin" {
  if (!userPanelAvailable(accessLevel)) throw new Error("owner or admin access is required to manage users");
}

function asRegistry(value: unknown): UserRegistry {
  const source = value && typeof value === "object" ? value as Record<string, unknown> : {};
  if (!Array.isArray(source.users)) throw new Error("User registry is unavailable");
  return {
    ...source,
    users: source.users.filter((item): item is ManagedUser => (
      Boolean(item) && typeof item === "object" && typeof (item as ManagedUser).id === "string"
    )),
  };
}

export async function loadUserRegistry(request: ApiRequester, accessLevel: AccessLevel): Promise<UserRegistry> {
  assertManager(accessLevel);
  const response = await request(`/api/${accessLevel}/users`, {cache: "no-store"});
  const result = await response.json() as Record<string, unknown> & {ok?: boolean; error?: string};
  if (!response.ok || !result.ok) throw new Error(result.error || "Users are unavailable");
  return asRegistry(result);
}

export async function saveUserRegistry(
  request: ApiRequester,
  accessLevel: AccessLevel,
  registry: UserRegistry,
): Promise<UserRegistry> {
  assertManager(accessLevel);
  const response = await request(`/api/${accessLevel}/users`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(registry),
  });
  const result = await response.json() as {ok?: boolean; payload?: unknown; error?: string};
  if (!response.ok || !result.ok) throw new Error(result.error || "User could not be saved");
  return asRegistry(result.payload);
}

export function updateUser(registry: UserRegistry, user: ManagedUser): UserRegistry {
  const users = [...registry.users];
  const index = users.findIndex((item) => item.id === user.id);
  if (index >= 0) users[index] = user;
  else users.push(user);
  return {...registry, users};
}
