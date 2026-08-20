import {useEffect, useState} from "preact/hooks";

import type {AccessLevel} from "./capabilities";
import type {ApiRequester} from "./organizations";
import {
  loadUserRegistry,
  saveUserRegistry,
  updateUser,
  userPanelAvailable,
  type ManagedUser,
  type UserRegistry,
} from "./users";


function list(value: string): string[] {
  return [...new Set(value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean))];
}

function userId(value: string): string {
  return value.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function template(): ManagedUser {
  return {
    id: "",
    name: "",
    email: "",
    role: "user",
    status: "active",
    assignedCapabilities: [],
    organizationIds: [],
    identitySubjects: [],
    externalIdentities: {github: [], slack: [], jira: []},
  };
}

export function UsersPanel({accessLevel, request}: {accessLevel: AccessLevel; request: ApiRequester}) {
  const [registry, setRegistry] = useState<UserRegistry | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [draft, setDraft] = useState<ManagedUser>(template());
  const [status, setStatus] = useState("Loading users…");

  useEffect(() => {
    let current = true;
    if (!userPanelAvailable(accessLevel)) {
      setStatus("Owner or admin access is required to manage users.");
      return () => { current = false; };
    }
    loadUserRegistry(request, accessLevel)
      .then((loaded) => {
        if (!current) return;
        const initial = loaded.users[0] || template();
        setRegistry(loaded);
        setSelectedId(initial.id);
        setDraft(initial);
        setStatus("");
      })
      .catch((error: unknown) => current && setStatus(error instanceof Error ? error.message : "Users are unavailable"));
    return () => { current = false; };
  }, [accessLevel, request]);

  if (!userPanelAvailable(accessLevel)) return <div id="usersPanel"><p>{status}</p></div>;

  const choose = (id: string) => {
    const selected = registry?.users.find((item) => item.id === id) || template();
    setSelectedId(id);
    setDraft(selected);
    setStatus("");
  };
  const setField = (key: keyof ManagedUser, value: unknown) => setDraft((current) => ({...current, [key]: value}));
  const setExternal = (key: string, value: string) => setDraft((current) => ({
    ...current,
    externalIdentities: {...current.externalIdentities, [key]: list(value)},
  }));
  const save = async (event: Event) => {
    event.preventDefault();
    if (!registry) return;
    setStatus("Saving user…");
    try {
      const id = userId(draft.id || draft.email || draft.name);
      const next = updateUser(registry, {...draft, id, name: draft.name.trim() || id});
      const saved = await saveUserRegistry(request, accessLevel, next);
      const selected = saved.users.find((item) => item.id === id) || draft;
      setRegistry(saved);
      setSelectedId(id);
      setDraft(selected);
      setStatus("User saved and reloaded.");
    } catch (error: unknown) {
      setStatus(error instanceof Error ? error.message : "User could not be saved");
    }
  };
  const external = draft.externalIdentities || {};

  return (
    <div id="usersPanel" class="parity-panel">
      <div class="panel-actions">
        <label>User
          <select value={selectedId} onChange={(event) => choose(event.currentTarget.value)}>
            {registry?.users.map((user) => <option key={user.id} value={user.id}>{user.name || user.id} · {user.role}</option>)}
          </select>
        </label>
        <button type="button" onClick={() => { setSelectedId(""); setDraft(template()); }}>Create User</button>
      </div>
      <form class="panel-form" onSubmit={save}>
        <div class="panel-grid">
          <label>User ID<input value={draft.id} readOnly={Boolean(selectedId)} onInput={(event) => setField("id", event.currentTarget.value)} /></label>
          <label>Display Name<input value={draft.name} onInput={(event) => setField("name", event.currentTarget.value)} /></label>
          <label>Email<input type="email" value={draft.email || ""} onInput={(event) => setField("email", event.currentTarget.value)} /></label>
          <label>Access Level
            <select value={draft.role} onChange={(event) => setField("role", event.currentTarget.value as AccessLevel)}>
              {(["owner", "admin", "publisher", "user"] as AccessLevel[]).map((role) => <option key={role} value={role}>{role}</option>)}
            </select>
          </label>
          <label>Account Status
            <select value={draft.status} onChange={(event) => setField("status", event.currentTarget.value as "active" | "disabled")}>
              <option value="active">active</option>
              <option value="disabled">disabled</option>
            </select>
          </label>
          <label>Organization IDs<input value={(draft.organizationIds || []).join(", ")} onInput={(event) => setField("organizationIds", list(event.currentTarget.value))} /></label>
          <label>Assigned Capability Keys<input value={draft.assignedCapabilities.join(", ")} onInput={(event) => setField("assignedCapabilities", list(event.currentTarget.value))} /></label>
          <label>GitHub Identities<input value={(external.github || []).join(", ")} onInput={(event) => setExternal("github", event.currentTarget.value)} /></label>
          <label>Slack Identities<input value={(external.slack || []).join(", ")} onInput={(event) => setExternal("slack", event.currentTarget.value)} /></label>
          <label>Jira Identities<input value={(external.jira || []).join(", ")} onInput={(event) => setExternal("jira", event.currentTarget.value)} /></label>
        </div>
        <label>Identity Subjects<textarea rows={3} value={(draft.identitySubjects || []).join("\n")} onInput={(event) => setField("identitySubjects", list(event.currentTarget.value))} /></label>
        <button type="submit" disabled={!registry || !(draft.id || draft.email || draft.name).trim()}>Save User</button>
        {status && <p role="status">{status}</p>}
      </form>
    </div>
  );
}
