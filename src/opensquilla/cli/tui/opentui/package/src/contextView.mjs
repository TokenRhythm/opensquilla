import { THEME } from "./theme.mjs";
import {
  clampFooterHeight,
  clipToCells,
  stripTerminalControls,
  textWidth,
} from "./primitives.mjs";
import { destroyChildren } from "./renderableLifecycle.mjs";
import { SURFACE_Z_INDEX, rendererViewportSnapshot } from "./screenMode.mjs";

// The rail appears only when subtracting its minimum width still leaves the
// transcript enough room for the full welcome wordmark. This makes responsive
// behavior monotonic: growing from 131 -> 132 columns never makes the primary
// content shrink from the block logo to the tiny logo.
export const CONTEXT_RAIL_MIN_COLUMNS = 132;
export const CONTEXT_RAIL_MIN_WIDTH = 30;
export const CONTEXT_RAIL_MAX_WIDTH = 36;
export const CONTEXT_HEADER_HEIGHT = 2;

const CONTEXT_KEYS = Object.freeze([
  "agent",
  "agentEmoji",
  "agentId",
  "task",
  "surface",
  "gateway",
  "model",
  "permission",
  "workspace",
  "queue",
  "context",
]);

export function emptyContextState() {
  return Object.fromEntries(CONTEXT_KEYS.map((key) => [key, ""]));
}

function clean(value) {
  if (value === null || value === undefined) return "";
  return stripTerminalControls(String(value)).replace(/\s+/gu, " ").trim();
}

function displayValue(value) {
  const normalized = clean(value);
  return /^(?:pending|-)$/iu.test(normalized) ? "" : normalized;
}

function own(object, key) {
  return object !== null
    && typeof object === "object"
    && Object.prototype.hasOwnProperty.call(object, key);
}

function firstPresent(object, keys) {
  for (const key of keys) {
    if (own(object, key)) return { present: true, value: object[key] };
  }
  return { present: false, value: undefined };
}

// Normalize the additive `context.update` frame. Partial frames preserve the
// previous snapshot; explicit null/empty values clear a field. `agent` may be a
// display string or a small object ({name, emoji, id}) so a future Python
// bootstrap can forward its canonical identity without another host protocol.
export function normalizeContextUpdate(message, previous = emptyContextState()) {
  const next = { ...emptyContextState(), ...(previous ?? {}) };
  const rawAgent = own(message, "agent") ? message.agent : undefined;
  const agentObject = rawAgent !== null && typeof rawAgent === "object" && !Array.isArray(rawAgent)
    ? rawAgent
    : null;
  const agentField = (nestedKeys, messageKeys) => {
    const nested = agentObject ? firstPresent(agentObject, nestedKeys) : { present: false };
    return nested.present ? nested : firstPresent(message, messageKeys);
  };

  const fields = {
    agent: agentObject
      ? agentField(["name", "display_name", "displayName"], ["agent_name", "agentName"])
      : firstPresent(message, ["agent"]),
    agentEmoji: agentField(["emoji"], ["agent_emoji", "agentEmoji"]),
    agentId: agentField(["id", "agent_id", "agentId"], ["agent_id", "agentId"]),
    task: firstPresent(message, ["task", "task_title", "taskTitle", "session_title", "sessionTitle"]),
    surface: firstPresent(message, ["surface", "surface_id", "surfaceId"]),
    gateway: firstPresent(message, ["gateway", "gateway_status", "gatewayStatus", "connection"]),
    model: firstPresent(message, ["model", "effective_model", "effectiveModel"]),
    permission: firstPresent(message, ["permission", "permissions", "permission_mode", "permissionMode"]),
    workspace: firstPresent(message, ["workspace", "workspace_label", "workspaceLabel"]),
    queue: firstPresent(message, ["queue", "queue_status", "queueStatus"]),
    context: firstPresent(message, ["context", "context_status", "contextStatus"]),
  };

  for (const [key, field] of Object.entries(fields)) {
    if (field.present) next[key] = clean(field.value);
  }
  return next;
}

export function hasContextState(context) {
  return CONTEXT_KEYS.some((key) => clean(context?.[key]));
}

