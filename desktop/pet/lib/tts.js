// TTS 引擎：
//  - windows：Windows PowerShell 5.1 + System.Speech（本地 SAPI，免 KEY，含中文音色）
//  - openai：OpenAI 兼容 /audio/speech（云端，URL/KEY/model/voice 可配）
// 模型/音色拉取：/models、/audio/voices（或自定义 voicesUrl），失败回退预置音色。
// 文本通过环境变量传给 PowerShell，彻底规避中文/引号转义问题。
"use strict";
const { execFile } = require("node:child_process");

const OPENAI_PRESET_VOICES = [
  "alloy", "echo", "fable", "onyx", "nova", "shimmer", "ash", "sage", "coral", "ballad",
];

function clamp(n, lo, hi) {
  return Math.max(lo, Math.min(hi, n));
}

function runPowerShell(script, env, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    if (process.platform !== "win32") {
      reject(new Error("windows TTS 仅在 Windows 可用"));
      return;
    }
    execFile(
      "powershell.exe",
      ["-NoProfile", "-NonInteractive", "-Command", script],
      { env: { ...process.env, ...env }, windowsHide: true, timeout: timeoutMs, maxBuffer: 4 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err) {
          reject(new Error((stderr && stderr.trim()) || err.message));
          return;
        }
        resolve(String(stdout || ""));
      }
    );
  });
}

const SPEAK_SCRIPT = [
  "$ErrorActionPreference='Stop'",
  "Add-Type -AssemblyName System.Speech",
  "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer",
  "if ($env:PET_TTS_VOICE) { try { $s.SelectVoice($env:PET_TTS_VOICE) } catch {} }",
  "try { $s.Volume = [int]([double]$env:PET_TTS_VOLUME * 100) } catch { $s.Volume = 100 }",
  "try { $s.Rate = [int]$env:PET_TTS_RATE } catch { $s.Rate = 0 }",
  "$s.Speak($env:PET_TTS_TEXT)",
  "$s.Dispose()",
].join("; ");

const VOICES_SCRIPT = [
  "Add-Type -AssemblyName System.Speech",
  "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer",
  "$s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }",
  "$s.Dispose()",
].join("; ");

