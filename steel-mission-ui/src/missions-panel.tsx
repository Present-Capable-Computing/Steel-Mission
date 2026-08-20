import {useEffect, useState} from "preact/hooks";

import type {AccessLevel} from "./capabilities";
import {
  loadMissionPanel,
  runMissionAction,
  startMission,
  type MissionPanelData,
  type MissionStartRequest,
} from "./missions";
import type {ApiRequester} from "./organizations";


const EMPTY_PANEL: MissionPanelData = {missions: [], templates: []};
const DEFAULT_DELIVERY = {
  repositoryPath: "",
  worktreeMode: "isolated",
  baseBranch: "main",
  deliveryBranch: "",
  worktreePath: "",
  branch: "",
  prProvider: "github",
  githubRepository: "",
  prMode: "readiness",
  ciProvider: "github-actions",
  ciRequired: false,
  ciWait: false,
  ciCommand: "",
  deployProvider: "none",
  deployEnvironment: "",
  deployUrl: "",
  modifyCommand: "",
  buildCommand: "",
  testCommand: "",
  inspectCommand: "",
  repairCommand: "",
  prCommand: "",
  prTarget: "",
  prTitle: "",
  prBody: "",
  deployCommand: "",
  deployTarget: "",
  deployHealthCommand: "",
  rollbackCommand: "",
  repairBudget: 2,
};

function list(value: string): string[] {
  return [...new Set(value.split(",").map((item) => item.trim()).filter(Boolean))];
}

