// 蛋仔桌宠配置：userData/pet-config.json，深合并默认值，原子写入。
"use strict";
const fs = require("node:fs");
const path = require("node:path");

const DEFAULTS = {
  gateway: {
    url: "ws://127.0.0.1:18791/ws",
    token: "",
  },
  companion: {
    // 与网关 [pet].bridge_url 对齐的本地回环 HTTP 契约端口。
    port: 45915,
    exe: "OpenSquilla.exe",
  },
  tts: {
    provider: "windows", // windows | openai
    baseUrl: "", // OpenAI 兼容 base，例如 https://api.openai.com/v1
    apiKey: "",
    model: "", // 例如 tts-1 / gpt-4o-mini-tts
    voice: "", // 例如 alloy / Microsoft Huihui Desktop
    voicesUrl: "", // 可选：音色列表端点覆盖
    volume: 1.0,
    rate: 1.0, // windows 语速倍率（映射到 -10..10）
    enabled: true,
  },
  pet: {
    muted: false,
    size: 1.0,
    autoSpeak: true, // WS 事件触发语音
    showTurnStart: false,
    x: null,
    y: null,
    bottom: null, // 窗口底边屏幕坐标（蛋仔脚底锚点，气泡长高时保持不变）
  },
  spool: {
    dir: "", // 空 = %APPDATA%/LIG/AgentRMCNative
    pauseFile: "", // 空 = %TEMP%/opensquilla-pet-bridge.pause
  },
};

function isObject(v) {
  return v && typeof v === "object" && !Array.isArray(v);
}

function deepMerge(base, override) {
  const out = { ...base };
  if (!isObject(override)) return out;
  for (const key of Object.keys(override)) {
    const bv = base ? base[key] : undefined;
    const ov = override[key];
    out[key] = isObject(bv) && isObject(ov) ? deepMerge(bv, ov) : ov;
  }
  return out;
}

class Config {
  constructor(userDataDir) {
    this.file = path.join(userDataDir, "pet-config.json");
    this.data = structuredClone(DEFAULTS);
    this.load();
  }

  load() {
    try {
      const raw = fs.readFileSync(this.file, "utf8");
      const parsed = JSON.parse(raw);
      this.data = deepMerge(DEFAULTS, parsed);
    } catch {
      this.data = structuredClone(DEFAULTS);
    }
    return this.data;
  }

  get() {
    return this.data;
  }

  set(partial) {
    this.data = deepMerge(this.data, partial || {});
    this.save();
    return this.data;
  }

  save() {
    try {
      fs.mkdirSync(path.dirname(this.file), { recursive: true });
      const tmp = this.file + ".tmp";
      fs.writeFileSync(tmp, JSON.stringify(this.data, null, 2), "utf8");
      fs.renameSync(tmp, this.file);
    } catch (err) {
      console.error("[pet] config save failed:", err.message);
    }
  }
}

module.exports = { Config, DEFAULTS };