async function listWindowsVoices() {
  const out = await runPowerShell(VOICES_SCRIPT, {}, 20000);
  return out
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

async function speakWindows(text, { voice, volume = 1, rate = 1 } = {}) {
  const sapiRate = clamp(Math.round((Number(rate) - 1) * 10), -10, 10);
  await runPowerShell(SPEAK_SCRIPT, {
    PET_TTS_TEXT: text,
    PET_TTS_VOICE: voice || "",
    PET_TTS_VOLUME: String(clamp(Number(volume), 0, 1)),
    PET_TTS_RATE: String(sapiRate),
  });
  return { provider: "windows", played: true };
}

function trimBase(baseUrl) {
  return String(baseUrl || "").trim().replace(/\/+$/, "");
}

async function fetchWithTimeout(url, options, timeoutMs = 30000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

function authHeaders(apiKey) {
  const headers = { "Content-Type": "application/json" };
  if (apiKey) headers.Authorization = "Bearer " + apiKey;
  return headers;
}

async function speakOpenAI(text, cfg) {
  const base = trimBase(cfg.baseUrl);
  if (!base) throw new Error("未配置 TTS baseUrl");
  if (!cfg.model) throw new Error("未配置 TTS model");
  const body = {
    model: cfg.model,
    input: text,
    voice: cfg.voice || "alloy",
    response_format: "mp3",
  };
  const res = await fetchWithTimeout(base + "/audio/speech", {
    method: "POST",
    headers: authHeaders(cfg.apiKey),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error("TTS HTTP " + res.status + ": " + detail.slice(0, 200));
  }
  const buf = Buffer.from(await res.arrayBuffer());
  const mime = res.headers.get("content-type") || "audio/mpeg";
  return { provider: "openai", audio: { mime, base64: buf.toString("base64") } };
}

function extractIds(json) {
  if (Array.isArray(json)) return json.map((x) => (typeof x === "string" ? x : x && (x.id || x.name))).filter(Boolean);
  if (json && Array.isArray(json.data)) return json.data.map((x) => x && (x.id || x.name)).filter(Boolean);
  if (json && Array.isArray(json.models)) return json.models.map((x) => (typeof x === "string" ? x : x && (x.id || x.name))).filter(Boolean);
  return [];
}

async function listModels(cfg) {
  const base = trimBase(cfg.baseUrl);
  if (!base) throw new Error("未配置 TTS baseUrl");
  const res = await fetchWithTimeout(base + "/models", { headers: authHeaders(cfg.apiKey) }, 20000);
  if (!res.ok) throw new Error("models HTTP " + res.status);
  const json = await res.json();
  const ids = [...new Set(extractIds(json))].sort();
  return { models: ids };
}

function extractVoices(json) {
  const pick = (x) => (typeof x === "string" ? x : x && (x.id || x.name || x.voice || x.short_name));
  if (Array.isArray(json)) return json.map(pick).filter(Boolean);
  if (json && Array.isArray(json.voices)) return json.voices.map(pick).filter(Boolean);
  if (json && Array.isArray(json.data)) return json.data.map(pick).filter(Boolean);
  if (json && Array.isArray(json.result)) return json.result.map(pick).filter(Boolean);
  return [];
}

async function listVoices(cfg) {
  const base = trimBase(cfg.baseUrl);
  const candidates = [];
  if (cfg.voicesUrl && cfg.voicesUrl.trim()) candidates.push(cfg.voicesUrl.trim());
  if (base) {
    candidates.push(base + "/audio/voices");
    candidates.push(base + "/voices");
  }
  for (const url of candidates) {
    try {
      const res = await fetchWithTimeout(url, { headers: authHeaders(cfg.apiKey) }, 15000);
      if (!res.ok) continue;
      const json = await res.json();
      const voices = [...new Set(extractVoices(json))];
      if (voices.length) return { voices, source: url };
    } catch {
      /* try next */
    }
  }
  return { voices: OPENAI_PRESET_VOICES, source: "preset" };
}

// 复用桌面 app 的语音配置：网关 /api/audio/speech 服务端合成（URL/Key/音色都在网关侧）。
async function speakGateway(text, { gatewayBase, voice, model } = {}) {
  const base = String(gatewayBase || "").trim().replace(/\/+$/, "");
  if (!base) throw new Error("未连接网关，无法复用桌面语音");
  const payload = { text };
  if (voice) payload.voice = voice;
  if (model) payload.model = model;
  const res = await fetchWithTimeout(
    base + "/api/audio/speech",
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) },
    60000
  );
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error("gateway speech HTTP " + res.status + ": " + detail.slice(0, 160));
  }
  const buf = Buffer.from(await res.arrayBuffer());
  const mime = res.headers.get("content-type") || "audio/mpeg";
  return { provider: "gateway", audio: { mime, base64: buf.toString("base64") } };
}

// 统一入口。gateway=复用桌面配置（失败回退本地）；windows 直接出声；openai 返回音频由渲染进程播放。
async function speak(text, cfg, gatewayBase) {
  const clean = String(text || "").trim();
  if (!clean) return { skipped: true };
  if (cfg.enabled === false) return { skipped: true, reason: "disabled" };
  if (cfg.provider === "gateway") {
    try {
      return await speakGateway(clean, { gatewayBase, voice: cfg.voice, model: cfg.model });
    } catch {
      // 网关语音不可用（未配置/离线）时回退本地 SAPI，保证蛋仔仍能出声。
      return speakWindows(clean, cfg);
    }
  }
  if (cfg.provider === "openai") return speakOpenAI(clean, cfg);
  return speakWindows(clean, cfg);
}

module.exports = {
  speak,
  speakWindows,
  speakOpenAI,
  speakGateway,
  listWindowsVoices,
  listModels,
  listVoices,
  OPENAI_PRESET_VOICES,
};