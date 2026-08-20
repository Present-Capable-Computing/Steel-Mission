import assert from "node:assert/strict";
import test from "node:test";

import {loadModelRoles, saveModelRole} from "../src/model-roles";


const role = {id: "dc13.coordination-report", title: "Delivery Coordinator", primaryModel: "claude-sonnet-5", fallbackModels: []};

test("the Model Roles panel loads the existing registry", async () => {
  const request = async (path: string) => ({
    ok: true,
    json: async () => ({ok: true, registry: {roles: [role], models: [{id: "claude-sonnet-5", provider: "claude"}]}, path}),
  }) as Response;

  const registry = await loadModelRoles(request);

  assert.equal(registry.roles[0].id, "dc13.coordination-report");
  assert.equal(registry.models[0].provider, "claude");
});

test("model-role saves use the existing manager endpoint", async () => {
  const calls: Array<{path: string; body: unknown}> = [];
  const request = async (path: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body || "{}"));
    calls.push({path, body});
    return {ok: true, json: async () => ({ok: true, payload: {roles: [role], models: []}})} as Response;
  };

  await saveModelRole(request, "owner", role);

  assert.deepEqual(calls, [{path: "/api/model-roles/save", body: {role}}]);
});

test("a non-manager is refused before a model-role write", async () => {
  let requested = false;
  await assert.rejects(saveModelRole(async () => {
    requested = true;
    return {} as Response;
  }, "publisher", role), /owner or admin access is required/);
  assert.equal(requested, false);
});
