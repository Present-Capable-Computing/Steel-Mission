import assert from "node:assert/strict";
import test from "node:test";

import {loadMutationLedger} from "../src/audit";


test("the Audit panel loads the existing manager mutation ledger", async () => {
  const calls: string[] = [];
  const request = async (path: string) => {
    calls.push(path);
    return {
      ok: true,
      json: async () => ({ok: true, mutations: [{mutationId: "mu-1", action: "user-saved", beforeHash: "before", afterHash: "after", details: {users: 4}}]}),
    } as Response;
  };

  const mutations = await loadMutationLedger(request, "admin");

  assert.equal(mutations[0].mutationId, "mu-1");
  assert.deepEqual(calls, ["/api/admin/mutations"]);
});

test("a non-manager is refused before requesting the mutation ledger", async () => {
  let requested = false;
  await assert.rejects(loadMutationLedger(async () => {
    requested = true;
    return {} as Response;
  }, "publisher"), /owner or admin access is required/);
  assert.equal(requested, false);
});
