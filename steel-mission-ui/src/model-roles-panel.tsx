import {useEffect, useState} from "preact/hooks";

import type {AccessLevel} from "./capabilities";
import {
  loadModelRoles,
  modelRoleManagementAvailable,
  saveModelRole,
  type ModelRole,
  type ModelRoleRegistry,
} from "./model-roles";
import type {ApiRequester} from "./organizations";


const EMPTY: ModelRoleRegistry = {roles: [], models: []};

function list(value: string): string[] {
  return [...new Set(value.split(",").map((item) => item.trim()).filter(Boolean))];
}

export function ModelRolesPanel({accessLevel, request}: {accessLevel: AccessLevel; request: ApiRequester}) {
  const [registry, setRegistry] = useState(EMPTY);
  const [selectedId, setSelectedId] = useState("");
  const [role, setRole] = useState<ModelRole>({id: ""});
  const [roleJson, setRoleJson] = useState("{}");
  const [status, setStatus] = useState("Loading model roles…");

  const select = (next: ModelRole) => {
    setSelectedId(next.id);
    setRole(next);
    setRoleJson(JSON.stringify(next, null, 2));
    setStatus("");
  };
  const reload = async (preferredId?: string) => {
    const loaded = await loadModelRoles(request);
    setRegistry(loaded);
    select(loaded.roles.find((item) => item.id === preferredId) || loaded.roles[0] || {id: ""});
  };

  useEffect(() => {
    let current = true;
    loadModelRoles(request)
      .then((loaded) => {
        if (!current) return;
        setRegistry(loaded);
        select(loaded.roles[0] || {id: ""});
      })
      .catch((error: unknown) => current && setStatus(error instanceof Error ? error.message : "Model roles are unavailable"));
    return () => { current = false; };
  }, [request]);

  const update = (key: string, value: unknown) => {
    const next = {...role, [key]: value} as ModelRole;
    setRole(next);
    setRoleJson(JSON.stringify(next, null, 2));
  };
  const save = async () => {
    setStatus("Saving binding…");
    try {
      const parsed = JSON.parse(roleJson) as unknown;
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed) || typeof (parsed as ModelRole).id !== "string") throw new Error("Model Role JSON must be an object with an id");
      const current = parsed as ModelRole;
      await saveModelRole(request, accessLevel, current);
      await reload(current.id);
      setStatus("Delivery Coordinator model binding saved and reloaded.");
    } catch (error: unknown) {
      setStatus(error instanceof Error ? error.message : "Binding could not be saved");
    }
  };
  const canManage = modelRoleManagementAvailable(accessLevel);
  const modelIds = registry.models.map((model) => model.id);

  return (
    <div id="modelRolesPanel" class="parity-panel">
      <h3>Delivery Coordinator Model Binding</h3>
      <p>{canManage ? "Owners and admins can change which model instance a coordinator role binds to first." : "Existing model bindings are read-only for this access level."}</p>

      <section>
        <h3>Models and Native capabilities</h3>
        <div class="record-list">
          {registry.models.map((model) => <article key={model.id}><strong>{model.id} · {String(model.provider || "provider")}</strong><p>Native capabilities: {Array.isArray(model.nativeCapabilities) ? model.nativeCapabilities.join(", ") : "not declared"}</p></article>)}
        </div>
      </section>

      <div class="panel-form">
        <label>Model Role
          <select value={selectedId} onChange={(event) => {
            const next = registry.roles.find((item) => item.id === event.currentTarget.value); if (next) select(next);
          }}>{registry.roles.map((item) => <option key={item.id} value={item.id}>{String(item.title || item.id)}</option>)}</select>
        </label>
        <div class="panel-grid">
          <label>Role ID<input value={role.id} readOnly /></label>
          <label>Title<input value={String(role.title || "")} readOnly={!canManage} onInput={(event) => update("title", event.currentTarget.value)} /></label>
          <label>Primary Model<select value={String(role.primaryModel || "")} disabled={!canManage} onChange={(event) => update("primaryModel", event.currentTarget.value)}>{modelIds.map((id) => <option key={id} value={id}>{id}</option>)}</select></label>
          <label>Snapshot policy<input value={String(role.snapshotProfile || "")} readOnly={!canManage} onInput={(event) => update("snapshotProfile", event.currentTarget.value)} /></label>
          <label>Fallback Models<input value={Array.isArray(role.fallbackModels) ? role.fallbackModels.join(", ") : ""} readOnly={!canManage} onInput={(event) => update("fallbackModels", list(event.currentTarget.value).filter((id) => id !== role.primaryModel))} /></label>
          <label>Governance Capabilities<input value={Array.isArray(role.governanceCapabilities) ? role.governanceCapabilities.join(", ") : ""} readOnly={!canManage} onInput={(event) => update("governanceCapabilities", list(event.currentTarget.value))} /></label>
        </div>
        <label>Model Role JSON<textarea rows={12} value={roleJson} readOnly={!canManage} onInput={(event) => {
          setRoleJson(event.currentTarget.value);
          try { const next = JSON.parse(event.currentTarget.value) as ModelRole; if (next && typeof next === "object") setRole(next); } catch { /* validate on save */ }
        }} /></label>
        {canManage && <button type="button" onClick={save}>Save Binding</button>}
        {status && <p role="status">{status}</p>}
      </div>
    </div>
  );
}
