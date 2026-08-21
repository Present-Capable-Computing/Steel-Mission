import assert from "node:assert/strict";
import test from "node:test";

import {
  answerDecision,
  chatAnswerText,
  pollChatJob,
  sendFollowUp,
  startChat,
} from "../src/chat";


test("chat start and polling consume the existing pipeline endpoints", async () => {
  const calls: Array<{path: string; init?: RequestInit}> = [];
  const request = async (path: string, init?: RequestInit) => {
    calls.push({path, init});
    if (path === "/api/chat") {
      return {ok: true, json: async () => ({ok: true, jobId: "JOB1", state: "running"})} as Response;
    }
    return {ok: true, json: async () => ({ok: true, jobId: "JOB1", state: "done", payload: {summary: "The answer."}})} as Response;
  };

  const started = await startChat(request, {
    question: "What needs my attention?",
    messages: [{role: "user", content: "Earlier context"}],
    workMode: "normal",
    profile: "dc13.local",
  });
  const completed = await pollChatJob(request, started.jobId);

  assert.equal(started.state, "running");
  assert.equal(completed.state, "done");
  assert.equal(calls[0].path, "/api/chat");
  assert.equal(calls[0].init?.method, "POST");
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), {
    question: "What needs my attention?",
    messages: [{role: "user", content: "Earlier context"}],
    workMode: "normal",
    profile: "dc13.local",
  });
  assert.equal(calls[1].path, "/api/chat/JOB1");
  assert.equal(chatAnswerText(completed), "The answer.");
});


test("follow-ups and decisions use the active chat job", async () => {
  const calls: Array<{path: string; body: unknown}> = [];
  const request = async (path: string, init?: RequestInit) => {
    calls.push({path, body: JSON.parse(String(init?.body || "{}"))});
    return {ok: true, json: async () => ({ok: true, jobId: "JOB1", state: "running", progress: {phase: "Continuing"}})} as Response;
  };

  await sendFollowUp(request, "JOB1", "Focus on blockers.");
  await answerDecision(request, "JOB1", "continue", "Use the safe default.");

  assert.deepEqual(calls, [
    {path: "/api/chat/JOB1/follow-up", body: {content: "Focus on blockers."}},
    {path: "/api/chat/JOB1/decision", body: {optionId: "continue", freeText: "Use the safe default."}},
  ]);
});
