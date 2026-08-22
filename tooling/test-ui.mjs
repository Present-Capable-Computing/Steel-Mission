import {spawn} from "node:child_process";
import {mkdtemp, rm, readdir} from "node:fs/promises";
import {tmpdir} from "node:os";
import {dirname, join} from "node:path";
import {fileURLToPath} from "node:url";

import {build} from "esbuild";


const toolingDir = dirname(fileURLToPath(import.meta.url));
const repoDir = dirname(toolingDir);
const testDir = await mkdtemp(join(tmpdir(), "steel-mission-ui-tests-"));
const uiTestDir = join(repoDir, "steel-mission-ui", "tests");
const testEntries = (await readdir(uiTestDir)).filter(name => name.endsWith(".test.ts") || name.endsWith(".test.tsx")).sort();
if (testEntries.length === 0) {
  throw new Error("No test files found in steel-mission-ui/tests");
}
const bundledTests = testEntries.map((name) => join(testDir, name.replace(/\.tsx?$/, ".mjs")));

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
