// 发送反馈冒烟（带 GUI，但窗口隐藏）：验证「回复当前会话」气泡在网关偏慢时
// 不会因为反复点击而重复发送，并且发送过程/结果都有可见反馈。
//
// 运行：node_modules/electron/dist/electron.exe scripts/smoke-send-feedback.cjs
// 断言：
//   1. 连点三次「发送」只产生一次 reply:send；
//   2. 点击后按钮立即进入忙碌态（禁用 + 发送中…），成功后显示「已发送」完成态；
//   3. 完成态停留片刻后气泡自动收起；
//   4. 失败时展示错误、按钮恢复可点、气泡保留；
//   5. 同一气泡里重试同一段内容复用同一个 clientMessageId（对网关是同一条消息）。
"use strict";
const path = require("node:path");
const { app, BrowserWindow, ipcMain } = require("electron");

const SEND_DELAY_MS = 400;
const records = { sends: [], dismisses: [], others: [] };
let failNextSend = null;

const results = [];
function check(name, ok, extra) {
  results.push({ name, ok });
  console.log((ok ? "PASS " : "FAIL ") + name + (extra ? " :: " + extra : ""));
}
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const stubConfig = {
  gateway: { url: "ws://127.0.0.1:18791/ws", token: "" },
  companion: { port: 45915, exe: "OpenSquilla.exe" },
  tts: { provider: "windows", baseUrl: "", apiKey: "", model: "", voice: "", voicesUrl: "", volume: 1, rate: 1, enabled: true },
  pet: { muted: false, autoSpeak: true, showTurnStart: false, size: 0.7 },
  spool: { dir: "", pauseFile: "" },
};

ipcMain.handle("test:invoke", async (_event, { channel, payload }) => {
  if (channel === "reply:send") {
    records.sends.push(payload || {});
    await wait(SEND_DELAY_MS); // 模拟偏慢的网关：这正是用户会连点的时间窗
    if (failNextSend) {
      const message = failNextSend;
      failNextSend = null;
      return { error: message };
    }
    return { ok: true };
  }
  if (channel === "bubble:dismiss") {
    records.dismisses.push(payload || {});
    return { ok: true };
  }
  records.others.push(channel);
  if (channel === "config:get") return stubConfig;
  if (channel === "state:get") return { gateway_connected: true, muted: false };
  return { ok: true };
});

const clickSendThrice = `(() => {
  const bubble = document.querySelector('.bubble');
  if (!bubble) return { error: 'no bubble' };
  const ta = bubble.querySelector('.b-textarea');
  const btn = bubble.querySelector('.b-actions .btn.primary');
  if (!ta || !btn) return { error: 'no reply controls' };
  ta.value = '冒烟：回复当前会话';
  btn.click(); btn.click(); btn.click();
  return { text: btn.textContent, disabled: btn.disabled };
})()`;

const readBubble = `(() => {
  const bubble = document.querySelector('.bubble');
  const btn = bubble && bubble.querySelector('.b-actions .btn.primary');
  const ta = bubble && bubble.querySelector('.b-textarea');
  const err = bubble && bubble.querySelector('.b-err');
  return {
    text: btn ? btn.textContent : null,
    disabled: btn ? btn.disabled : null,
    done: btn ? btn.classList.contains('done') : null,
    taDisabled: ta ? ta.disabled : null,
    err: err ? err.textContent : null,
  };
})()`;

