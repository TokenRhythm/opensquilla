const path = require("node:path");
const fs = require("node:fs");
const { execFile, execFileSync } = require("node:child_process");
const { promisify } = require("node:util");

const execFileAsync = promisify(execFile);
const projectDir = path.resolve(__dirname, "..");
const repositoryRoot = path.resolve(projectDir, "..", "..");
const policyPath = path.join(
  repositoryRoot,
  ".github",
  "signing",
  "windows-signing-policy.json",
);

function loadPolicy() {
  const policy = JSON.parse(fs.readFileSync(policyPath, "utf8"));
  if (policy.schemaVersion !== 1) {
    throw new Error(`Unsupported Windows signing policy schema: ${policy.schemaVersion}`);
  }
  if (!/^[0-9A-F]{40}$/u.test(policy.certificateSha1)) {
    throw new Error("Windows signing policy certificateSha1 must be 40 uppercase hexadecimal characters");
  }
  if (!policy.publisherSubjectContains || !policy.timestampUrl) {
    throw new Error("Windows signing policy is missing publisherSubjectContains or timestampUrl");
  }
  return policy;
}

function findSignTool() {
  if (process.env.SIGNTOOL_PATH) {
    return path.resolve(process.env.SIGNTOOL_PATH);
  }
  const result = execFileSync("where.exe", ["signtool.exe"], {
    encoding: "utf8",
    windowsHide: true,
  });
  const candidate = result.split(/\r?\n/u).map((line) => line.trim()).find(Boolean);
  if (!candidate) throw new Error("signtool.exe was not found on PATH");
  return candidate;
}

async function runSignTool(signTool, args) {
  const result = await execFileAsync(signTool, args, {
    windowsHide: true,
    maxBuffer: 16 * 1024 * 1024,
  });
  if (result.stdout) process.stdout.write(result.stdout);
  if (result.stderr) process.stderr.write(result.stderr);
}

async function main() {
  if (process.platform !== "win32") {
    throw new Error("Signed Windows packaging must run on Windows");
  }
  const policy = loadPolicy();
  const signTool = findSignTool();
  const electronBuilder = require(path.join(projectDir, "node_modules", "electron-builder"));

  async function sign(configuration) {
    if (configuration.hash !== "sha256") {
      throw new Error(`Unexpected signing hash: ${configuration.hash}`);
    }
    const targetPath = configuration.path;
    if (path.basename(targetPath).toLowerCase() === "opensquilla-gateway.exe") {
      try {
        await runSignTool(signTool, ["verify", "/pa", "/all", "/tw", targetPath]);
        process.stdout.write(`SIGN_SKIP_ALREADY_VALID=${targetPath}\n`);
        return;
      } catch (_error) {
        process.stdout.write(`SIGN_EXISTING_INVALID=${targetPath}\n`);
      }
    }
    process.stdout.write(`SIGNING_FILE=${targetPath}\n`);
    await runSignTool(signTool, [
      "sign",
      "/sha1",
      policy.certificateSha1,
      "/fd",
      "SHA256",
      "/tr",
      policy.timestampUrl,
      "/td",
      "SHA256",
      "/v",
      targetPath,
    ]);
  }

  process.stdout.write(
    `WINDOWS_SIGNING_POLICY=${JSON.stringify({
      certificateSha1: `${policy.certificateSha1.slice(0, 6)}...${policy.certificateSha1.slice(-6)}`,
      publisherSubjectContains: policy.publisherSubjectContains,
      timestampUrl: policy.timestampUrl,
    })}\n`,
  );
  const artifacts = await electronBuilder.build({
    targets: electronBuilder.Platform.WINDOWS.createTarget(),
    projectDir,
    publish: "never",
    config: {
      win: {
        signtoolOptions: {
          sign,
          publisherName: policy.publisherSubjectContains,
          signingHashAlgorithms: ["sha256"],
          rfc3161TimeStampServer: policy.timestampUrl,
        },
      },
    },
  });
  process.stdout.write(`SIGNED_ARTIFACTS=${JSON.stringify(artifacts)}\n`);
}

main().catch((error) => {
  process.stderr.write(`${error && error.stack ? error.stack : error}\n`);
  process.exitCode = 1;
});
