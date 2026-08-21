import assert from "node:assert/strict";
import test from "node:test";

import {loadCoordinatorModels} from "../src/coordinator-models";


test("the normal-mode picker keeps the active auto profile when it resolves to Codex", async () => {
  const calls: string[] = [];
  const request = async (path: string) => {
    calls.push(path);
    if (path === "/api/runtime-profiles") {
      return {ok: true, json: async () => ({
        ok: true,
        activeProfile: "dc13.auto",
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
    const provider = profile === "dc13.local" ? "glimmer" : profile === "dc13.claude" ? "claude" : "codex";
    const model = provider === "glimmer" ? "qwen2.5-coder:14b" : provider === "claude" ? "claude-sonnet-5" : "codex-cli-default";
    return {ok: true, json: async () => ({
      ok: true,
      payload: {
        runtimeProfile: {id: profile, modelProvider: profile === "dc13.auto" ? "auto" : provider},
        modelPolicy: {provider, selectedModel: model},
      },
    })} as Response;
  };

  const result = await loadCoordinatorModels(request);

  assert.equal(result.selectedProfileId, "dc13.auto");
  assert.deepEqual(result.models.map((model) => [model.profileId, model.provider]), [
    ["dc13.auto", "codex"],
    ["dc13.claude", "claude"],
    ["dc13.local", "glimmer"],
  ]);
  assert(calls.includes("/api/runtime-profiles/resolve?profile=dc13.codex"));
  assert(!calls.some((path) => path.includes("disabled")));
});


test("an unavailable Codex profile leaves the registry-authored auto fallback visible", async () => {
  const request = async (path: string) => {
    if (path === "/api/runtime-profiles") {
      return {ok: true, json: async () => ({
        ok: true,
        activeProfile: "dc13.auto",
        registry: {profiles: [
          {id: "dc13.auto", label: "Automatic", status: "active", modelRole: "dc13.coordination-report", modelProvider: "auto"},
          {id: "dc13.codex", label: "Codex", status: "active", modelRole: "dc13.coordination-report", modelProvider: "codex"},
          {id: "dc13.claude", label: "Claude", status: "active", modelRole: "dc13.coordination-report", modelProvider: "claude"},
        ]},
      })} as Response;
    }
    const profile = new URL(`http://local${path}`).searchParams.get("profile");
    if (profile === "dc13.codex") {
      return {ok: false, status: 400, json: async () => ({ok: false, error: "Codex is unavailable"})} as Response;
    }
    return {ok: true, json: async () => ({
      ok: true,
      payload: {
        runtimeProfile: {id: profile, modelProvider: profile === "dc13.auto" ? "auto" : "claude"},
        modelPolicy: {
          provider: "claude",
          selectedModel: "claude-sonnet-5",
          ...(profile === "dc13.auto" ? {fallbackReason: "primary-unavailable"} : {}),
        },
      },
    })} as Response;
  };

  const result = await loadCoordinatorModels(request);

  assert.equal(result.selectedProfileId, "dc13.auto");
  assert.deepEqual(result.models.map((model) => [model.profileId, model.provider]), [
    ["dc13.auto", "claude"],
  ]);
});