export function isWideContextLayout(terminalWidth) {
  return (Number(terminalWidth) || 0) >= CONTEXT_RAIL_MIN_COLUMNS;
}

export function contextRailWidth(terminalWidth) {
  const width = Number(terminalWidth) || 0;
  if (!isWideContextLayout(width)) return 0;
  return Math.max(
    CONTEXT_RAIL_MIN_WIDTH,
    Math.min(CONTEXT_RAIL_MAX_WIDTH, Math.floor(width * 0.225)),
  );
}

function shortModel(value) {
  const model = clean(value);
  return model ? model.split("/").pop() : "";
}

function shortWorkspace(value) {
  const workspace = clean(value).replace(/[\\/]+$/u, "");
  if (!workspace) return "";
  const parts = workspace.split(/[\\/]/u).filter(Boolean);
  if (parts.length <= 2) return workspace;
  return `…/${parts.slice(-2).join("/")}`;
}

function permissionLabel(value) {
  const permission = clean(value);
  const normalized = permission.toLowerCase().replaceAll("_", "-");
  if (normalized === "workspace-write") return "write";
  if (normalized === "read-only" || normalized === "restricted") return "read-only";
  return permission;
}

function gatewayLabel(value) {
  const gateway = clean(value);
  if (!gateway) return "";
  const normalized = gateway.toLowerCase();
  if (/connected|ready|healthy|^ok$/u.test(normalized)) return "GW ✓";
  if (/disconnected|failed|error|offline/u.test(normalized)) return "GW ✗";
  if (/connecting|starting|pending/u.test(normalized)) return "GW …";
  if (/isolated|standalone/u.test(normalized)) return "isolated";
  return `GW ${gateway}`;
}

export function contextAgentLabel(context) {
  if (!hasContextState(context)) return "squilla";
  const value = { ...emptyContextState(), ...(context ?? {}) };
  const identity = displayValue(value.agent) || displayValue(value.agentId);
  return `${value.agentEmoji ? `${displayValue(value.agentEmoji)}${identity ? " " : ""}` : ""}${identity}` || "squilla";
}

// One quiet, fixed-height identity row above the transcript. Items are selected
// by product priority (brand -> connection -> identity -> task -> shared
// surface), then rendered in the canonical visual order requested by the
// product: brand · task · agent · shared surface · Gateway. This lets an 80-cell
// terminal drop low-priority context without ever wrapping the header.
export function contextHeaderItems(context, terminalWidth, railWidth = contextRailWidth(terminalWidth)) {
  if (!hasContextState(context)) return [];
  const value = { ...emptyContextState(), ...(context ?? {}) };
  const identity = contextAgentLabel(value);
  const surface = displayValue(value.surface);
  const candidates = [
    value.task ? { key: "task", content: clipToCells(value.task, 24), token: "text", priority: 3, order: 1 } : null,
    identity !== "squilla" ? { key: "agent", content: clipToCells(identity, 20), token: "brandAccentSoft", priority: 2, order: 2 } : null,
    { key: "surface", content: clipToCells(surface ? `shared · ${surface}` : "shared", 18), token: "muted", priority: 4, order: 3 },
    value.gateway ? { key: "gateway", content: clipToCells(gatewayLabel(value.gateway), 14), token: "routeText", priority: 1, order: 4 } : null,
  ].filter(Boolean);
  // Persistent brand anchor: keep the large OPEN… wordmark for the empty-state
  // welcome, and retain a one-row mark after the transcript fills. This gives
  // daily coding sessions identity without permanently spending 6–8 rows.
  const brand = { key: "brand", content: "OpenSquilla", token: "brandAccent", order: 0 };
  const available = Math.max(1, (Number(terminalWidth) || 80) - (Number(railWidth) || 0) - 2);
  const selected = [brand];
  let used = textWidth(brand.content);
  for (const item of candidates.toSorted((a, b) => a.priority - b.priority)) {
    const width = 3 + textWidth(item.content); // " · "
    if (used + width > available) continue;
    selected.push(item);
    used += width;
  }
  return selected.toSorted((a, b) => a.order - b.order);
}

