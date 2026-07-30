import assert from "node:assert/strict";
import test from "node:test";

import { attachmentChipText, attachmentSubmitBlocked } from "./composer.mjs";
import { createDispatcher } from "./ipc.mjs";

test("attachment chips expose each lifecycle state without paths or bytes", () => {
  assert.equal(
    attachmentChipText({ kind: "file", label: "brief.pdf", status: "reading" }),
    "[◌ file brief.pdf]",
  );
  assert.equal(
    attachmentChipText({ kind: "file", label: "brief.pdf", status: "uploading" }),
    "[⇡ file brief.pdf]",
  );
  assert.equal(
    attachmentChipText({ kind: "image", label: "chart.png", status: "ready" }),
    "[✓ image chart.png]",
  );
  assert.equal(
    attachmentChipText({
      kind: "path",
      label: "report.md",
      status: "failed",
      message: "check the file and retry /path",
    }),
    "[✗ path report.md · check the file and retry /path]",
  );
});

test("only reading and uploading attachments block submit", () => {
  assert.equal(attachmentSubmitBlocked([{ status: "reading" }]), true);
  assert.equal(attachmentSubmitBlocked([{ status: "uploading" }]), true);
  assert.equal(attachmentSubmitBlocked([{ status: "ready" }]), false);
  assert.equal(attachmentSubmitBlocked([{ status: "failed" }]), false);
});

test("ipc dispatcher routes additive attachment state messages", () => {
  const seen = [];
  const dispatch = createDispatcher({
    attachmentAdd: (m) => seen.push(["add", m.id]),
    attachmentUpdate: (m) => seen.push(["update", m.status]),
    attachmentRemove: (m) => seen.push(["remove", m.id]),
    attachmentClear: (m) => seen.push(["clear", m.status]),
  });

  dispatch({ type: "attachment.add", id: "a1" });
  dispatch({ type: "attachment.update", id: "a1", status: "ready" });
  dispatch({ type: "attachment.remove", id: "a1" });
  dispatch({ type: "attachment.clear", status: "ready" });

  assert.deepEqual(seen, [
    ["add", "a1"],
    ["update", "ready"],
    ["remove", "a1"],
    ["clear", "ready"],
  ]);
});
