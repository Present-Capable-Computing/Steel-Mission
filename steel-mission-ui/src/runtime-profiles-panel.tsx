import {useEffect, useState} from "preact/hooks";

import type {AccessLevel} from "./capabilities";
import type {ApiRequester} from "./organizations";
import {
  cloneRuntimeProfile,
  loadRuntimeProfiles,
  runtimeProfileManagementAvailable,
  saveRuntimeProfile,
  validateRuntimeProfile,
  type RuntimeProfile,
  type RuntimeProfilesPayload,
} from "./runtime-profiles";


const EMPTY: RuntimeProfilesPayload = {activeProfile: "", registry: {profiles: []}, modelRoles: {roles: []}};

function textList(value: string): string[] {
  return [...new Set(value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean))];
}

function template(source?: RuntimeProfile): RuntimeProfile {
  return {
    ...(source || {
      status: "active",
      modelRole: "dc13.coordination-report",
      modelProvider: "auto",
      requiredProviderCapabilities: [],
      snapshotProfile: "worker-local-default",
      defaultFor: [],
      editableBy: ["owner", "admin"],
      visibilityRoleKeys: ["DC13"],
      includeCollections: ["missions"],
      limits: {},
      taskSelector: {mode: "latest"},
      sources: {},
    }),
    id: "",
    label: "",
  };
}