function routerRoute(router) {
  const route = displayValue(router?.route);
  const source = clean(router?.source);
  if (router?.rolloutPhase === "observe" || router?.routingApplied === false) {
    return route ? `${route} observe` : "observe";
  }
  if (["forced", "observe", "fallback"].includes(source)) {
    return route ? `${route} ·${source}` : source;
  }
  return route;
}

function abnormalRoute(router) {
  return router?.style === "warning"
    || router?.style === "error"
    || router?.rolloutPhase === "observe"
    || router?.routingApplied === false
    || ["forced", "observe", "fallback"].includes(clean(router?.source));
}

function hasRouterDecision(router) {
  const route = displayValue(router?.route);
  if (!route) return false;
  // Bootstrap describes the transport as gateway/standalone before any model
  // routing decision exists. Keep those placeholders out of the compact strip,
  // but retain every real decision — including an ordinary applied route.
  return !/^(?:gateway|standalone)$/iu.test(route);
}

function effectiveContext(context, router) {
  return {
    ...emptyContextState(),
    ...(context ?? {}),
    model: displayValue(context?.model) || displayValue(router?.model),
    context: displayValue(context?.context) || displayValue(router?.context),
  };
}

function compactItem(key, value, token, maxCells) {
  const normalized = clean(value);
  if (!normalized) return null;
  return { key, content: clipToCells(normalized, maxCells), token };
}

// Priority-ordered footer content for terminals narrower than 132 columns.
// Fitting happens by display cells (not JS string length), so CJK/emoji identity
// names cannot push a safety or connection field beyond the viewport.
export function compactContextItems(context, router, terminalWidth) {
  if (!hasContextState(context)) return [];
  const value = effectiveContext(context, router);
  const identityName = value.agent || value.agentId;
  const agent = `${value.agentEmoji ? `${value.agentEmoji}${identityName ? " " : ""}` : ""}${identityName}`;
  const routeIsAbnormal = abnormalRoute(router);
  const candidates = [
    compactItem("agent", agent, "brandAccentSoft", 20),
    compactItem("permission", permissionLabel(value.permission), "warning", 15),
    hasRouterDecision(router) ? compactItem(
      "route",
      `router ${routerRoute(router)}`,
      router?.style === "error" ? "error" : routeIsAbnormal ? "warning" : "routeText",
      20,
    ) : null,
    compactItem("gateway", gatewayLabel(value.gateway), "routeText", 14),
    compactItem("model", shortModel(value.model), "text", 18),
    Number(terminalWidth) >= 104 ? compactItem("task", value.task, "muted", 20) : null,
    compactItem("queue", value.queue ? `queue ${value.queue}` : "", "muted", 16),
    compactItem("context", value.context ? `ctx ${value.context}` : "", "detailText", 14),
  ].filter(Boolean);

  const available = Math.max(1, (Number(terminalWidth) || 80) - 8);
  const fitted = [];
  let used = 0;
  for (const item of candidates) {
    const separator = fitted.length ? 3 : 0; // " · "
    const width = textWidth(item.content);
    if (used + separator + width <= available) {
      fitted.push(item);
      used += separator + width;
    }
  }
  // A pathological single over-wide identity still renders as one clipped item
  // instead of leaving the strip blank.
  if (fitted.length === 0 && candidates.length) {
    fitted.push({ ...candidates[0], content: clipToCells(candidates[0].content, available) });
  }
  return fitted;
}

