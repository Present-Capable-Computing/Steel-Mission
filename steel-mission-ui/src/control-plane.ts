import type {AccessLevel} from "./capabilities";
import type {ApiRequester} from "./organizations";

export type ControlPlaneDocument = "control-policy" | "auth-policy" | "integrations";

export interface ControlPlaneData {
  integrations: Record<string, unknown>;
  controlPolicy: Record<string, unknown>;
  authPolicy: Record<string, unknown>;
  readiness: Record<string, unknown>;
}

export function controlPlaneManagementAvailable(accessLevel: unknown): accessLevel is "owner" | "admin" {
  return accessLevel === "owner" || accessLevel === "admin";
}

function assertManager(accessLevel: AccessLevel): asserts accessLevel is "owner" | "admin" {
  if (!controlPlaneManagementAvailable(accessLevel)) throw new Error("owner or admin access is required to manage the Control Plane");
}

async function checked(response: Response, fallback: string): Promise<Record<string, unknown>> {
  const result = await response.json() as Record<string, unknown> & {ok?: boolean; error?: string};
  if (!response.ok || !result.ok) throw new Error(String(result.error || fallback));
  return result;
}

export async function loadControlPlane(request: ApiRequester, accessLevel: AccessLevel): Promise<ControlPlaneData> {
  const [integrationsResponse, controlResponse, authResponse, readinessResponse] = await Promise.all([
    request(`/api/${accessLevel}/integrations`, {cache: "no-store"}),
    request(`/api/${accessLevel}/control-policy`, {cache: "no-store"}),
    request(`/api/${accessLevel}/auth-policy`, {cache: "no-store"}),
    request(`/api/control-plane/readiness?role=${encodeURIComponent(accessLevel)}`, {cache: "no-store"}),
  ]);
  const integrations = await checked(integrationsResponse, "Integrations are unavailable");
  const control = await checked(controlResponse, "Control policy is unavailable");
  const auth = await checked(authResponse, "Auth policy is unavailable");
  const readiness = await checked(readinessResponse, "Control Plane readiness is unavailable");
  return {
    integrations,
    controlPolicy: control.policy && typeof control.policy === "object" ? control.policy as Record<string, unknown> : {},
    authPolicy: auth.policy && typeof auth.policy === "object" ? auth.policy as Record<string, unknown> : {},
    readiness,
  };
}

export async function saveControlPlaneDocument(
  request: ApiRequester,
  accessLevel: AccessLevel,
  document: ControlPlaneDocument,
  value: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  assertManager(accessLevel);
  const wrapper = document === "integrations" ? {registry: value} : {policy: value};
  const response = await request(`/api/${accessLevel}/${document}`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(wrapper),
  });
  const result = await checked(response, `${document} could not be saved`);
  return result.payload && typeof result.payload === "object" ? result.payload as Record<string, unknown> : {};
}
