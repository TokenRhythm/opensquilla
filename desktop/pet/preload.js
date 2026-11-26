// 沙箱 preload：仅暴露 contextBridge + ipcRenderer 的最小 API。
"use strict";
const { contextBridge, ipcRenderer } = require("electron");

const invokeChannels = [
  "config:get", "config:set", "state:get", "sessions:list",
  "reply:send", "approval:resolve", "clarify:submit", "bubble:dismiss",
  "pet:open-session", "tts:test", "tts:models", "tts:voices", "tts:windows-voices",
  "pet:set-muted", "pet:hide", "pet:quit", "pet:open-settings",
];

const onChannels = [
  "bubble:show", "bubble:dismiss", "pet:state", "pet:toast", "tts:play",
  "sessions:updated", "settings:open", "reply:open", "ctxmenu:dismiss",
];

const api = {
  invoke: (channel, payload) => {
    if (!invokeChannels.includes(channel)) return Promise.reject(new Error("blocked channel: " + channel));
    return ipcRenderer.invoke(channel, payload);
  },
  send: (channel, payload) => {
    if (["pet:drag", "pet:drag-end", "pet:fit", "mouse-ignore", "menu:open", "menu:close", "tts:ended"].includes(channel)) {
      ipcRenderer.send(channel, payload);
    }
  },
  on: (channel, callback) => {
    if (!onChannels.includes(channel)) return () => {};
    const listener = (_e, data) => callback(data);
    ipcRenderer.on(channel, listener);
    return () => ipcRenderer.removeListener(channel, listener);
  },
};

contextBridge.exposeInMainWorld("petAPI", api);