export function RuntimeProfilesPanel({accessLevel, request}: {accessLevel: AccessLevel; request: ApiRequester}) {
  const [payload, setPayload] = useState(EMPTY);
  const [selectedId, setSelectedId] = useState("");
  const [profile, setProfile] = useState<RuntimeProfile>(template());
  const [profileJson, setProfileJson] = useState(JSON.stringify(template(), null, 2));
  const [cloneId, setCloneId] = useState("");
  const [cloneLabel, setCloneLabel] = useState("");
  const [status, setStatus] = useState("Loading runtime profiles…");

  const select = (next: RuntimeProfile) => {
    setSelectedId(next.id);
    setProfile(next);
    setProfileJson(JSON.stringify(next, null, 2));
    setCloneId(`${next.id}.copy`);
    setCloneLabel(`${String(next.label || next.id)} Copy`);
    setStatus("");
  };
  const reload = async (preferredId?: string) => {
    const loaded = await loadRuntimeProfiles(request);
    setPayload(loaded);
    const next = loaded.registry.profiles.find((item) => item.id === preferredId) || loaded.registry.profiles[0] || template();
    select(next);
  };

  useEffect(() => {
    let current = true;
    loadRuntimeProfiles(request)
      .then((loaded) => {
        if (!current) return;
        setPayload(loaded);
        const next = loaded.registry.profiles[0] || template();
        select(next);
      })
      .catch((error: unknown) => current && setStatus(error instanceof Error ? error.message : "Runtime profiles are unavailable"));
    return () => { current = false; };
  }, [request]);

  const update = (key: string, value: unknown) => {
    const next = {...profile, [key]: value} as RuntimeProfile;
    setProfile(next);
    setProfileJson(JSON.stringify(next, null, 2));
  };
  const parsedProfile = (): RuntimeProfile => {
    const value = JSON.parse(profileJson) as unknown;
    if (!value || typeof value !== "object" || Array.isArray(value) || typeof (value as RuntimeProfile).id !== "string") {
      throw new Error("Runtime Profile JSON must be an object with an id");
    }
    return value as RuntimeProfile;
  };
  const act = async (kind: "validate" | "save") => {
    setStatus(`${kind === "save" ? "Saving" : "Validating"} profile…`);
    try {
      const current = parsedProfile();
      if (kind === "validate") await validateRuntimeProfile(request, accessLevel, current);
      else await saveRuntimeProfile(request, accessLevel, current);
      if (kind === "save") await reload(current.id);
      setStatus(kind === "save" ? "Profile saved and reloaded." : "Profile is valid.");
    } catch (error: unknown) {
      setStatus(error instanceof Error ? error.message : "Runtime profile request failed");
    }
  };
  const clone = async () => {
    setStatus("Cloning profile…");
    try {
      await cloneRuntimeProfile(request, accessLevel, profile.id, cloneId.trim(), cloneLabel.trim());
      await reload(cloneId.trim());
      setStatus("Profile cloned and reloaded.");
    } catch (error: unknown) {
      setStatus(error instanceof Error ? error.message : "Profile could not be cloned");
    }
  };
  const canManage = runtimeProfileManagementAvailable(accessLevel);
  const roles = Array.isArray(payload.modelRoles.roles) ? payload.modelRoles.roles : [];

  return (
    <div id="runtimeProfilesPanel" class="parity-panel">
      <p>Active Coordinator model: <strong>{payload.activeProfile || "not selected"}</strong></p>
      <div class="panel-actions">
        <label>Runtime Profile
          <select value={selectedId} onChange={(event) => {
            const next = payload.registry.profiles.find((item) => item.id === event.currentTarget.value); if (next) select(next);
          }}>{payload.registry.profiles.map((item) => <option key={item.id} value={item.id}>{String(item.label || item.id)}</option>)}</select>
        </label>
        {canManage && <button type="button" onClick={() => select(template(payload.registry.profiles[0]))}>New Runtime Profile</button>}
      </div>

      <div class="panel-form">
        <div class="panel-grid">
          <label>Profile ID<input value={profile.id} readOnly={Boolean(selectedId)} onInput={(event) => update("id", event.currentTarget.value)} /></label>
          <label>Label<input value={String(profile.label || "")} onInput={(event) => update("label", event.currentTarget.value)} /></label>
          <label>Status<select value={String(profile.status || "active")} onChange={(event) => update("status", event.currentTarget.value)}><option value="active">active</option><option value="disabled">disabled</option></select></label>
          <label>Coordinator Role<select value={String(profile.modelRole || "")} onChange={(event) => update("modelRole", event.currentTarget.value)}>{roles.map((role) => <option key={String(role.id)} value={String(role.id)}>{String(role.title || role.id)}</option>)}</select></label>
          <label>Model Binding<select value={String(profile.modelProvider || "auto")} onChange={(event) => update("modelProvider", event.currentTarget.value)}><option value="auto">auto</option><option value="claude">claude</option><option value="codex">codex</option><option value="glimmer">glimmer</option></select></label>
          <label>Snapshot policy<input value={String(profile.snapshotProfile || "")} onInput={(event) => update("snapshotProfile", event.currentTarget.value)} /></label>
          <label>Required Provider-Native Capabilities<input value={Array.isArray(profile.requiredProviderCapabilities) ? profile.requiredProviderCapabilities.join(", ") : ""} onInput={(event) => update("requiredProviderCapabilities", textList(event.currentTarget.value))} /></label>
          <label>Default For<input value={Array.isArray(profile.defaultFor) ? profile.defaultFor.join(", ") : ""} onInput={(event) => update("defaultFor", textList(event.currentTarget.value))} /></label>
          <label>Editable By<input value={Array.isArray(profile.editableBy) ? profile.editableBy.join(", ") : ""} onInput={(event) => update("editableBy", textList(event.currentTarget.value))} /></label>
          <label>Visible Domain Capabilities<input value={Array.isArray(profile.visibilityRoleKeys) ? profile.visibilityRoleKeys.join(", ") : ""} onInput={(event) => update("visibilityRoleKeys", textList(event.currentTarget.value))} /></label>
          <label>Collections<input value={Array.isArray(profile.includeCollections) ? profile.includeCollections.join(", ") : ""} onInput={(event) => update("includeCollections", textList(event.currentTarget.value))} /></label>
        </div>
        <label>Runtime Profile JSON<textarea rows={18} value={profileJson} readOnly={!canManage} onInput={(event) => {
          setProfileJson(event.currentTarget.value);
          try { const next = JSON.parse(event.currentTarget.value) as RuntimeProfile; if (next && typeof next === "object") setProfile(next); } catch { /* validate on action */ }
        }} /></label>
        {canManage && <div class="panel-actions">
          <button type="button" onClick={() => act("validate")}>Validate Profile</button>
          <button type="button" onClick={() => act("save")}>Save Profile</button>
        </div>}
        {canManage && selectedId && <div class="panel-grid">
          <label>Clone Profile ID<input value={cloneId} onInput={(event) => setCloneId(event.currentTarget.value)} /></label>
          <label>Clone Label<input value={cloneLabel} onInput={(event) => setCloneLabel(event.currentTarget.value)} /></label>
          <button type="button" disabled={!cloneId.trim()} onClick={clone}>Clone Profile</button>
        </div>}
        {status && <p role="status">{status}</p>}
      </div>
    </div>
  );
}
