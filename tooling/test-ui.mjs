import {spawn} from "node:child_process";
import {mkdtemp, rm} from "node:fs/promises";
import {tmpdir} from "node:os";
import {dirname, join} from "node:path";
import {fileURLToPath} from "node:url";

import {build} from "esbuild";


const toolingDir = dirname(fileURLToPath(import.meta.url));
const repoDir = dirname(toolingDir);
const testDir = await mkdtemp(join(tmpdir(), "steel-mission-ui-tests-"));
const bundledTest = join(testDir, "work-mode.test.mjs");

try {
  await build({
    entryPoints: [join(repoDir, "steel-mission-ui", "tests", "work-mode.test.ts")],
    bundle: true,
    format: "esm",
    platform: "node",
    target: ["node24"],
    outfile: bundledTest,
  });

  const exitCode = await new Promise((resolve, reject) => {
    const testRun = spawn(process.execPath, ["--test", bundledTest], {stdio: "inherit"});
    testRun.on("error", reject);
    testRun.on("exit", (code) => resolve(code ?? 1));
  });
  process.exitCode = exitCode;
} finally {
  await rm(testDir, {recursive: true, force: true});
}
