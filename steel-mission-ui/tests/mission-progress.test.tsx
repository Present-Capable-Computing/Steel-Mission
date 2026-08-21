import assert from "node:assert/strict";
import test from "node:test";

import {
  MissionProgressPanel,
  loadMissionProgress,
  type AgentSessionStatus,
} from "../src/mission-progress";


function session(overrides: Partial<AgentSessionStatus>): AgentSessionStatus {
  return {
    sessionId: "as-0123456789abcdef01234567",
    issue: {
      repository: "Present-Capable-Computing/Steel-Mission",
      number: 184,
      url: "https://github.com/Present-Capable-Computing/Steel-Mission/issues/184",
    },
    stage: "plan",
    state: "working",
    worker: {id: "claude:planner", provider: "claude", model: "claude-opus-5", role: "planner"},
    machineAccount: "sm-agent-claude",
    budgetLimit: {elapsedSeconds: 1800, turns: 12},
    budgetSpent: {elapsedSeconds: 240, turns: 2},
    lastEvent: {summary: "Planner is checking the grant."},
    ...overrides,
  };
}

function visit(value: unknown, callback: (node: Record<string, unknown>) => void): void {
  if (Array.isArray(value)) {
    value.forEach((item) => visit(item, callback));
    return;
  }
  if (!value || typeof value !== "object") return;
  const node = value as Record<string, unknown>;
  if ("type" in node && "props" in node) callback(node);
  const props = node.props;
  if (props && typeof props === "object") visit((props as Record<string, unknown>).children, callback);
}

function renderedText(value: unknown): string {
  const chunks: string[] = [];
  const collect = (item: unknown): void => {
    if (Array.isArray(item)) {
      item.forEach(collect);
    } else if (typeof item === "string" || typeof item === "number") {
      chunks.push(String(item));
    } else if (item && typeof item === "object") {
      const props = (item as Record<string, unknown>).props;
      if (props && typeof props === "object") collect((props as Record<string, unknown>).children);
    }
  };
  collect(value);
  return chunks.join(" ");
}

test("two concurrent sessions and a rejected feed entry render in a read-only panel", async () => {
  const calls: string[] = [];
  const waiting = session({
    sessionId: "as-bbbbbbbbbbbbbbbbbbbbbbbb",
    issue: {
      repository: "Present-Capable-Computing/Steel-Mission",
      number: 186,
      url: "https://github.com/Present-Capable-Computing/Steel-Mission/issues/186",
    },
    stage: "review-loop",
    state: "waiting-on-person",
    worker: {id: "codex:reviewer", provider: "codex", model: "codex", role: "reviewer"},
    machineAccount: "sm-agent-codex",
    budgetSpent: {elapsedSeconds: 600, turns: 5},
    pendingDecision: {question: "Should the mission stay within the granted paths?"},
    lastEvent: {summary: "Review needs a scope decision."},
  });
  const progress = await loadMissionProgress(async (path) => {
    calls.push(path);
    return {
      ok: true,
      json: async () => ({
        ok: true,
        sessions: [session({}), waiting],
        errors: [{line: 3, message: "$.sessionId: does not match the canonical shape"}],
      }),
    } as Response;
  });

  const panel = MissionProgressPanel({progress});
  const text = renderedText(panel);
  const elements: string[] = [];
  const alerts: unknown[] = [];
  visit(panel, (node) => {
    if (typeof node.type === "string") elements.push(node.type);
    const props = node.props as Record<string, unknown>;
    if (props.role === "alert") alerts.push(node);
  });

  assert.deepEqual(calls, ["/api/agent-sessions"]);
  assert.match(text, /#\s*184/);
  assert.match(text, /#\s*186/);
  assert.match(text, /Plan/);
  assert.match(text, /Review loop/);
  assert.match(text, /Working/);
  assert.match(text, /Waiting on Person/);
  assert.match(text, /sm-agent-claude/);
  assert.match(text, /sm-agent-codex/);
  assert.match(text, /2\s*\/\s*12\s*turns/);
  assert.match(text, /600\s*\/\s*1800\s*seconds/);
  assert.match(text, /Should the mission stay within the granted paths\?/);
  assert.match(text, /Feed line 3 rejected/);
  assert.equal(alerts.length, 1);
  assert.deepEqual(
    elements.filter((tag) => ["button", "form", "input", "select", "textarea"].includes(tag)),
    [],
  );
});