export function contextRailRows(context, router) {
  const value = effectiveContext(context, router);
  const agent = `${value.agentEmoji ? `${value.agentEmoji} ` : ""}${value.agent || value.agentId || "OpenSquilla"}`;
  const route = routerRoute(router);
  const agentRows = [
    { kind: "section", key: "agent-heading", content: "AGENT", token: "brandAccent" },
    { kind: "primary", key: "agent", content: agent, token: "text" },
  ];
  const taskRows = [
    value.task ? { kind: "primary", key: "task", content: value.task, token: "text" } : null,
    value.workspace ? { kind: "field", key: "workspace", label: "workspace", value: shortWorkspace(value.workspace), token: "muted" } : null,
    value.surface ? { kind: "field", key: "surface", label: "surface", value: value.surface, token: "muted" } : null,
  ].filter(Boolean);
  const runtimeRows = [
    value.model ? { kind: "field", key: "model", label: "model", value: shortModel(value.model), token: "text" } : null,
    value.gateway ? { kind: "field", key: "gateway", label: "gateway", value: gatewayLabel(value.gateway).replace(/^GW\s+/u, ""), token: "routeText" } : null,
    value.queue ? { kind: "field", key: "queue", label: "queue", value: value.queue, token: "muted" } : null,
    value.context ? { kind: "field", key: "context", label: "context", value: value.context, token: "detailText" } : null,
  ].filter(Boolean);
  const safetyRows = [
    value.permission ? { kind: "field", key: "permission", label: "permission", value: permissionLabel(value.permission), token: "warning" } : null,
  ].filter(Boolean);
  const routingRows = [
    route ? { kind: "field", key: "route", label: "route", value: route, token: router?.style === "error" ? "error" : router?.style === "warning" ? "warning" : "routeText" } : null,
    displayValue(router?.saving) ? { kind: "field", key: "saving", label: "save", value: displayValue(router.saving), token: "metricPositive" } : null,
    displayValue(router?.io) ? { kind: "field", key: "io", label: "io", value: displayValue(router.io), token: "detailText" } : null,
  ].filter(Boolean);
  return [
    ...agentRows,
    ...(taskRows.length ? [
      { kind: "spacer", key: "task-gap" },
      { kind: "section", key: "task-heading", content: "TASK", token: "brandAccent" },
      ...taskRows,
    ] : []),
    ...(runtimeRows.length ? [
      { kind: "spacer", key: "runtime-gap" },
      { kind: "section", key: "runtime-heading", content: "RUNTIME", token: "brandAccent" },
      ...runtimeRows,
    ] : []),
    ...(safetyRows.length ? [
      { kind: "spacer", key: "safety-gap" },
      { kind: "section", key: "safety-heading", content: "SAFETY", token: "brandAccent" },
      ...safetyRows,
    ] : []),
    ...(routingRows.length ? [
      { kind: "spacer", key: "router-gap" },
      { kind: "section", key: "router-heading", content: "ROUTING", token: "brandAccent" },
      ...routingRows,
    ] : []),
  ];
}

