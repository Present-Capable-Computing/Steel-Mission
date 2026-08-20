import {render} from "preact";
import {useEffect, useState} from "preact/hooks";

import "./styles.css";
import {
  assignmentControlsAvailable,
  capabilityOwnership,
  saveCapabilityAssignment,
  type AssignmentCapability,
  type AssignmentUser,
} from "./assignments";
import {
  CAPABILITY_EMPTY_STATE,
  capabilityWorkspaceView,
  type CapabilityWorkspaceView,
} from "./capabilities";
import {SETTINGS_SECTIONS, type SettingsSectionId} from "./settings";
import {OrganizationsPanel} from "./organizations-panel";
import {UsersPanel} from "./users-panel";
import {WORK_MODES, type WorkMode} from "./work-mode";


function browserCookie(name: string): string {
  return document.cookie.split(";").map((item) => item.trim()).find((item) => item.startsWith(`${name}=`))?.slice(name.length + 1) || "";
}

function apiRequest(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  const localRole = localStorage.getItem("steel-mission-operator-role");
  const localActor = localStorage.getItem("steel-mission-actor-user");
  if (localRole) headers.set("X-Present-Role", localRole);
  if (localActor) headers.set("X-Present-Actor", localActor);
  const method = String(init.method || "GET").toUpperCase();
  const csrf = browserCookie("present_csrf");
  if (csrf && ["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
    headers.set("X-Present-CSRF", decodeURIComponent(csrf));
  }
  return fetch(path, {...init, headers, credentials: "same-origin"});
}

function App() {
  const [workMode, setWorkMode] = useState<WorkMode>("normal");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [activeSettingsSection, setActiveSettingsSection] = useState<SettingsSectionId>("organizations");
  const [capabilityState, setCapabilityState] = useState<
    {status: "loading" | "ready" | "error"; view?: CapabilityWorkspaceView; error?: string}
  >({status: "loading"});
  const [assignmentUsers, setAssignmentUsers] = useState<AssignmentUser[]>([]);
  const [assignmentCapabilities, setAssignmentCapabilities] = useState<AssignmentCapability[]>([]);
  const [selectedAssignmentUser, setSelectedAssignmentUser] = useState("");
  const [selectedAssignmentCapabilities, setSelectedAssignmentCapabilities] = useState<string[]>([]);
  const [assignmentStatus, setAssignmentStatus] = useState("");

  useEffect(() => {
    let current = true;
    Promise.all([
      apiRequest("/api/auth/whoami", {cache: "no-store"}),
      apiRequest("/api/vocabulary", {cache: "no-store"}),
    ])
      .then(async ([identityResponse, vocabularyResponse]) => {
        const identity = await identityResponse.json() as {ok?: boolean; actor?: unknown; error?: string};
        const vocabulary = await vocabularyResponse.json() as {
          ok?: boolean;
          error?: string;
          capabilities?: unknown[];
        };
        if (!identityResponse.ok || !identity.ok) throw new Error(identity.error || "Identity is unavailable");
        if (!vocabularyResponse.ok || !vocabulary.ok) throw new Error(vocabulary.error || "Vocabulary is unavailable");
        const view = capabilityWorkspaceView(identity.actor, vocabulary);
        if (current) {
          setCapabilityState({status: "ready", view});
          const registeredCapabilities = Array.isArray(vocabulary.capabilities)
            ? vocabulary.capabilities.filter((item): item is AssignmentCapability => (
              Boolean(item) && typeof item === "object" && typeof (item as AssignmentCapability).capabilityKey === "string"
            ))
            : [];
          setAssignmentCapabilities(registeredCapabilities);
          if (assignmentControlsAvailable(view.accessLevel)) {
            const usersResponse = await apiRequest("/api/owner/users", {cache: "no-store"});
            const usersPayload = await usersResponse.json() as {ok?: boolean; users?: AssignmentUser[]; error?: string};
            if (!usersResponse.ok || !usersPayload.ok) throw new Error(usersPayload.error || "Users are unavailable");
            const assignableUsers = (usersPayload.users || []).filter((user) => user.role === "publisher" || user.role === "user");
            setAssignmentUsers(assignableUsers);
            if (assignableUsers[0]) {
              setSelectedAssignmentUser(assignableUsers[0].id);
              setSelectedAssignmentCapabilities([...assignableUsers[0].assignedCapabilities]);
            }
          }
        }
      })
      .catch((error: unknown) => {
        if (current) {
          setCapabilityState({status: "error", error: error instanceof Error ? error.message : "Capability workspace is unavailable"});
        }
      });
    return () => {
      current = false;
    };
  }, []);

  const chooseAssignmentUser = (userId: string) => {
    setSelectedAssignmentUser(userId);
    const user = assignmentUsers.find((item) => item.id === userId);
    setSelectedAssignmentCapabilities([...(user?.assignedCapabilities || [])]);
    setAssignmentStatus("");
  };

  const toggleAssignmentCapability = (capabilityKey: string) => {
    setSelectedAssignmentCapabilities((currentCapabilities) => (
      currentCapabilities.includes(capabilityKey)
        ? currentCapabilities.filter((key) => key !== capabilityKey)
        : [...currentCapabilities, capabilityKey]
    ));
  };

  const saveAssignments = async (event: Event) => {
    event.preventDefault();
    const accessLevel = capabilityState.view?.accessLevel || "user";
    setAssignmentStatus("Saving…");
    try {
      await saveCapabilityAssignment(
        apiRequest,
        assignmentUsers,
        assignmentCapabilities,
        selectedAssignmentUser,
        selectedAssignmentCapabilities,
        accessLevel,
      );
      setAssignmentUsers((users) => users.map((user) => (
        user.id === selectedAssignmentUser
          ? {...user, assignedCapabilities: [...selectedAssignmentCapabilities]}
          : user
      )));
      setAssignmentStatus("Capability assignment saved and reloaded.");
    } catch (error: unknown) {
      setAssignmentStatus(error instanceof Error ? error.message : "Capability assignment failed");
    }
  };

  return (
    <main class="app-shell">
      <aside class="rail" aria-label="Steel Mission">
        <div class="mark" aria-hidden="true">SM</div>
        <div>
          <p class="eyebrow">Agent delivery plane</p>
          <strong>Steel Mission</strong>
        </div>
        <dl>
          <div><dt>Server</dt><dd>Not connected</dd></div>
          <div><dt>Authority</dt><dd>Advisory</dd></div>
        </dl>
      </aside>

      <section class="workspace">
        <header class="toolbar">
          <div>
            <label id="coordinatorModelLabel" for="coordinatorModel">Coordinator model</label>
            <select id="coordinatorModel" aria-describedby="coordinatorModelDescription" disabled>
              <option>Delivery Coordinator</option>
            </select>
            <p id="coordinatorModelDescription">Selects the model configuration that executes the Delivery Coordinator.</p>
          </div>

          <div class="mode-control">
            <span id="workModeLabel">Work mode</span>
            <div
              class="mode-switch"
              role="group"
              aria-labelledby="workModeLabel"
              aria-describedby="normalModeDescription domainCapabilityModeDescription"
            >
              {WORK_MODES.map((mode) => (
                <button
                  key={mode.id}
                  type="button"
                  aria-pressed={workMode === mode.id}
                  aria-describedby={mode.id === "normal" ? "normalModeDescription" : "domainCapabilityModeDescription"}
                  onClick={() => setWorkMode(mode.id)}
                >
                  {mode.label}
                </button>
              ))}
            </div>
            <p id="normalModeDescription" class="visually-hidden">{WORK_MODES[0].description}</p>
            <p id="domainCapabilityModeDescription" class="visually-hidden">{WORK_MODES[1].description}</p>
          </div>

          <button
            id="openSettings"
            class="settings-trigger"
            type="button"
            aria-controls="settingsPanel"
            aria-expanded={settingsOpen}
            onClick={() => setSettingsOpen((open) => !open)}
          >
            {settingsOpen ? "Close settings" : "Settings"}
          </button>
        </header>

        <p
          id="domainCapabilityDefinition"
          class="definition"
          data-vocabulary-term="domain-capability"
        >
          Domain Capability: An assignable organizational role and workflow lens backed by governed knowledge.
        </p>

        <section id="capabilityWorkspace" class="capability-workspace" aria-labelledby="capabilityWorkspaceTitle">
          <div>
            <p class="eyebrow">Capability workspace</p>
            <h2 id="capabilityWorkspaceTitle">Your Domain Capabilities</h2>
            {capabilityState.view && (
              <p class="workspace-identity">{capabilityState.view.actorId} · {capabilityState.view.accessLevel}</p>
            )}
          </div>
          {capabilityState.status === "loading" && <p>Loading assigned capabilities…</p>}
          {capabilityState.status === "error" && <p role="alert">{capabilityState.error}</p>}
          {capabilityState.status === "ready" && capabilityState.view?.capabilities.length === 0 && (
            <p id="capabilityEmptyState" class="capability-empty">{CAPABILITY_EMPTY_STATE}</p>
          )}
          {capabilityState.status === "ready" && Boolean(capabilityState.view?.capabilities.length) && (
            <div class="capability-grid">
              {capabilityState.view?.capabilities.map((capability) => (
                <article key={capability.capabilityKey} class="capability-card">
                  <strong>{capability.label}</strong>
                  <p>Assigned to your workspace</p>
                </article>
              ))}
            </div>
          )}
        </section>

        {settingsOpen && (
          <section id="settingsPanel" class="settings-panel" aria-label="Settings">
            <nav class="settings-nav" aria-label="Settings sections">
              {SETTINGS_SECTIONS.map((section) => (
                <button
                  id={`settingsNav-${section.id}`}
                  key={section.id}
                  type="button"
                  data-settings-target={section.id}
                  aria-controls={`settingsSection-${section.id}`}
                  aria-current={activeSettingsSection === section.id ? "page" : undefined}
                  onClick={() => setActiveSettingsSection(section.id)}
                >
                  <strong>{section.label}</strong>
                  <span>{section.hint}</span>
                </button>
              ))}
            </nav>
            <div class="settings-content">
              {SETTINGS_SECTIONS.map((section) => (
                <section
                  id={`settingsSection-${section.id}`}
                  key={section.id}
                  class="settings-section"
                  hidden={activeSettingsSection !== section.id}
                  aria-labelledby={`settingsNav-${section.id}`}
                >
                  <p class="eyebrow">Settings</p>
                  <h2>{section.label}</h2>
                  <p>{section.hint}</p>
                  {section.id === "organizations" && activeSettingsSection === "organizations" && capabilityState.view && (
                    <OrganizationsPanel accessLevel={capabilityState.view.accessLevel} request={apiRequest} />
                  )}
                  {section.id === "people" && activeSettingsSection === "people" && capabilityState.view && (
                    <UsersPanel accessLevel={capabilityState.view.accessLevel} request={apiRequest} />
                  )}
                  {section.id === "people" && assignmentControlsAvailable(capabilityState.view?.accessLevel) && (
                    <form id="capabilityAssignmentForm" class="assignment-form" onSubmit={saveAssignments}>
                      <label>
                        User
                        <select value={selectedAssignmentUser} onChange={(event) => chooseAssignmentUser(event.currentTarget.value)}>
                          {assignmentUsers.map((user) => (
                            <option key={user.id} value={user.id}>{user.name || user.id} · {user.role}</option>
                          ))}
                        </select>
                      </label>
                      <fieldset>
                        <legend>Domain Capabilities</legend>
                        <div class="assignment-grid">
                          {assignmentCapabilities.map((capability) => {
                            const ownership = capabilityOwnership(capability, assignmentUsers);
                            return (
                              <label
                                key={capability.capabilityKey}
                                class="assignment-choice"
                                data-capability-ownership={ownership.status}
                              >
                                <span>
                                  <input
                                    type="checkbox"
                                    checked={selectedAssignmentCapabilities.includes(capability.capabilityKey)}
                                    onChange={() => toggleAssignmentCapability(capability.capabilityKey)}
                                  />
                                  {capability.capabilityKey} · {capability.displayName}
                                </span>
                                <strong>{ownership.label}</strong>
                                <small>{ownership.action}</small>
                              </label>
                            );
                          })}
                        </div>
                      </fieldset>
                      <button type="submit" disabled={!selectedAssignmentUser}>Save capability assignment</button>
                      {assignmentStatus && <p role="status">{assignmentStatus}</p>}
                    </form>
                  )}
                </section>
              ))}
            </div>
          </section>
        )}

        <section class="empty-state" aria-labelledby="newConsoleTitle" hidden={settingsOpen}>
          <p class="eyebrow">Rebuilt console</p>
          <h1 id="newConsoleTitle">A typed shell, ready for parity work.</h1>
          <p>
            This non-default application proves the committed single-file build. Existing work,
            settings, and mission behavior remains on the current console until each surface reaches parity.
          </p>
        </section>
      </section>
    </main>
  );
}

const root = document.querySelector<HTMLElement>("#app");
if (!root) throw new Error("Steel Mission UI root is missing");
root.replaceChildren();
render(<App />, root);