async function run() {
  const win = new BrowserWindow({
    show: false,
    width: 420,
    height: 640,
    webPreferences: {
      preload: path.join(__dirname, "smoke-send-feedback-preload.cjs"),
      contextIsolation: true,
      sandbox: false,
      backgroundThrottling: false,
    },
  });
  const rendererErrors = [];
  win.webContents.on("render-process-gone", (_e, details) => rendererErrors.push(JSON.stringify(details)));
  await win.loadFile(path.join(__dirname, "..", "renderer", "index.html"));
  await wait(300); // 等 init() 的 config:get 完成

  // ── 成功路径：连点三次 ────────────────────────────────────────────
  await win.webContents.executeJavaScript("openReplyUI('agent:test:webchat:1'); true");
  await wait(150);
  const clicked = await win.webContents.executeJavaScript(clickSendThrice);
  check("打开「回复会话」气泡并连点三次发送", !clicked.error, JSON.stringify(clicked));
  check(
    "点击后立即进入忙碌态（禁用 + 发送中…）",
    clicked.disabled === true && /发送中/.test(clicked.text || ""),
    JSON.stringify(clicked),
  );
  check("连点三次只发出一次 reply:send", records.sends.length === 1, "sends=" + records.sends.length);
  check(
    "发送携带稳定的 clientMessageId",
    records.sends.length === 1 && String(records.sends[0].clientMessageId || "").startsWith("pet-"),
    JSON.stringify(records.sends[0] || {}),
  );

  await wait(SEND_DELAY_MS + 350);
  const afterOk = await win.webContents.executeJavaScript(readBubble);
  check("成功后显示「已发送」完成态", /已发送/.test(afterOk.text || "") && afterOk.done === true, JSON.stringify(afterOk));
  check("成功后输入框被锁定，避免再次发送", afterOk.taDisabled === true, JSON.stringify(afterOk));

  await wait(1500);
  const bubblesAfterFeedback = await win.webContents.executeJavaScript("document.querySelectorAll('.bubble').length");
  check(
    "完成态停留后气泡自动收起",
    records.dismisses.length === 1 && bubblesAfterFeedback === 0,
    JSON.stringify({ dismisses: records.dismisses.length, bubbles: bubblesAfterFeedback }),
  );

  // ── 失败路径：错误可见、可重试、不收起 ───────────────────────────
  failNextSend = "网关未连接";
  const sendsBeforeFailure = records.sends.length;
  await win.webContents.executeJavaScript("openReplyUI('agent:test:webchat:1'); true");
  await wait(150);
  await win.webContents.executeJavaScript(`(() => {
    const bubble = document.querySelector('.bubble');
    bubble.querySelector('.b-textarea').value = '冒烟：失败路径';
    bubble.querySelector('.b-actions .btn.primary').click();
    return true;
  })()`);
  await wait(SEND_DELAY_MS + 350);
  const afterFail = await win.webContents.executeJavaScript(readBubble);
  check("失败时展示错误文案", /网关未连接/.test(afterFail.err || ""), JSON.stringify(afterFail));
  check(
    "失败后按钮恢复可点（可以重试）",
    afterFail.disabled === false && /发送/.test(afterFail.text || "") && afterFail.done === false,
    JSON.stringify(afterFail),
  );
  const bubblesAfterFailure = await win.webContents.executeJavaScript("document.querySelectorAll('.bubble').length");
  check(
    "失败时不收起气泡",
    bubblesAfterFailure === 1 && records.dismisses.length === 1,
    JSON.stringify({ bubbles: bubblesAfterFailure, dismisses: records.dismisses.length }),
  );

  // ── 重试复用同一个 id ────────────────────────────────────────────
  await win.webContents.executeJavaScript(`(() => {
    document.querySelector('.bubble .b-actions .btn.primary').click();
    return true;
  })()`);
  await wait(SEND_DELAY_MS + 350);
  const failedSend = records.sends[sendsBeforeFailure];
  const retriedSend = records.sends[sendsBeforeFailure + 1];
  check(
    "同一气泡重试同一内容复用同一个 clientMessageId",
    Boolean(failedSend && retriedSend) && failedSend.clientMessageId === retriedSend.clientMessageId,
    JSON.stringify([failedSend && failedSend.clientMessageId, retriedSend && retriedSend.clientMessageId]),
  );
  check("渲染进程未崩溃", rendererErrors.length === 0, rendererErrors.join(" | "));

  const failed = results.filter((item) => !item.ok);
  console.log("\nSUMMARY: " + (results.length - failed.length) + "/" + results.length + " passed");
  win.destroy();
  app.exit(failed.length ? 1 : 0);
}

app.whenReady().then(() =>
  run().catch((error) => {
    console.error("SMOKE ERROR", error);
    app.exit(2);
  }),
);
