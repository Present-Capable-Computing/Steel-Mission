import type {AccessLevel} from "./capabilities";

export interface Organization {
  id: string;
  name: string;
  slug: string;
  identifiers?: Record<string, unknown>;
  knowledgeDomainKeys?: string[];
  domainCapabilityKeys?: string[];
  knowledgeSources?: {
    repositories?: unknown[];
    documents?: unknown[];
  };
  notes?: string;
  [key: string]: unknown;
}

export interface OrganizationRegistry {
  schemaVersion?: number;
  activeOrganizationId: string;
  organizations: Organization[];
  [key: string]: unknown;
}

export type ApiRequester = (path: string, init?: RequestInit) => Promise<Response>;

export function organizationPanelAvailable(accessLevel: unknown): accessLevel is "owner" | "admin" {
  return accessLevel === "owner" || accessLevel === "admin";
}

function assertManager(accessLevel: AccessLevel): asserts accessLevel is "owner" | "admin" {
  if (!organizationPanelAvailable(accessLevel)) {
    throw new Error("owner or admin access is required to manage organizations");
  }
}

function asRegistry(value: unknown): OrganizationRegistry {
  const source = value && typeof value === "object" ? value as Record<string, unknown> : {};
  if (!Array.isArray(source.organizations)) throw new Error("Organization registry is unavailable");
  return {
    ...source,
    activeOrganizationId: String(source.activeOrganizationId || ""),
    organizations: source.organizations.filter((item): item is Organization => (
      Boolean(item) && typeof item === "object" && typeof (item as Organization).id === "string"
    )),
  };
}

export async function loadOrganizationRegistry(
  request: ApiRequester,
  accessLevel: AccessLevel,
): Promise<OrganizationRegistry> {
  assertManager(accessLevel);
  const response = await request(`/api/${accessLevel}/organizations`, {cache: "no-store"});
  const result = await response.json() as {ok?: boolean; payload?: unknown; error?: string};
  if (!response.ok || !result.ok) throw new Error(result.error || "Organizations are unavailable");
  return asRegistry(result.payload);
}

export async function saveOrganizationRegistry(
  request: ApiRequester,
  accessLevel: AccessLevel,
  registry: OrganizationRegistry,
): Promise<OrganizationRegistry> {
  assertManager(accessLevel);
  const response = await request(`/api/${accessLevel}/organizations`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(registry),
  });
  const result = await response.json() as {ok?: boolean; payload?: unknown; error?: string};
  if (!response.ok || !result.ok) throw new Error(result.error || "Organization could not be saved");
  return asRegistry(result.payload);
}

export function updateOrganization(
  registry: OrganizationRegistry,
  organization: Organization,
): OrganizationRegistry {
  const organizations = [...registry.organizations];
  const index = organizations.findIndex((item) => item.id === organization.id);
  if (index >= 0) organizations[index] = organization;
  else organizations.push(organization);
  return {
    ...registry,
    activeOrganizationId: registry.activeOrganizationId || organization.id,
    organizations,
  };
}
