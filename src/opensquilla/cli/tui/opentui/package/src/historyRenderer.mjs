import { stripTerminalControls } from "./primitives.mjs";
import { destroyChildren } from "./renderableLifecycle.mjs";

function safeText(value) {
  if (typeof value === "string") return stripTerminalControls(value);
  if (value === null || value === undefined) return "";
  try { return stripTerminalControls(JSON.stringify(value)); }
  catch { return stripTerminalControls(String(value)); }
}

function itemName(item, fallback) {
  return safeText(item?.name ?? item?.filename ?? item?.path ?? item?.id ?? fallback);
}

function optionalNumber(value) {
  if (value === undefined || value === null || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : null;
}

function attachmentTail(message) {
  const names = Array.isArray(message?.attachments)
    ? message.attachments.map((item) => itemName(item, "attachment")).filter(Boolean)
    : [];
  return names.length ? `\nattachments · ${names.join(" · ")}` : "";
}

function artifactText(message) {
  const names = Array.isArray(message?.artifacts)
    ? message.artifacts.map((item) => itemName(item, "artifact")).filter(Boolean)
    : [];
  return names.length ? `artifacts · ${names.join(" · ")}` : "";
}

function toolCall(call, index) {
  if (!call || typeof call !== "object" || Array.isArray(call)) return null;
  if (call.type === "text") return null;
  const fn = call.function && typeof call.function === "object" ? call.function : {};
  const name = safeText(call.name ?? call.tool_name ?? call.toolName ?? fn.name ?? "");
  if (!name) return null;
  const args = safeText(call.input ?? call.arguments ?? fn.arguments ?? "").replace(/\s+/g, " ").trim();
  const result = safeText(call.result ?? call.output ?? call.content ?? call.error ?? "");
  const execution = call.execution_status && typeof call.execution_status === "object"
    ? String(call.execution_status.status ?? "")
    : "";
  const failed = Boolean(call.is_error ?? call.isError ?? call.error)
    || ["error", "timeout", "cancelled"].includes(execution);
  return {
    id: safeText(call.tool_use_id ?? call.toolId ?? call.id ?? `tool-${index}`),
    name,
    args,
    result,
    status: failed ? "error" : "ok",
  };
}

function usageText(message) {
  const usage = message?.usage && typeof message.usage === "object" ? message.usage : null;
  if (!usage) return "";
  const input = Number(usage.input_tokens ?? usage.inputTokens ?? 0);
  const output = Number(usage.output_tokens ?? usage.outputTokens ?? 0);
  const reasoning = Number(usage.reasoning_tokens ?? usage.reasoningTokens ?? 0);
  const cost = Number(usage.cost_usd ?? usage.costUsd ?? 0);
  const model = safeText(usage.model ?? "");
  const fields = [];
  if (input || output || reasoning) {
    let tokens = `in ${input.toLocaleString("en-US")} / out ${output.toLocaleString("en-US")}`;
    // reasoning is an output-token breakdown. Keep it inline with usage and
    // omit zero/missing values so older providers and hydrated sessions do not
    // gain a misleading "think 0" field.
    if (Number.isFinite(reasoning) && reasoning > 0) {
      tokens += ` / think ${reasoning.toLocaleString("en-US")}`;
    }
    fields.push(tokens);
  }
  if (Number.isFinite(cost) && cost > 0) fields.push(`$${cost.toFixed(6)}`);
  if (model) fields.push(model);
  return fields.join(" · ");
}

function ensembleReceipt(message) {
  const usage = message?.usage && typeof message.usage === "object" ? message.usage : null;
  if (!usage) return null;
  const rawTrace = usage.ensemble_trace ?? usage.ensembleTrace;
  if (!rawTrace || typeof rawTrace !== "object" || Array.isArray(rawTrace)
    || Object.keys(rawTrace).length === 0) return null;
  const rawBreakdown = usage.model_usage_breakdown ?? usage.modelUsageBreakdown;
  const members = new Map();

  const mergeMember = (raw, fallbackIndex, defaultRole = "member") => {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return;
    const role = safeText(raw.role || defaultRole) || defaultRole;
    const label = safeText(raw.label || role) || role;
    const provider = safeText(raw.provider);
    const model = safeText(raw.model);
    const sample = Number(raw.sample_index ?? raw.sampleIndex ?? 0) || 0;
    const identity = `${role}\u0000${label}\u0000${provider}\u0000${model}\u0000${sample}`;
    const previous = members.get(identity) ?? {};
    const error = safeText(raw.error);
    const explicitStatus = safeText(raw.status);
    const ok = raw.ok === undefined ? !error : Boolean(raw.ok);
    const index = Number(raw.index ?? raw.proposer_index ?? raw.proposerIndex ?? fallbackIndex) || 0;
    members.set(identity, {
      ...previous,
      id: previous.id || `${role}:${index}:${sample}`,
      role,
      label,
      provider,
      model,
      status: explicitStatus || (ok ? "done" : "error"),
      elapsed_ms: optionalNumber(raw.elapsed_ms ?? raw.elapsedMs ?? previous.elapsed_ms),
      input_tokens: optionalNumber(raw.input_tokens ?? raw.inputTokens ?? previous.input_tokens),
      output_tokens: optionalNumber(raw.output_tokens ?? raw.outputTokens ?? previous.output_tokens),
      cost_usd: optionalNumber(
        raw.billed_cost ?? raw.billedCost ?? raw.cost_usd ?? raw.costUsd ?? previous.cost_usd,
      ),
      error,
    });
  };

  // Trace candidates contribute lifecycle/error timing only. Candidate
  // content/text/reasoning are deliberately never copied into the block.
  const candidates = Array.isArray(rawTrace.candidates) ? rawTrace.candidates : [];
  candidates.forEach((row, index) => mergeMember(row, index, "proposer"));
  if (Array.isArray(rawBreakdown)) {
    rawBreakdown.forEach((row, index) => mergeMember(row, index));
  }
  const total = Math.max(
    0,
    Number(rawTrace.total_candidates ?? rawTrace.totalCandidates ?? candidates.length) || 0,
  );
  const requestCount = Array.isArray(rawBreakdown)
    ? rawBreakdown.reduce(
      (sum, row) => sum + Math.max(1, Number(row?.request_count ?? row?.requestCount ?? 1) || 1),
      0,
    )
    : 0;
  const fallbackUsed = Boolean(rawTrace.fallback_used ?? rawTrace.fallbackUsed);
  return {
    completed: total,
    total,
    members: [...members.values()],
    status: fallbackUsed ? "fallback" : "done",
    request_count: requestCount,
    fallback_used: fallbackUsed,
    fallback_reason: safeText(rawTrace.fallback_reason ?? rawTrace.fallbackReason),
  };
}

export function historyBoundaryText(message) {
  const count = Number(message?.loaded_count ?? message?.messages?.length ?? 0);
  const scope = String(message?.history_scope ?? "complete");
  if (scope === "compacted") {
    const summaries = Array.isArray(message?.compaction_summaries) ? message.compaction_summaries.length : 0;
    const summaryTail = summaries ? ` · ${summaries} earlier ${summaries === 1 ? "summary" : "summaries"}` : "";
    return `history · compacted · ${count} recent messages${summaryTail}`;
  }
  if (scope === "latest_window" || message?.has_more) return `history · latest ${count} messages · older messages available`;
  if (count === 0) return "";
  return `history · complete · ${count} messages`;
}

/** Clear old renderables, create a fresh flow, and synchronously replay one frame. */
export function replaceHistoryConversation({ message, conversationBox, flowFactory, addBoundary, nextId }) {
  destroyChildren(conversationBox);
  const flow = flowFactory();
  const boundary = historyBoundaryText(message);
  if (boundary) addBoundary(boundary);
  replayHistory({ messages: message?.messages, flow, nextId });
  return flow;
}

/** Replay one canonical snapshot through the same turn views used by live turns. */
export function replayHistory({ messages, flow, nextId }) {
  const seen = new Set();
  // Compatibility state for rows written before turn.identity.v2. Canonical
  // rows never depend on adjacency; their durable turn_id drives grouping.
  let legacyAdjacentTurnOpen = false;
  let identityTurnId = "";
  for (const [index, raw] of (Array.isArray(messages) ? messages : []).entries()) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) continue;
    const durableId = safeText(raw.id || `legacy-${index}`);
    if (seen.has(durableId)) continue;
    seen.add(durableId);
    const context = raw.turn_context && typeof raw.turn_context === "object"
      && !Array.isArray(raw.turn_context) ? raw.turn_context : null;
    const causalTurnId = safeText(context?.turn_id ?? context?.turnId ?? "");
    if (!causalTurnId && identityTurnId) {
      flow.endTurn(false);
      legacyAdjacentTurnOpen = false;
      identityTurnId = "";
    }
    if (causalTurnId && identityTurnId && causalTurnId !== identityTurnId) {
      flow.endTurn(false);
      legacyAdjacentTurnOpen = false;
    }
    if (causalTurnId) identityTurnId = causalTurnId;
    const id = nextId(causalTurnId || durableId);
    const blockPrefix = nextId(durableId);
    const role = String(raw.role ?? "message");

    if (role === "user") {
      // Identity-aware steer rows belong to their target Turn. Legacy rows keep
      // the adjacent user→assistant grouping used before turn.identity.v2.
      if (legacyAdjacentTurnOpen && !causalTurnId) flow.endTurn(false);
      const view = flow.turnForPrompt(id);
      view.begin(`${blockPrefix}-prompt`, "prompt", {
        text: `${safeText(raw.text)}${attachmentTail(raw)}`.trim(),
        intent: safeText(context?.intent || "send"),
        disposition: safeText(context?.disposition || ""),
      });
      legacyAdjacentTurnOpen = true;
      continue;
    }

    const view = flow.ensure(id);
    const ensemble = ensembleReceipt(raw);
    if (ensemble) {
      const blockId = `${blockPrefix}-ensemble`;
      view.begin(blockId, "ensemble", ensemble);
      view.end(blockId);
    }
    const reasoning = safeText(raw.reasoning);
    if (reasoning) {
      view.begin(`${blockPrefix}-reasoning`, "reasoning", {});
      view.append(`${blockPrefix}-reasoning`, reasoning);
      view.end(`${blockPrefix}-reasoning`);
    }

    for (const [toolIndex, rawCall] of (Array.isArray(raw.tool_calls) ? raw.tool_calls : []).entries()) {
      const call = toolCall(rawCall, toolIndex);
      if (!call) continue;
      const blockId = `${blockPrefix}-tool-${call.id}`;
      view.begin(blockId, "tool", { name: call.name, args: call.args });
      if (call.result) view.append(blockId, call.result);
      view.update(blockId, { status: call.status });
      view.end(blockId);
    }

    const text = safeText(raw.text);
    if (text) {
      const kind = role === "error" ? "error" : role === "assistant" ? "answer" : "thinking";
      const blockId = `${blockPrefix}-${kind}`;
      view.begin(blockId, kind, kind === "error" ? { text } : {});
      if (kind !== "error") view.append(blockId, text);
      view.end(blockId);
    }
    const artifacts = artifactText(raw);
    if (artifacts) {
      const blockId = `${blockPrefix}-artifacts`;
      view.begin(blockId, "history-detail", { text: artifacts });
      view.end(blockId);
    }
    const usage = usageText(raw);
    if (usage) view.begin(`${blockPrefix}-usage`, "usage", { text: usage });
    if (!causalTurnId) {
      flow.endTurn(false);
      legacyAdjacentTurnOpen = false;
    }
  }
  if (legacyAdjacentTurnOpen || identityTurnId) flow.endTurn(false);
}
