import assert from "node:assert/strict";
import test from "node:test";

import {loadMissionPanel, runMissionAction, startMission} from "../src/missions";


test("the Missions panel loads missions and templates from existing role endpoints", async () => {
  const calls: string[] = [];
  const request = async (path: string) => {
    calls.push(path);
    return {
      ok: true,
      json: async () => path.endsWith("mission-templates")
        ? {ok: true, templates: [{templateId: "delivery-execution", title: "Delivery Execution"}]}
        : {ok: true, missions: [{missionId: "mission-1", state: "paused", objective: "Ship the slice"}]},
    } as Response;
  };

  const panel = await loadMissionPanel(request, "publisher");

  assert.equal(panel.templates[0].templateId, "delivery-execution");
  assert.equal(panel.missions[0].state, "paused");
  assert.deepEqual(calls, ["/api/publisher/missions", "/api/publisher/mission-templates"]);
});

test("mission start and lifecycle controls use existing endpoints", async () => {
  const calls: Array<{path: string; method: string; body: unknown}> = [];
  const request = async (path: string, init?: RequestInit) => {
    calls.push({path, method: init?.method || "GET", body: JSON.parse(String(init?.body || "{}"))});
    return {ok: true, json: async () => ({ok: true, missionId: "mission-2"})} as Response;
  };

  await startMission(request, {
    templateId: "delivery-execution",
    objective: "Ship the slice",
    userIds: ["avery-stone"],
    domainCapabilityKeys: ["DC13"],
    delivery: {repositoryPath: "/workspace"},
  });
  await runMissionAction(request, "mission-2", "pause");

  assert.deepEqual(calls, [
    {
      path: "/api/missions/start",
      method: "POST",
      body: {
        templateId: "delivery-execution",
        objective: "Ship the slice",
        userIds: ["avery-stone"],
        domainCapabilityKeys: ["DC13"],
        delivery: {repositoryPath: "/workspace"},
      },
    },
    {path: "/api/missions/mission-2/pause", method: "POST", body: {}},
  ]);
});
