// 离线 spool / 暂停文件，兼容网关 opensquilla.pet.spool 的路径与 JSONL 格式。
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");

const SPOOL_FILENAME = "pending-events.jsonl";
const PAUSE_FILENAME = "opensquilla-pet-bridge.pause";

function defaultSpoolDir() {
  const appdata = process.env.APPDATA;
  const base = appdata || os.tmpdir();
  return path.join(base, "LIG", "AgentRMCNative");
}

function defaultPauseFile() {
  const temp = process.env.TEMP || process.env.TMP || os.tmpdir();
  return path.join(temp, PAUSE_FILENAME);
}

function resolveSpoolDir(configured) {
  return configured && configured.trim() ? configured.trim() : defaultSpoolDir();
}

function resolvePauseFile(configured) {
  return configured && configured.trim() ? configured.trim() : defaultPauseFile();
}

function isPaused(pauseFile) {
  try {
    return fs.existsSync(pauseFile);
  } catch {
    return false;
  }
}

function spoolPath(dir) {
  return path.join(dir, SPOOL_FILENAME);
}

// 读取 spool 中的通知（容忍坏行）。
function readSpool(dir) {
  try {
    const raw = fs.readFileSync(spoolPath(dir), "utf8");
    const items = [];
    for (const line of raw.split("\n")) {
      if (!line.trim()) continue;
      try {
        const value = JSON.parse(line);
        if (value && typeof value === "object") items.push(value);
      } catch {
        /* skip bad line */
      }
    }
    return items;
  } catch {
    return [];
  }
}

// 导入后清空（网关侧离线积压一次性回放）。
function clearSpool(dir) {
  try {
    fs.writeFileSync(spoolPath(dir), "", "utf8");
  } catch {
    /* best-effort */
  }
}

module.exports = {
  defaultSpoolDir,
  defaultPauseFile,
  resolveSpoolDir,
  resolvePauseFile,
  isPaused,
  readSpool,
  clearSpool,
  SPOOL_FILENAME,
  PAUSE_FILENAME,
};