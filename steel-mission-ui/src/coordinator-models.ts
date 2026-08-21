import type {ChatRequester} from "./chat";

export interface CoordinatorModel {
  profileId: string;
  profileLabel: string;
  provider: string;
  model: string;
  label: string;
}

export interface CoordinatorModels {
  models: CoordinatorModel[];
  selectedProfileId: string;
}

interface RuntimeProfile {
  id?: unknown;
  label?: unknown;
  status?: unknown;
  modelRole?: unknown;
  modelProvider?: unknown;
}

function title(value: string): string {
  return value ? value[0].toUpperCase() + value.slice(1) : "Provider";
}

async function resolveProfile(request: ChatRequester, profile: RuntimeProfile): Promise<CoordinatorModel | null> {
  const profileId = String(profile.id || "");
  const response = await request(`/api/runtime-profiles/resolve?profile=${encodeURIComponent(profileId)}`, {cache: "no-store"});
  const result = await response.json() as {ok?: boolean; payload?: unknown};
  if (!response.ok || !result.ok || !result.payload || typeof result.payload !== "object") return null;
  const payload = result.payload as Record<string, unknown>;
  const policy = payload.modelPolicy && typeof payload.modelPolicy === "object"
    ? payload.modelPolicy as Record<string, unknown>
    : {};
  const provider = typeof policy.provider === "string" ? policy.provider : "";
  const model = typeof policy.selectedModel === "string" ? policy.selectedModel : "";
  if (!provider || !model) return null;
  const profileLabel = typeof profile.label === "string" ? profile.label : profileId;
  return {
    profileId,
    profileLabel,
    provider,
    model,
    label: `${title(provider)} · ${model}`,
  };
}

export async function loadCoordinatorModels(request: ChatRequester): Promise<CoordinatorModels> {
  const response = await request("/api/runtime-profiles", {cache: "no-store"});
  const payload = await response.json() as {
    ok?: boolean;
    error?: string;
    activeProfile?: unknown;
    registry?: {profiles?: unknown};
  };
  if (!response.ok || !payload.ok) throw new Error(payload.error || "Coordinator models are unavailable");
  const profiles = Array.isArray(payload.registry?.profiles)
    ? payload.registry.profiles.filter((item): item is RuntimeProfile => (
      Boolean(item)
      && typeof item === "object"
      && (item as RuntimeProfile).status === "active"
      && (item as RuntimeProfile).modelRole === "dc13.coordination-report"
      && typeof (item as RuntimeProfile).id === "string"
    ))
    : [];
  const resolved = (await Promise.all(profiles.map((profile) => resolveProfile(request, profile))))
    .filter((model): model is CoordinatorModel => Boolean(model));
  const byProvider = new Map<string, {model: CoordinatorModel; explicit: boolean}>();
  for (const model of resolved) {
    const profile = profiles.find((item) => item.id === model.profileId);
    const explicit = profile?.modelProvider === model.provider;
    const current = byProvider.get(model.provider);
    if (!current || (explicit && !current.explicit)) byProvider.set(model.provider, {model, explicit});
  }
  const activeProfile = typeof payload.activeProfile === "string" ? payload.activeProfile : "";
  const models = [...byProvider.values()].map((item) => item.model).sort((left, right) => {
    if (left.profileId === activeProfile) return -1;
    if (right.profileId === activeProfile) return 1;
    return left.label.localeCompare(right.label);
  });
  return {
    models,
    selectedProfileId: models.some((model) => model.profileId === activeProfile)
      ? activeProfile
      : models[0]?.profileId || "",
  };
}
