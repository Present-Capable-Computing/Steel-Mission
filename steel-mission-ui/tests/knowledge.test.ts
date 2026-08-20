import assert from "node:assert/strict";
import test from "node:test";

import {
  loadKnowledgeRegistry,
  previewPreparedSnapshot,
  saveKnowledgeSources,
  uploadKnowledge,
} from "../src/knowledge";


test("the Knowledge panel reads the existing role-scoped registry", async () => {
  const calls: string[] = [];
  const request = async (path: string) => {
    calls.push(path);
    return {ok: true, json: async () => ({ok: true, roles: [{roleKey: "DC13"}], generalKnowledge: {repositories: [], documents: []}})} as Response;
  };

  const registry = await loadKnowledgeRegistry(request, "publisher");

  assert.equal(registry.roles?.[0].roleKey, "DC13");
  assert.deepEqual(calls, ["/api/publisher/knowledge"]);
});

test("knowledge source saves, snapshot previews, and uploads use existing manager endpoints", async () => {
  const calls: Array<{path: string; method: string; body?: unknown}> = [];
  const request = async (path: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({path, method: init?.method || "GET", body});
    return {ok: true, json: async () => ({ok: true, payload: body || {sourceCount: 2}})} as Response;
  };
  const sources = {repositories: [{name: "product", path: "/repo"}], documents: []};
  const upload = {label: "policy", sourceKind: "files", files: [{name: "policy.md", contentBase64: "IyBQb2xpY3k="}]};

  await saveKnowledgeSources(request, "admin", sources);
  await previewPreparedSnapshot(request, "admin");
  await uploadKnowledge(request, "admin", upload);

  assert.deepEqual(calls, [
    {path: "/api/admin/knowledge", method: "POST", body: sources},
    {path: "/api/admin/knowledge/prepared", method: "GET", body: undefined},
    {path: "/api/admin/knowledge/upload", method: "POST", body: upload},
  ]);
});

test("non-managers are refused before a knowledge write request", async () => {
  let requested = false;
  await assert.rejects(
    saveKnowledgeSources(async () => {
      requested = true;
      return {} as Response;
    }, "user", {repositories: [], documents: []}),
    /owner or admin access is required/,
  );
  assert.equal(requested, false);
});
