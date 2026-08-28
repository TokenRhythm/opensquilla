import net from "node:net";
import readline from "node:readline";

export const HOST_PROTOCOL_VERSION = 1;

// Connect to the parent-owned loopback listener and authenticate before any
// product protocol frames are exchanged. Keeping the token in the inherited
// environment avoids exposing it in the process command line.
export async function connectIpc({ host, port, token, protocol = HOST_PROTOCOL_VERSION, timeoutMs = 5000 }) {
  if (!host || !Number.isInteger(port) || port <= 0 || !token) {
    throw new Error("OpenTUI IPC environment is incomplete");
  }
  const socket = net.createConnection({ host, port });
  socket.setNoDelay(true);
  const lines = readline.createInterface({ input: socket, crlfDelay: Infinity });

  await new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => finish(new Error("OpenTUI IPC authentication timed out")), timeoutMs);
    timer.unref?.();
    const finish = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      lines.off("line", onLine);
      socket.off("error", onError);
      socket.off("close", onClose);
      if (error) {
        reject(error);
      } else {
        // Keep the authenticated stream paused until createIpc.start() has
        // installed the product dispatcher. Python can send canonical
        // bootstrap frames immediately after HostReady; a flowing readline
        // interface with no line listener silently discards that whole first
        // screen in the tiny send-ready -> start gap.
        lines.pause();
        resolve();
      }
    };
    const onError = (error) => finish(error);
    const onClose = () => finish(new Error("OpenTUI IPC closed during authentication"));
    const onLine = (line) => {
      try {
        const message = JSON.parse(line);
        if (message?.type !== "auth.ok" || message?.protocol !== protocol) {
          finish(new Error(message?.message ?? "OpenTUI IPC authentication failed"));
          return;
        }
        finish();
      } catch {
        finish(new Error("OpenTUI IPC returned an invalid authentication response"));
      }
    };
    socket.once("error", onError);
    socket.once("close", onClose);
    lines.once("line", onLine);
    socket.once("connect", () => {
      try {
        socket.write(`${JSON.stringify({ type: "auth", token, protocol })}\n`, "utf8");
      } catch (error) {
        finish(error);
      }
    });
  }).catch((error) => {
    lines.close();
    socket.destroy();
    throw error;
  });

  return createIpc({ socket, lines });
}

export function connectIpcFromEnv(env = process.env) {
  const port = Number(env.OPENSQUILLA_OPENTUI_IPC_PORT);
  const protocol = Number(env.OPENSQUILLA_OPENTUI_PROTOCOL_VERSION ?? HOST_PROTOCOL_VERSION);
  return connectIpc({
    host: env.OPENSQUILLA_OPENTUI_IPC_HOST,
    port,
    token: env.OPENSQUILLA_OPENTUI_IPC_TOKEN,
    protocol,
  });
}

export function createIpc({ socket, lines = readline.createInterface({ input: socket, crlfDelay: Infinity }) }) {
  let closed = false;
  let closeCallback = null;

  // Install lifecycle listeners immediately. main.mjs builds the composer
  // between authentication and start(); a parent death in that window must be
  // remembered instead of becoming an unhandled socket error.
  const close = () => {
    if (closed) return;
    closed = true;
    lines.close();
    closeCallback?.();
  };
  lines.on("error", close);
  socket.on("close", close);
  socket.on("error", close);

  function send(message) {
    // send() is called from unguarded hot paths. A dead Python parent must not
    // turn a best-effort status write into an uncaught exception that skips the
    // renderer's terminal teardown.
    try {
      if (!socket.destroyed) socket.write(`${JSON.stringify(message)}\n`, "utf8");
    } catch {
      // Closed socket / unserializable payload: nothing actionable at teardown.
    }
  }

  function start(onMessage, onClose) {
    closeCallback = onClose;
    if (closed) {
      onClose();
      return;
    }
    lines.on("line", (line) => {
      if (!line.trim()) return;
      try {
        onMessage(JSON.parse(line));
      } catch (error) {
        send({ type: "error", message: error instanceof Error ? error.message : String(error) });
      }
    });
    lines.on("close", close);
    // connectIpc pauses after authentication specifically so product frames
    // received before this handler exists remain in the socket buffer.
    lines.resume();
  }
  return { send, start };
}

// Build a dispatcher that routes block.* + turn.* + composer/router to handlers.
export function createDispatcher(h) {
  return (m) => {
    switch (m.type) {
      case "turn.begin": return h.turnBegin(m);
      case "turn.end": return h.turnEnd(m);
      case "turn.status": return h.turnStatus(m);
      case "prompt.state": return h.promptState?.(m);
      case "composer.set": return h.composerSet(m);
      case "attachment.add": return h.attachmentAdd?.(m);
      case "attachment.update": return h.attachmentUpdate?.(m);
      case "attachment.remove": return h.attachmentRemove?.(m);
      case "attachment.clear": return h.attachmentClear?.(m);
      case "history.replace": return h.historyReplace?.(m);
      case "completion.context": return h.completionContext?.(m);
      case "completion.response": return h.completionResponse?.(m);
      case "context.update": return h.contextUpdate?.(m);
      case "router.update": return h.routerUpdate(m);
      case "model.routing.state": return h.modelRoutingState?.(m);
      case "model.routing.picker": return h.modelRoutingPicker?.(m);
      case "model.picker": return h.modelPicker?.(m);
      case "block.begin": return h.blockBegin(m);
      case "block.append": return h.blockAppend(m);
      case "block.update": return h.blockUpdate(m);
      case "block.end": return h.blockEnd(m);
      case "prompt.echo": return h.promptEcho?.(m);
      case "model.text": return h.modelText?.(m);
      case "scrollback.write": return h.scrollback?.(m);
      case "notice.write": return h.notice?.(m);
      case "theme.set": return h.themeSet?.(m);
      case "theme.pick": return h.themePick?.(m);
      case "session.pick": return h.sessionPick?.(m);
      case "approval.request": return h.approvalRequest?.(m);
      case "approval.dismiss": return h.approvalDismiss?.(m);
      case "shutdown": return h.shutdown(m);
      default: return h.unknown(m);
    }
  };
}
