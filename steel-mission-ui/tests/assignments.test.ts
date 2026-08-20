import assert from "node:assert/strict";
import test from "node:test";

import {
  assignmentControlsAvailable,
  assignmentRegistry,
  saveCapabilityAssignment,
} from "../src/assignments";


const capabilities = [
  {capabilityKey: "DC03", displayName: "Architecture"},
  {capabilityKey: "DC13", displayName: "Delivery Coordinator"},
];
const users = [
  {id: "publisher-1", role: "publisher", assignedCapabilities: ["DC13"]},
  {id: "user-1", role: "user", assignedCapabilities: ["DC03"]},
];

test("only an owner can reach capability assignment controls", () => {
  assert.equal(assignmentControlsAvailable("owner"), true);
  for (const accessLevel of ["admin", "publisher", "user"]) {
    assert.equal(assignmentControlsAvailable(accessLevel), false);
  }
});

test("the owner assignment model writes the capability registry shape", () => {
  const registry = assignmentRegistry(users, capabilities, "publisher-1", ["DC03", "DC13"]);
  const architecture = registry.assignments.find((item) => item.roleKey === "DC03");
  const coordinator = registry.assignments.find((item) => item.roleKey === "DC13");

  assert.deepEqual(architecture?.publishers, ["publisher-1"]);
  assert.deepEqual(architecture?.users, ["user-1"]);
  assert.deepEqual(coordinator?.publishers, ["publisher-1"]);
});

test("an owner save posts to the registry endpoint and reloads the round trip", async () => {
  const calls: Array<{path: string; method: string}> = [];
  const expected = assignmentRegistry(users, capabilities, "publisher-1", ["DC03", "DC13"]);
  const request = async (path: string, init?: RequestInit) => {
    calls.push({path, method: init?.method || "GET"});
    return {
      ok: true,
      json: async () => init?.method === "POST" ? {ok: true, payload: expected} : {ok: true, ...expected},
    } as Response;
  };

  const saved = await saveCapabilityAssignment(request, users, capabilities, "publisher-1", ["DC03", "DC13"]);

  assert.deepEqual(calls, [
    {path: "/api/owner/assignments", method: "POST"},
    {path: "/api/owner/assignments", method: "GET"},
  ]);
  assert.deepEqual(saved.assignments, expected.assignments);
});

test("a non-owner is refused before any request is made", async () => {
  let requested = false;
  await assert.rejects(
    saveCapabilityAssignment(async () => {
      requested = true;
      return {} as Response;
    }, users, capabilities, "user-1", ["DC13"], "user"),
    /owner access is required/,
  );
  assert.equal(requested, false);
});