// Render controller for the wide context rail. It also owns the transcript's
// right inset, keeping the conversation a single linear column rather than a
// second independently-scrollable pane.
export function createContextRail({
  renderer,
  BoxRenderable,
  TextRenderable,
  conversationBox,
  inputBox,
  footerHeight,
  viewport = () => rendererViewportSnapshot(renderer),
  allowWideRail = true,
}) {
  let context = emptyContextState();
  let router = {};
  const initialWidth = contextRailWidth(viewport().width) || CONTEXT_RAIL_MIN_WIDTH;
  // OpenTUI emits `resize` after marking Yoga dirty but before the next frame
  // recomputes child geometry. Reading node.width in that callback therefore
  // returns the previous frame's computed width. Keep the logical inset here so
  // transcript, welcome, turn and composer relayouts all observe one atomic
  // viewport even before Yoga renders it.
  let currentRightInset = 0;
  const node = new BoxRenderable(renderer, {
    id: "context-rail",
    position: "absolute",
    top: 0,
    right: 0,
    width: initialWidth,
    height: viewport().height,
    border: ["left"],
    borderColor: THEME.detailText,
    backgroundColor: THEME.appBg,
    paddingLeft: 1,
    paddingRight: 1,
    flexDirection: "column",
    visible: false,
    zIndex: SURFACE_Z_INDEX.contextRail,
  });
  const header = new BoxRenderable(renderer, {
    id: "context-header",
    position: "absolute",
    left: 0,
    top: 0,
    right: 0,
    height: CONTEXT_HEADER_HEIGHT,
    border: ["bottom"],
    borderColor: THEME.detailText,
    backgroundColor: THEME.appBg,
    paddingLeft: 1,
    paddingRight: 1,
    flexDirection: "row",
    visible: false,
    zIndex: SURFACE_Z_INDEX.header,
  });

  function clear(target) {
    destroyChildren(target);
  }

  function renderHeader(items, rightInset) {
    clear(header);
    header.right = rightInset;
    header.borderColor = THEME.detailText;
    header.backgroundColor = THEME.appBg;
    items.forEach((item, index) => {
      if (index) {
        header.add(new TextRenderable(renderer, {
          id: `context-header-separator-${index}`,
          content: " · ",
          fg: THEME.detailText,
          wrapMode: "none",
        }));
      }
      header.add(new TextRenderable(renderer, {
        id: `context-header-${item.key}`,
        content: item.content,
        fg: THEME[item.token] ?? THEME.text,
        wrapMode: "none",
      }));
    });
  }

  function render() {
    const previousRightInset = Number(inputBox?.right ?? 0);
    const previousTop = Number(conversationBox?.top ?? 0);
    const { width: terminalWidth, height: layoutHeight } = viewport();
    const width = allowWideRail ? contextRailWidth(terminalWidth) : 0;
    // Older parents and the pre-bootstrap first frame have no canonical context.
    // Do not reserve a blank identity rail (or render invented defaults) until a
    // real context.update arrives.
    const visible = width > 0 && hasContextState(context);
    // OpenTUI's height setter updates Yoga style immediately, while the getter
    // still reports the previous computed frame until layout runs. Derive the
    // footer from the same viewport snapshot as every sibling instead of
    // reading that stale computed value during this transaction.
    const currentFooterHeight = clampFooterHeight(footerHeight, layoutHeight);
    const mainRows = Math.max(1, layoutHeight - currentFooterHeight);
    const headerItems = allowWideRail ? contextHeaderItems(context, terminalWidth, width) : [];
    const headerVisible = headerItems.length > 0 && mainRows > CONTEXT_HEADER_HEIGHT;
    const headerRows = headerVisible ? CONTEXT_HEADER_HEIGHT : 0;
    currentRightInset = visible ? width : 0;
    node.visible = visible;
    header.visible = headerVisible;
    conversationBox.right = currentRightInset;
    conversationBox.top = headerRows;
    conversationBox.height = Math.max(1, mainRows - headerRows);
    if (inputBox) inputBox.right = currentRightInset;
    renderHeader(headerVisible ? headerItems : [], currentRightInset);

    node.width = visible ? width : initialWidth;
    node.height = Math.max(1, layoutHeight);
    node.borderColor = THEME.detailText;
    node.backgroundColor = THEME.appBg;
    clear(node);
    if (!visible) {
      renderer.requestRender?.();
      return {
        geometryChanged:
          Number(inputBox?.right ?? 0) !== previousRightInset
          || Number(conversationBox?.top ?? 0) !== previousTop,
        rightInset: 0,
      };
    }
    const inner = Math.max(8, width - 4);
    // node.height is another computed Yoga value and can still describe the
    // previous frame during this callback. The current terminal-derived value
    // above is authoritative for child clipping.
    const maxRows = Math.max(1, layoutHeight);
    for (const [index, row] of contextRailRows(context, router).slice(0, maxRows).entries()) {
      if (row.kind === "spacer") {
        node.add(new TextRenderable(renderer, {
          id: `context-rail-${row.key}-${index}`,
          content: " ",
          fg: THEME.detailText,
        }));
        continue;
      }
      const content = row.kind === "field"
        ? `${row.label}  ${row.value}`
        : row.content;
      node.add(new TextRenderable(renderer, {
        id: `context-rail-${row.key}-${index}`,
        content: clipToCells(content, inner),
        fg: THEME[row.token] ?? THEME.muted,
        wrapMode: "none",
      }));
    }
    renderer.requestRender?.();
    return {
      geometryChanged:
        Number(inputBox?.right ?? 0) !== previousRightInset
        || Number(conversationBox?.top ?? 0) !== previousTop,
      rightInset: width,
    };
  }

  return {
    node,
    header,
    render,
    updateContext(message) {
      context = normalizeContextUpdate(message, context);
      return render();
    },
    updateRouter(message) {
      router = { ...router, ...(message ?? {}) };
      render();
    },
    onResize: render,
    recolor: render,
    rightInset: () => currentRightInset,
    contentWidth: () => Math.max(
      1,
      viewport().width - currentRightInset,
    ),
    agentLabel: () => contextAgentLabel(context),
  };
}
