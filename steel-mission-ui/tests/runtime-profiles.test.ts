import assert from "node:assert/strict";
import test from "node:test";

import {
  cloneRuntimeProfile,
  loadRuntimeProfiles,
  saveRuntimeProfile,
  validateRuntimeProfile,
} from "../src/runtime-profiles";


const profile = {id: "dc13.auto", label: "Delivery Coordinator", status: "active", modelProvider: "auto"};

test("the Runtime Profiles panel loads the existing registry contract", async () => {
  const request = async (path: string) => ({
    ok: true,
    json: async () => ({ok: true, activeProfile: "dc13.auto", registry: {profiles: [profile]}, modelRoles: {roles: []}, path}),
  }) as Response;

  const loaded = await loadRuntimeProfiles(request);

  assert.equal(loaded.activeProfile, "dc13.auto");
  assert.equal(loaded.registry.profiles[0].id, "dc13.auto");
});

test("validate, save, and clone use the existing manager endpoints", async () => {
  const calls: Array<{path: string; body: unknown}> = [];
  const request = async (path: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body || "{}"));
    calls.push({path, body});
    return {ok: true, json: async () => ({ok: true, payload: {profiles: [profile]}})} as Response;
  };

  await validateRuntimeProfile(request, "owner", profile);
  await saveRuntimeProfile(request, "owner", profile);
  await cloneRuntimeProfile(request, "owner", "dc13.auto", "dc13.copy", "Copy");

  assert.deepEqual(calls, [
    {path: "/api/runtime-profiles/validate", body: {profile}},
    {path: "/api/runtime-profiles/save", body: {profile}},
    {path: "/api/runtime-profiles/clone", body: {sourceId: "dc13.auto", newId: "dc13.copy", label: "Copy"}},
  ]);
});

test("non-managers are refused before a runtime-profile write", async () => {
  let requested = false;
  await assert.rejects(validateRuntimeProfile(async () => {
    requested = true;
    return {} as Response;
  }, "publisher", profile), /owner or admin access is required/);
  assert.equal(requested, false);
});
