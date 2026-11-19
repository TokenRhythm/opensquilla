// 发送反馈冒烟测试专用的 petAPI 替身：所有 invoke 统一走 test:invoke，
// 由冒烟脚本按通道记录并返回，绝不触碰真实网关或真实会话。
"use strict";
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("petAPI", {
  invoke: (channel, payload) => ipcRenderer.invoke("test:invoke", { channel, payload }),
  on: (channel, handler) => {
    ipcRenderer.on("test:event:" + channel, (_event, payload) => handler(payload));
  },
});
