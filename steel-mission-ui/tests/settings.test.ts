import assert from "node:assert/strict";
import test from "node:test";

import {SETTINGS_SECTIONS} from "../src/settings";


test("settings exposes the eight agreed sections in navigation order", () => {
  assert.deepEqual(
    SETTINGS_SECTIONS.map(({id, label}) => [id, label]),
    [
      ["organizations", "Organizations"],
      ["people", "Users"],
      ["missions", "Missions"],
      ["knowledge", "Knowledge"],
      ["profiles", "Runtime Profiles"],
      ["integrations", "Control Plane"],
      ["models", "Model Roles"],
      ["audit", "Audit"],
    ],
  );
});

test("every settings section has a unique id and a useful hint", () => {
  assert.equal(new Set(SETTINGS_SECTIONS.map((section) => section.id)).size, 8);
  assert.equal(SETTINGS_SECTIONS.every((section) => section.hint.length > 10), true);
});