export function MissionsPanel({accessLevel, request}: {accessLevel: AccessLevel; request: ApiRequester}) {
  const [panel, setPanel] = useState<MissionPanelData>(EMPTY_PANEL);
  const [templateId, setTemplateId] = useState("");
  const [objective, setObjective] = useState("");
  const [userIds, setUserIds] = useState("");
  const [capabilityKeys, setCapabilityKeys] = useState("");
  const [delivery, setDelivery] = useState<Record<string, unknown>>(DEFAULT_DELIVERY);
  const [deliveryJson, setDeliveryJson] = useState(JSON.stringify(DEFAULT_DELIVERY, null, 2));
  const [status, setStatus] = useState("Loading missions…");

  const reload = async () => {
    const loaded = await loadMissionPanel(request, accessLevel);
    setPanel(loaded);
    setTemplateId((current) => current || loaded.templates[0]?.templateId || "");
    setStatus("");
  };

  useEffect(() => {
    let current = true;
    loadMissionPanel(request, accessLevel)
      .then((loaded) => {
        if (!current) return;
        setPanel(loaded);
        setTemplateId(loaded.templates[0]?.templateId || "");
        setStatus("");
      })
      .catch((error: unknown) => current && setStatus(error instanceof Error ? error.message : "Missions are unavailable"));
    return () => { current = false; };
  }, [accessLevel, request]);

  const parseDelivery = (value: string) => {
    setDeliveryJson(value);
    try {
      const parsed = JSON.parse(value) as unknown;
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) setDelivery(parsed as Record<string, unknown>);
    } catch { /* retain the last valid payload until submit reports the error */ }
  };
  const submit = async (event: Event) => {
    event.preventDefault();
    setStatus("Starting mission…");
    try {
      const parsed = JSON.parse(deliveryJson) as unknown;
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("Delivery Options must be a JSON object");
      const payload: MissionStartRequest = {
        templateId,
        objective: objective.trim(),
        userIds: list(userIds),
        domainCapabilityKeys: list(capabilityKeys),
        delivery: parsed as Record<string, unknown>,
      };
      await startMission(request, payload);
      setObjective("");
      await reload();
      setStatus("Mission started.");
    } catch (error: unknown) {
      setStatus(error instanceof Error ? error.message : "Mission could not be started");
    }
  };
  const action = async (missionId: string, nextAction: "approve" | "pause" | "resume") => {
    setStatus(`${nextAction} in progress…`);
    try {
      await runMissionAction(request, missionId, nextAction);
      await reload();
      setStatus(`Mission ${nextAction} completed.`);
    } catch (error: unknown) {
      setStatus(error instanceof Error ? error.message : "Mission action failed");
    }
  };

  return (
    <div id="missionsPanel" class="parity-panel">
      <form class="panel-form" onSubmit={submit}>
        <div class="panel-grid">
          <label>Mission Template
            <select value={templateId} onChange={(event) => setTemplateId(event.currentTarget.value)}>
              {panel.templates.map((template) => <option key={template.templateId} value={template.templateId}>{template.title || template.templateId}</option>)}
            </select>
          </label>
          <label>Mission Users<input value={userIds} placeholder="user ids, comma separated" onInput={(event) => setUserIds(event.currentTarget.value)} /></label>
          <label>Capability Work Set<input value={capabilityKeys} placeholder="Domain Capability keys" onInput={(event) => setCapabilityKeys(event.currentTarget.value)} /></label>
        </div>
        <label>Mission Objective<textarea rows={4} value={objective} onInput={(event) => setObjective(event.currentTarget.value)} /></label>
        <h3>Delivery Scope</h3>
        <div class="panel-grid">
          <label>Repository Path<input value={String(delivery.repositoryPath || "")} onInput={(event) => {
            const next = {...delivery, repositoryPath: event.currentTarget.value}; setDelivery(next); setDeliveryJson(JSON.stringify(next, null, 2));
          }} /></label>
          <label>Worktree Mode
            <select value={String(delivery.worktreeMode || "isolated")} onChange={(event) => {
              const next = {...delivery, worktreeMode: event.currentTarget.value}; setDelivery(next); setDeliveryJson(JSON.stringify(next, null, 2));
            }}><option value="isolated">Isolated delivery worktree</option><option value="in-place">Use bound repository</option></select>
          </label>
          <label>Base Branch<input value={String(delivery.baseBranch || "")} onInput={(event) => {
            const next = {...delivery, baseBranch: event.currentTarget.value}; setDelivery(next); setDeliveryJson(JSON.stringify(next, null, 2));
          }} /></label>
          <label>Delivery Branch<input value={String(delivery.deliveryBranch || "")} onInput={(event) => {
            const next = {...delivery, deliveryBranch: event.currentTarget.value}; setDelivery(next); setDeliveryJson(JSON.stringify(next, null, 2));
          }} /></label>
        </div>
        <label>Delivery Options JSON<textarea rows={12} value={deliveryJson} onInput={(event) => parseDelivery(event.currentTarget.value)} /></label>
        <button type="submit" disabled={!templateId || !objective.trim()}>Start Mission</button>
      </form>

      <section aria-label="Mission templates">
        <h3>Mission Templates</h3>
        <div class="record-list">
          {panel.templates.map((template) => (
            <article key={template.templateId}><strong>{template.title || template.templateId}</strong><p>{template.description || template.templateId}</p></article>
          ))}
        </div>
      </section>

      <section aria-label="Active Missions">
        <h3>Active Missions</h3>
        <div class="record-list">
          {panel.missions.length === 0 && <p>No missions yet.</p>}
          {panel.missions.slice(0, 8).map((mission) => (
            <article key={mission.missionId}>
              <strong>{mission.objective || mission.templateTitle || mission.missionId}</strong>
              <p>{mission.missionId} · {mission.state || "unknown"}{mission.profile ? ` · ${mission.profile}` : ""}</p>
              <div class="panel-actions">
                {mission.state === "waiting_for_approval" && <button type="button" onClick={() => action(mission.missionId, "approve")}>Approve</button>}
                {(mission.state === "running" || mission.state === "waiting_for_approval") && <button type="button" onClick={() => action(mission.missionId, "pause")}>Pause</button>}
                {mission.state === "paused" && <button type="button" onClick={() => action(mission.missionId, "resume")}>Resume</button>}
                <a href={`/mission/${encodeURIComponent(mission.missionId)}?role=${accessLevel}`}>Open mission detail</a>
              </div>
            </article>
          ))}
        </div>
      </section>
      {status && <p role="status">{status}</p>}
    </div>
  );
}
