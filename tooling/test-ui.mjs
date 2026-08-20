import {spawn} from "node:child_process";
import {mkdtemp, rm} from "node:fs/promises";
import {tmpdir} from "node:os";
import {dirname, join} from "node:path";
import {fileURLToPath} from "node:url";

import {build} from "esbuild";


const toolingDir = dirname(fileURLToPath(import.meta.url));
const repoDir = dirname(toolingDir);
const testDir = await mkdtemp(join(tmpdir(), "steel-mission-ui-tests-"));
const testEntries = ["assignments.test.ts", "capabilities.test.ts", "control-plane.test.ts", "knowledge.test.ts", "missions.test.ts", "organizations.test.ts", "runtime-profiles.test.ts", "settings.test.ts", "users.test.ts", "work-mode.test.ts"];
const bundledTests = testEntries.map((name) => join(testDir, name.replace(/\.ts$/, ".mjs")));

try {
  await build({
    entryPoints: testEntries.map((name) => join(repoDir, "steel-mission-ui", "tests", name)),
    bundle: true,
    format: "esm",
    platform: "node",
    target: ["node24"],
    outdir: testDir,
    outExtension: {".js": ".mjs"},
  });

  const exitCode = await new Promise((resolve, reject) => {
    const testRun = spawn(process.execPath, ["--test", ...bundledTests], {stdio: "inherit"});
    testRun.on("error", reject);
    testRun.on("exit", (code) => resolve(code ?? 1));
  });
  process.exitCode = exitCode;
} finally {
  await rm(testDir, {recursive: true, force: true});
}
