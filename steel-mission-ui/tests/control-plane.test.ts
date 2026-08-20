import assert from "node:assert/strict";
import test from "node:test";

import {loadControlPlane, saveControlPlaneDocument} from "../src/control-plane";


test("the Control Plane panel loads every existing read contract", async () => {
  const calls: string[] = [];
  const request = async (path: string) => {
    calls.push(path);
    if (path.includes("integrations")) return {ok: true, json: async () => ({ok: true, connectors: [], modelProviders: []})} as Response;
    if (path.includes("auth-policy")) return {ok: true, json: async () => ({ok: true, policy: {policyId: "auth"}})} as Response;
    if (path.includes("control-policy")) return {ok: true, json: async () => ({ok: true, policy: {policyId: "control"}})} as Response;
    return {ok: true, json: async () => ({ok: true, alphaScore: 100, productionScore: 80, checks: []})} as Response;
  };

  const loaded = await loadControlPlane(request, "publisher");

  assert.equal(loaded.controlPolicy.policyId, "control");
  assert.equal(loaded.authPolicy.policyId, "auth");
  assert.deepEqual(calls, [
    "/api/publisher/integrations",
    "/api/publisher/control-policy",
    "/api/publisher/auth-policy",
    "/api/control-plane/readiness?role=publisher",
  ]);
});

test("the three Control Plane saves use existing manager endpoints and wrapper shapes", async () => {
  const calls: Array<{path: string; body: unknown}> = [];
  const request = async (path: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body || "{}"));
    calls.push({path, body});
    return {ok: true, json: async () => ({ok: true, payload: body.policy || body.registry})} as Response;
  };

  await saveControlPlaneDocument(request, "admin", "control-policy", {policyId: "control"});
  await saveControlPlaneDocument(request, "admin", "auth-policy", {policyId: "auth"});
  await saveControlPlaneDocument(request, "admin", "integrations", {connectors: []});

  assert.deepEqual(calls, [
    {path: "/api/admin/control-policy", body: {policy: {policyId: "control"}}},
    {path: "/api/admin/auth-policy", body: {policy: {policyId: "auth"}}},
    {path: "/api/admin/integrations", body: {registry: {connectors: []}}},
  ]);
});

test("a non-manager is refused before a Control Plane write request", async () => {
  let requested = false;
  await assert.rejects(saveControlPlaneDocument(async () => {
    requested = true;
    return {} as Response;
  }, "user", "control-policy", {}), /owner or admin access is required/);
  assert.equal(requested, false);
});
