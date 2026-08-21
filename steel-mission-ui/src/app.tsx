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
import {MissionsPanel} from "./missions-panel";
import {KnowledgePanel} from "./knowledge-panel";
import {RuntimeProfilesPanel} from "./runtime-profiles-panel";
import {ControlPlanePanel} from "./control-plane-panel";
import {ModelRolesPanel} from "./model-roles-panel";
import {AuditPanel} from "./audit-panel";
import {WORK_MODES, type WorkMode} from "./work-mode";
import {
  loadConsoleStatus,
  unavailableConsoleStatus,
  type ConsoleStatus,
} from "./status";
import {ChatPanel} from "./chat-panel";
import {
  loadCoordinatorModels,
  type CoordinatorModel,
} from "./coordinator-models";
import {
  loadMissionProgress,
  MissionProgressPanel,
  unavailableMissionProgress,
  type MissionProgress,
} from "./mission-progress";


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
  const [consoleStatus, setConsoleStatus] = useState<ConsoleStatus | null>(null);
  const [coordinatorModels, setCoordinatorModels] = useState<CoordinatorModel[]>([]);
  const [coordinatorProfile, setCoordinatorProfile] = useState("");
  const [coordinatorModelError, setCoordinatorModelError] = useState("");
  const [missionProgress, setMissionProgress] = useState<MissionProgress | null>(null);

  useEffect(() => {
    let current = true;
    loadCoordinatorModels(apiRequest)
      .then((loaded) => {
        if (!current) return;
        setCoordinatorModels(loaded.models);
        setCoordinatorProfile(loaded.selectedProfileId);
        setCoordinatorModelError(loaded.models.length ? "" : "No coordinator model is currently routable.");
      })
      .catch((error: unknown) => {
        if (!current) return;
        setCoordinatorModelError(error instanceof Error ? error.message : "Coordinator models are unavailable.");
      });
    return () => { current = false; };
  }, []);

  useEffect(() => {
    let current = true;
    let refreshTimer: number | undefined;
    const refresh = () => {
      loadConsoleStatus(apiRequest)
        .then((status) => {
          if (current) setConsoleStatus(status);
        })
        .catch((error: unknown) => {
          if (current) setConsoleStatus(unavailableConsoleStatus(error));
        })
        .finally(() => {
          if (current) refreshTimer = window.setTimeout(refresh, 10000);
        });
    };
    refresh();
    return () => {
      current = false;
      if (refreshTimer !== undefined) window.clearTimeout(refreshTimer);
    };
  }, []);

  useEffect(() => {
    let current = true;
    let refreshTimer: number | undefined;
    const refresh = () => {
      loadMissionProgress(apiRequest)
        .then((progress) => {
          if (current) setMissionProgress(progress);
        })
        .catch((error: unknown) => {
          if (current) setMissionProgress(unavailableMissionProgress(error));
        })
        .finally(() => {
          if (current) refreshTimer = window.setTimeout(refresh, 5000);
        });
    };
    refresh();
    return () => {
      current = false;
      if (refreshTimer !== undefined) window.clearTimeout(refreshTimer);
    };
  }, []);

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
        const actor = identity.actor && typeof identity.actor === "object"
          ? identity.actor as {role?: unknown}
          : {};
        const actorRole = ["owner", "admin", "publisher"].includes(String(actor.role))
          ? String(actor.role)
          : "user";
        const workspaceResponse = await apiRequest(`/api/${actorRole}/workspace`, {cache: "no-store"});
        const workspace = await workspaceResponse.json() as {ok?: boolean; error?: string};
        if (!workspaceResponse.ok || !workspace.ok) throw new Error(workspace.error || "Capability workspace is unavailable");
        const view = capabilityWorkspaceView(identity.actor, workspace);
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
        <dl class="system-status" aria-label="Live system status" aria-live="polite">
          <div><dt>Server</dt><dd>{consoleStatus?.server || "—"}</dd></div>
          <div><dt>Authority</dt><dd>{consoleStatus?.authority || "—"}</dd></div>
        </dl>
        <section class="provider-status" aria-labelledby="providerStatusTitle" aria-live="polite">
          <p id="providerStatusTitle" class="eyebrow">Providers</p>
          {consoleStatus?.providers.map((provider) => (
            <article key={provider.id} data-provider={provider.id}>
              <strong>{provider.label}</strong>
              <p>
                <span data-connection={provider.connection}>{provider.connection}</span>
                {" · "}<span data-activity={provider.activity}>{provider.activity}</span>
                {provider.thinkingTokens !== undefined && ` · ${provider.thinkingTokens.toLocaleString()} thinking tokens`}
              </p>
            </article>
          ))}
          {consoleStatus?.error && <p role="status" class="status-error">{consoleStatus.error}</p>}
        </section>
      </aside>

      <section class="workspace">
        <header class="toolbar">
          {workMode === "normal" && <div>
            <label id="coordinatorModelLabel" for="coordinatorModel">Coordinator model</label>
            <select
              id="coordinatorModel"
              aria-describedby="coordinatorModelDescription"
              value={coordinatorProfile}
              onChange={(event) => setCoordinatorProfile(event.currentTarget.value)}
            >
              {coordinatorModels.length === 0 && <option value="">Loading registered models…</option>}
              {coordinatorModels.map((model) => (
                <option key={model.profileId} value={model.profileId}>{model.label}</option>
              ))}
            </select>
            <p id="coordinatorModelDescription">Selects the model configuration that executes the Delivery Coordinator.</p>
            {coordinatorModelError && <p role="status">{coordinatorModelError}</p>}
          </div>}

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
                  id={mode.id === "normal" ? "normalMode" : "domainCapabilityMode"}
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

        {!settingsOpen && <MissionProgressPanel progress={missionProgress} />}

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
                  {section.id === "missions" && activeSettingsSection === "missions" && capabilityState.view && (
                    <MissionsPanel accessLevel={capabilityState.view.accessLevel} request={apiRequest} />
                  )}
                  {section.id === "knowledge" && activeSettingsSection === "knowledge" && capabilityState.view && (
                    <KnowledgePanel accessLevel={capabilityState.view.accessLevel} request={apiRequest} />
                  )}
                  {section.id === "profiles" && activeSettingsSection === "profiles" && capabilityState.view && (
                    <RuntimeProfilesPanel accessLevel={capabilityState.view.accessLevel} request={apiRequest} />
                  )}
                  {section.id === "integrations" && activeSettingsSection === "integrations" && capabilityState.view && (
                    <ControlPlanePanel accessLevel={capabilityState.view.accessLevel} request={apiRequest} />
                  )}
                  {section.id === "models" && activeSettingsSection === "models" && capabilityState.view && (
                    <ModelRolesPanel accessLevel={capabilityState.view.accessLevel} request={apiRequest} />
                  )}
                  {section.id === "audit" && activeSettingsSection === "audit" && capabilityState.view && (
                    <AuditPanel accessLevel={capabilityState.view.accessLevel} request={apiRequest} />
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
                                data-capability-control
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

        <section
          class="work-surface"
          data-work-surface="normal-chat"
          hidden={settingsOpen || workMode !== "normal"}
        >
          <ChatPanel request={apiRequest} workMode="normal" profile={coordinatorProfile} />
        </section>
        {!settingsOpen && workMode === "domain-capabilities" && (
          <section class="work-surface" data-work-surface="domain-capabilities">
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
            <section id="deliverySetup" class="delivery-setup" aria-labelledby="deliverySetupTitle">
              <p class="eyebrow">Delivery setup</p>
              <h2 id="deliverySetupTitle">Prepare a mission</h2>
              {capabilityState.view
                ? <MissionsPanel accessLevel={capabilityState.view.accessLevel} request={apiRequest} />
                : <p>Loading delivery setup…</p>}
            </section>
          </section>
        )}
      </section>
    </main>
  );
}

const root = document.querySelector<HTMLElement>("#app");
if (!root) throw new Error("Steel Mission UI root is missing");
root.replaceChildren();
render(<App />, root);
