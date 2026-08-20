import {readFile, writeFile} from "node:fs/promises";
import {fileURLToPath} from "node:url";
import {dirname, join} from "node:path";

import {build} from "esbuild";


const toolingDir = dirname(fileURLToPath(import.meta.url));
const repoDir = dirname(toolingDir);
const outputPath = join(repoDir, "steel-mission-chat", "app.html");
const checkOnly = process.argv.slice(2).includes("--check");
const staticShell = (
  await readFile(join(repoDir, "steel-mission-ui", "static-shell.html"), "utf8")
).trim();
const indentedStaticShell = staticShell.split("\n").map((line) => `    ${line}`).join("\n");

const result = await build({
  entryPoints: [join(repoDir, "steel-mission-ui", "src", "app.tsx")],
  bundle: true,
  format: "iife",
  platform: "browser",
  target: ["es2020"],
  jsx: "automatic",
  jsxImportSource: "preact",
  minify: false,
  sourcemap: false,
  write: false,
  outdir: join(repoDir, ".ui-build"),
});

const script = result.outputFiles.find((file) => file.path.endsWith(".js"));
const styles = result.outputFiles.find((file) => file.path.endsWith(".css"));
if (!script || !styles) {
  throw new Error("UI build must emit one JavaScript bundle and one stylesheet");
}

const page = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>Steel Mission</title>
  <style>
${styles.text.trim()}
  </style>
</head>
<body>
  <div id="app">
${indentedStaticShell}
  </div>
  <script>
${script.text.trim()}
  </script>
</body>
</html>
`;

if (checkOnly) {
  const committed = await readFile(outputPath, "utf8");
  if (committed !== page) {
    console.error("steel-mission-chat/app.html differs from a clean UI rebuild");
    process.exitCode = 1;
  }
} else {
  await writeFile(outputPath, page);
  console.log("built steel-mission-chat/app.html");
}
