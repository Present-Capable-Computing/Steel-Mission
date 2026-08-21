import assert from "node:assert/strict";
import test from "node:test";

import {loadConsoleStatus} from "../src/status";


test("console status is derived from health and authority readiness", async () => {
  const calls: string[] = [];
  const request = async (path: string) => {
    calls.push(path);
    if (path === "/api/health") {
      return {
        ok: true,
        json: async () => ({
          ok: true,
          providers: [
            {id: "claude", label: "Claude", connection: "connected", activity: "working", jobCount: 1, tokenUsage: {thinkingTokens: 234}},
            {id: "codex", label: "Codex", connection: "connected", activity: "idle", jobCount: 0},
            {id: "glimmer", label: "Glimmer", connection: "online", activity: "idle", jobCount: 0},
          ],
        }),
      } as Response;
    }
    return {ok: true, json: async () => ({ok: true, meetsAlphaTarget: true, meetsProductionTarget: false})} as Response;
  };

  const status = await loadConsoleStatus(request);

  assert.deepEqual(calls, ["/api/health", "/api/control-plane/readiness"]);
  assert.equal(status.server, "Connected");
  assert.equal(status.authority, "Alpha ready");
  assert.equal(status.providers[0].activity, "working");
  assert.equal(status.providers[0].thinkingTokens, 234);
  assert.equal(status.providers[2].connection, "online");
});
