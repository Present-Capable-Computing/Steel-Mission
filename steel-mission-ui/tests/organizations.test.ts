import assert from "node:assert/strict";
import test from "node:test";

import {
  loadOrganizationRegistry,
  organizationPanelAvailable,
  saveOrganizationRegistry,
  updateOrganization,
} from "../src/organizations";


const registry = {
  schemaVersion: 1,
  activeOrganizationId: "northstar-forge",
  organizations: [
    {id: "northstar-forge", name: "Northstar Forge", slug: "northstar-forge"},
  ],
};

test("organization management is available to owners and admins", () => {
  assert.equal(organizationPanelAvailable("owner"), true);
  assert.equal(organizationPanelAvailable("admin"), true);
  assert.equal(organizationPanelAvailable("publisher"), false);
  assert.equal(organizationPanelAvailable("user"), false);
});

test("the Organizations panel loads and saves through its existing role endpoint", async () => {
  const calls: Array<{path: string; method: string; body?: unknown}> = [];
  const request = async (path: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({path, method: init?.method || "GET", body});
    return {
      ok: true,
      json: async () => ({ok: true, payload: body || registry}),
    } as Response;
  };

  const loaded = await loadOrganizationRegistry(request, "admin");
  const edited = updateOrganization(loaded, {
    ...loaded.organizations[0],
    name: "Northstar Forge AG",
  });
  const saved = await saveOrganizationRegistry(request, "admin", edited);

  assert.equal(saved.organizations[0].name, "Northstar Forge AG");
  assert.deepEqual(calls, [
    {path: "/api/admin/organizations", method: "GET", body: undefined},
    {path: "/api/admin/organizations", method: "POST", body: edited},
  ]);
});

test("a non-manager cannot reach organization management requests", async () => {
  let requested = false;
  await assert.rejects(
    loadOrganizationRegistry(async () => {
      requested = true;
      return {} as Response;
    }, "user"),
    /owner or admin access is required/,
  );
  assert.equal(requested, false);
});
