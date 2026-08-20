import assert from "node:assert/strict";
import test from "node:test";

import {isWorkMode, WORK_MODES} from "../src/work-mode";


test("the work-mode vocabulary has two distinct stable choices", () => {
  assert.deepEqual(
    WORK_MODES.map((mode) => mode.id),
    ["normal", "domain-capabilities"],
  );
  assert.equal(new Set(WORK_MODES.map((mode) => mode.label)).size, WORK_MODES.length);
});

test("unknown wire values are not accepted as work modes", () => {
  assert.equal(isWorkMode("normal"), true);
  assert.equal(isWorkMode("domain-capabilities"), true);
  assert.equal(isWorkMode("profile"), false);
});
