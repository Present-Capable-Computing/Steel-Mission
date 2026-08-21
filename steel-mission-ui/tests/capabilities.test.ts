import assert from "node:assert/strict";
import test from "node:test";

import {CAPABILITY_EMPTY_STATE, capabilityWorkspaceView} from "../src/capabilities";


const vocabulary = {
  capabilities: [
    {capabilityKey: "DC03", displayName: "Architecture"},
    {capabilityKey: "DC04", displayName: "Product"},
    {capabilityKey: "DC13", displayName: "Delivery Coordinator"},
  ],
};

test("an owner sees the workspace grant even when raw assignments are empty", () => {
  const view = capabilityWorkspaceView(
    {actorId: "owner-1", role: "owner", capabilities: []},
    {visibleCapabilities: vocabulary.capabilities},
  );

  assert.equal(view.accessLevel, "owner");
  assert.deepEqual(view.capabilities.map((item) => item.label), [
    "DC03 · Architecture",
    "DC04 · Product",
    "DC13 · Delivery Coordinator",
  ]);
});

test("a publisher sees the capabilities assigned to that publisher", () => {
  const view = capabilityWorkspaceView(
    {actorId: "publisher-1", role: "publisher", capabilities: ["DC03", "DC13"]},
    {visibleCapabilities: [vocabulary.capabilities[0], vocabulary.capabilities[2]]},
  );

  assert.equal(view.accessLevel, "publisher");
  assert.deepEqual(view.capabilities.map((item) => item.label), [
    "DC03 · Architecture",
    "DC13 · Delivery Coordinator",
  ]);
});

test("a user sees the capabilities assigned to that user", () => {
  const view = capabilityWorkspaceView(
    {actorId: "user-1", role: "user", capabilities: ["DC04"]},
    {visibleCapabilities: [vocabulary.capabilities[1]]},
  );

  assert.equal(view.accessLevel, "user");
  assert.deepEqual(view.capabilities.map((item) => item.label), ["DC04 · Product"]);
});

test("an empty assignment explains who can assign a capability and where", () => {
  const view = capabilityWorkspaceView(
    {actorId: "user-2", role: "user", capabilities: []},
    {visibleCapabilities: []},
  );

  assert.deepEqual(view.capabilities, []);
  assert.match(CAPABILITY_EMPTY_STATE, /owner or admin/);
  assert.match(CAPABILITY_EMPTY_STATE, /Settings → Users/);
});
