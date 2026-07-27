import { spawnSync } from "node:child_process";
import { readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

const packageRoot = fileURLToPath(new URL("../", import.meta.url));
// Bun 1.3.14 can crash on Linux when native OpenTUI test files share a Bun
// process. Give each file a fresh process and keep concurrency inside it at 1.
const testFiles = readdirSync(new URL("../src/", import.meta.url), {
  withFileTypes: true,
})
  .filter((entry) => entry.isFile() && entry.name.endsWith(".bun.test.mjs"))
  .map((entry) => `src/${entry.name}`)
  .sort();

if (testFiles.length === 0) {
  throw new Error("No Bun test files found");
}

const bunExecutable = process.platform === "win32" ? "bun.exe" : "bun";
for (const testFile of testFiles) {
  console.log(`\n=== ${testFile} ===`);
  const result = spawnSync(
    bunExecutable,
    ["test", "--max-concurrency=1", testFile],
    {
      cwd: packageRoot,
      env: {
        ...process.env,
        OPENSQUILLA_TUI_COLOR: "truecolor",
      },
      stdio: "inherit",
    },
  );
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}
