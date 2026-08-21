import assert from "node:assert/strict";
import test from "node:test";

import {loadCoordinatorModels} from "../src/coordinator-models";


test("the picker offers one explicit profile per resolvable coordinator provider", async () => {
  const calls: string[] = [];
  const request = async (path: string) => {
    calls.push(path);
    if (path === "/api/runtime-profiles") {
      return {ok: true, json: async () => ({
        ok: true,
        activeProfile: "dc13.local",
        registry: {profiles: [
          {id: "dc13.auto", label: "Automatic", status: "active", modelRole: "dc13.coordination-report", modelProvider: "auto"},
          {id: "dc13.local", label: "Local", status: "active", modelRole: "dc13.coordination-report", modelProvider: "glimmer"},
          {id: "dc13.claude", label: "Claude", status: "active", modelRole: "dc13.coordination-report", modelProvider: "claude"},
          {id: "dc13.codex", label: "Codex", status: "active", modelRole: "dc13.coordination-report", modelProvider: "codex"},
          {id: "disabled", label: "Disabled", status: "disabled", modelRole: "dc13.coordination-report", modelProvider: "claude"},
        ]},
      })} as Response;
    }
    const profile = new URL(`http://local${path}`).searchParams.get("profile");
    if (profile === "dc13.codex") {
      return {ok: false, status: 400, json: async () => ({ok: false, error: "provider cannot route this role"})} as Response;
    }
    const provider = profile === "dc13.local" ? "glimmer" : "claude";
    const model = provider === "glimmer" ? "qwen2.5-coder:14b" : "claude-sonnet-5";
    return {ok: true, json: async () => ({
      ok: true,
      payload: {
        runtimeProfile: {id: profile, modelProvider: profile === "dc13.auto" ? "auto" : provider},
        modelPolicy: {provider, selectedModel: model},
      },
    })} as Response;
  };

  const result = await loadCoordinatorModels(request);

  assert.equal(result.selectedProfileId, "dc13.local");
  assert.deepEqual(result.models.map((model) => [model.profileId, model.provider]), [
    ["dc13.local", "glimmer"],
    ["dc13.claude", "claude"],
  ]);
  assert(calls.includes("/api/runtime-profiles/resolve?profile=dc13.codex"));
  assert(!calls.some((path) => path.includes("disabled")));
});
