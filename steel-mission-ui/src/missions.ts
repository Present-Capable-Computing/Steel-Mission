import type {AccessLevel} from "./capabilities";
import type {ApiRequester} from "./organizations";

export interface MissionTemplate {
  templateId: string;
  title?: string;
  description?: string;
  nodes?: Array<{nodeId?: string; title?: string}>;
  [key: string]: unknown;
}

export interface Mission {
  missionId: string;
  state?: string;
  objective?: string;
  templateTitle?: string;
  profile?: string;
  [key: string]: unknown;
}

export interface MissionPanelData {
  missions: Mission[];
  templates: MissionTemplate[];
}

export interface MissionStartRequest {
  templateId: string;
  objective: string;
  profile?: string;
  userIds?: string[];
  domainCapabilityKeys?: string[];
  delivery?: Record<string, unknown>;
  mock?: boolean;
}

function records<T>(value: unknown, key: string): T[] {
  const source = value && typeof value === "object" ? value as Record<string, unknown> : {};
  return Array.isArray(source[key]) ? source[key] as T[] : [];
}

async function checkedJson(response: Response, fallback: string): Promise<Record<string, unknown>> {
  const result = await response.json() as Record<string, unknown> & {ok?: boolean; error?: string};
  if (!response.ok || !result.ok) throw new Error(String(result.error || fallback));
  return result;
}

export async function loadMissionPanel(request: ApiRequester, accessLevel: AccessLevel): Promise<MissionPanelData> {
  const [missionsResponse, templatesResponse] = await Promise.all([
    request(`/api/${accessLevel}/missions`, {cache: "no-store"}),
    request(`/api/${accessLevel}/mission-templates`, {cache: "no-store"}),
  ]);
  const missions = await checkedJson(missionsResponse, "Missions are unavailable");
  const templates = await checkedJson(templatesResponse, "Mission templates are unavailable");
  return {missions: records<Mission>(missions, "missions"), templates: records<MissionTemplate>(templates, "templates")};
}

export async function startMission(request: ApiRequester, payload: MissionStartRequest): Promise<Record<string, unknown>> {
  const response = await request("/api/missions/start", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload),
  });
  return checkedJson(response, "Mission could not be started");
}

export async function runMissionAction(
  request: ApiRequester,
  missionId: string,
  action: "approve" | "pause" | "resume",
): Promise<Record<string, unknown>> {
  const response = await request(`/api/missions/${encodeURIComponent(missionId)}/${action}`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(action === "approve" ? {note: "Approved from Mission Control."} : {}),
  });
  return checkedJson(response, "Mission action failed");
}
