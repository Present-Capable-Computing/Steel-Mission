import assert from "node:assert/strict";
import test from "node:test";

import {
  loadUserRegistry,
  saveUserRegistry,
  updateUser,
  userPanelAvailable,
} from "../src/users";


const registry = {
  schemaVersion: 1,
  users: [
    {id: "avery-stone", name: "Avery Stone", email: "avery@example.test", role: "publisher", status: "active", assignedCapabilities: ["DC13"]},
  ],
};

test("user management is available to owners and admins", () => {
  assert.equal(userPanelAvailable("owner"), true);
  assert.equal(userPanelAvailable("admin"), true);
  assert.equal(userPanelAvailable("publisher"), false);
  assert.equal(userPanelAvailable("user"), false);
});

test("the Users panel loads and saves through its existing role endpoint", async () => {
  const calls: Array<{path: string; method: string; body?: unknown}> = [];
  const request = async (path: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({path, method: init?.method || "GET", body});
    return {
      ok: true,
      json: async () => init?.method === "POST" ? {ok: true, payload: body} : {ok: true, ...registry},
    } as Response;
  };

  const loaded = await loadUserRegistry(request, "owner");
  const edited = updateUser(loaded, {...loaded.users[0], status: "disabled"});
  const saved = await saveUserRegistry(request, "owner", edited);

  assert.equal(saved.users[0].status, "disabled");
  assert.deepEqual(calls, [
    {path: "/api/owner/users", method: "GET", body: undefined},
    {path: "/api/owner/users", method: "POST", body: edited},
  ]);
});

test("a non-manager cannot reach user management requests", async () => {
  let requested = false;
  await assert.rejects(
    loadUserRegistry(async () => {
      requested = true;
      return {} as Response;
    }, "publisher"),
    /owner or admin access is required/,
  );
  assert.equal(requested, false);
});
