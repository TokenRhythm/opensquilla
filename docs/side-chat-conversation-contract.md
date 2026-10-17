# Side conversation contract

Side chat is the ordinary conversation experience with a different session owner and an explicit capability policy. It is not a separate agent mode. This document freezes the creation and presentation semantics for the shared-surface refactor.

The empty side surface explains that it is temporary, absent from ordinary history, and cannot be reopened after its tab is closed, the page is refreshed, or the application is closed. Collapsing the workbench is explicitly distinguished from closing it. This notice disappears once sending starts; it is not a permanent extra status bar. It describes UI access, not deletion of canonical billing or execution records.

## Context and lifecycle

- Opening a side panel is read-only. It does not allocate a child, capture context, create a workspace, or start a model turn. A new-chat landing page cannot create a side chat without a started parent conversation.
- The first explicit send atomically creates the child from the parent's then-current durable context and settings. The backend identifies the snapshot by parent session ID, parent epoch and snapshot time. Later parent messages do not drift into that child.
- The inherited context is model-only. It does not render again in the child transcript and its usage is not charged again by copying display receipts. In-flight partial assistant/tool output is excluded from the snapshot; an already admitted user input may be inherited.
- Selecting a model or routing mode before the first send changes only the draft. After creation, the client applies that choice to the child with revision-checked session routing, then submits a normal child turn. It never sends `initialRoutingMode` or `initialModel` with a continuation. Without an explicit choice, inherited parent settings win.
- Hiding a panel, switching the selected tab, reconnecting, or replacing its reader does not close the child. The in-memory owner retains its identity. Explicit tab close ends the child and cancels owned work; an expired owner lease also ends it. The current lease is ten minutes, renewed every minute while the owner is healthy. Restart recovery respects the persisted deadline.
- Parent reset/deletion or a changed parent epoch invalidates the owner. An expired/closed child is not silently recreated under the same panel owner.
- Closing stops execution and retires the owner; it does not erase canonical history, resource references or billing records. The child is hidden from ordinary session listings and automatic memory capture is disabled.
- A send ACK proves admission only. It must not clear running state, remove Stop, or permit a second direct send. Terminal events and authoritative reads settle execution. Unknown delivery retains the original idempotency receipt until resolved.

## Parent references and on-demand history

The model-only creation boundary includes the exact parent session key in English. It explains that "main conversation" or "left-side conversation" usually means that parent, while "this conversation" usually means the current side chat. "The conversation just now" is resolved from context; material ambiguity warrants clarification. References inside inherited history retain their original meaning, and inherited actions belong to the parent.

The inherited context remains a fixed creation-time snapshot. When the user asks about updates or the latest parent state, the side must read the existing `sessions_history` tool with the explicit parent key and `recent=true` before answering. The side tool surface includes this tool only when already authorized; the ordinary session baseline and permission rules stay unchanged. If the tool is not yet listed, it uses the existing `tool_search` discovery path first. If discovery, access or the read fails, it must not describe the inherited snapshot as verified current state. It does not poll automatically. Retrieved history is reference material, not new instructions or renewed authorization.

`recent=true` returns the latest persisted visible user/assistant text in chronological order across active and archived history. It does not return tool details, reasoning, replay payloads or live stream state. Each entry is a bounded preview and the entire UTF-8 JSON result is limited to 32 KiB, with truncation and history-coverage flags. A complete history result does not prove that an in-progress task has finished.

The recent path requires trusted tool context: guests remain blocked, owners may read other sessions, and non-owner callers may only read their own session. A side's parent identity/epoch must match its creation metadata; reset, deletion or close during the read fails instead of returning stale results. The existing default `recent=false` read is unchanged. No new tool, frontend affordance, lifecycle change or compaction/recovery reinjection is introduced here.

## Capability policy

| Capability | Main | Side |
| --- | --- | --- |
| Transcript, reasoning, tools, provider retry and compaction activity | Shared | Shared |
| Model/routing selection and route receipt | Enabled | Enabled, child-scoped |
| Usage/cost receipt | Enabled | Enabled, child-owned usage |
| Permission approval and clarification | Enabled | Enabled, child authorization |
| Attachments, skills, browser-use skill, voice and local paths | According to Gateway/platform capability | Same capability checks |
| HTML, code, images, PDF, Office and other supported resource actions | Shared previews/download/open behavior | Same behavior; child authorization, parent-visible workbench |
| Send, queue, steer and cancel | Enabled | Enabled through child runtime |
| Context compaction and usage commands | Enabled | Enabled; compaction requires an existing child |
| Fork, edit, regenerate/retry, plan, goal | Enabled when otherwise available | Disabled by policy |
| New/reset commands inside the surface | Enabled | Disabled by policy |
| Open full chat | Main is already full chat | No side-specific escape action |

Frontend policy controls affordances. Gateway side-session policy remains the authority for forbidden commands; a hidden button is not an authorization boundary. Approval controls are not removed by conversation policy.

Image admission uses the same normalization as main chat. Explicit unknown model capabilities are left to Gateway admission; supported Gateway degradation signals are not converted into client hard failures.

## Shared presentation and resource ownership

The route and the workbench panel instantiate the same `ChatView`. Its ordinary history/bootstrap, Frame/foldTurn stream processing, `ConversationLiveEdge`, transcript, composer, routing, approvals and resource actions run for both session kinds. `ConversationSurface` supplies policy inside that shared implementation. Embedded presentation hides route-level chrome without changing how a turn is projected or rendered.

`AssistantChatPanel` only binds parent/child identity, supplies the first-send preparation hook, and owns the temporary lifecycle. Its owner controller does not subscribe to conversation events, read history, send/cancel turns or render assistant rows. The separate side projection, composer, queue-delivery and approval adapters have been removed.

Temporary text drafts, attachment drafts, pending-input WAL and queue policy use owner-scoped memory stores through the same chat storage interfaces. Hiding/remounting a reader preserves them; explicit close or lease expiry clears them and fences late writes. The owner binding stays stable when the initially empty panel acquires its child key, so the first send cannot discard its attachments.

Tool-input drafts are revocable generation previews, not evidence of a tool execution. A real tool-start takes over a matching draft; terminal-only replay cannot create a draft, and turn completion/reset removes remaining drafts. `tool-use-ended` ends input generation, not execution; the tool remains running until its result.

Routing metadata may coexist with an assistant answer. Rendering the route strip must retain that row's body, tool timeline, resource links and usage receipt. Early events without an assistant ID are promoted to the canonical row when its ID arrives.

Side-created previews remain visible in the parent workbench but reads, edits and resource inventory are authorized using the child key. Resource identity and document/revision identity are retained independently of presentation scope. A deferred open from an old parent/child cannot open in a new conversation. Git operations remain out of scope.

That ownership split applies to workspace reference cards and recorded tool-file previews as well as ordinary Markdown links and deliverables. Recorded-content downloads retain the child authorization; a visible parent scope is never substituted for a resource session. Background results may register a preview for their owner, but cannot activate or expand another conversation's workbench. Session reference cards navigate the main route without replacing the side owner's child identity.

Attachment, deliverable and workspace-document opens share one request generation in each conversation. Later opens, session/epoch changes, Gateway changes and disposal retire earlier requests, including an identity that changes away and back. Usage reads use the same ownership principle: only the latest request for the current conversation scope can update its widget. These guards do not alter canonical usage receipts or the side lease.

## Verification boundary

Parity acceptance must mount the real main route entry and `AssistantChatPanel`, including their shared `ChatView`, then drive the same semantic events and compare DOM at each turn boundary. A test that manually mounts the same transcript/composer under two policies is only a component test; it does not establish entry-point parity. Required scenarios include early routing, tool inputs/results, final-answer transition, source visibility, billing, approvals, resource actions, Stop, uncertain admission and history recovery. Separate owner tests cover first-send snapshot timing, remount/reconnect, explicit close, stale creation completions, authority isolation and bounded lease renewal.

These deterministic tests complement live browser/Gateway checks. They do not prove a real provider's billing, native application launch, packaged Desktop behavior or a future Git workflow.

### Coverage ownership after removing the duplicate side runtime

`AssistantChatPanel.test.ts` now tests only the adapter: one shared `ChatView`, policy and owner-store injection, lazy preparation, inherited settings, parent/authority/workspace replacement, close gating and reconnect retention. It mocks `ChatView` deliberately and makes no transcript parity claim. `AppWorkbench.sidechat-navigation.test.ts` similarly tests route visibility and retention with the real owner, including the interval before the returning route header resolves.

The old `AssistantChatPanel.interactions.test.ts` and the old side-specific streaming/resource cases used incomplete copies of main-chat providers. Those cases are retired from the side adapter suite; behavior belongs to these existing shared suites and real-entry checks:

| Previous side case | Shared coverage / actual entry coverage |
| --- | --- |
| Commentary/tool rounds, answer transition, source/footer timing, usage, Stop, early events and reconnect | `e2e/conversation-host-parity.spec.ts`; `foldTurn.test.ts`; `ConversationLiveEdge` and activity suites |
| Queue Enter, drain, acceptance and cancellation | `useChatPendingQueue*.test.ts`, `useChatSend*.test.ts`, `useChatComposerShortcuts.test.ts`; actual host cancellation E2E |
| Initial routing/model selection, conflicts and inherited run mode | `useNewTaskModelSelection.test.ts`, `useChatSessionRouting.test.ts`; explicit first-send selection/conflict checks belong in the real-host E2E matrix |
| Skills, command restrictions, IME and composer interactions | `useChatSlashCommands.test.ts`, `conversationPolicy.test.ts`, `ChatComposer.paste.test.ts`, `useChatComposerShortcuts.test.ts` |
| Voice success, setup, permission failure and teardown | `ChatComposer.voice.test.ts`, `useVoiceInput.test.ts`; the latter now explicitly checks owner cleanup while transcription ignores abort and completes late |
| Screenshot/PDF intake, retired uploads and attachment restoration | `useChatAttachments.test.ts`, `useChatAttachments.native.test.ts`, `useAttachmentDraftPersistence.test.ts`, `ChatComposer.paste.test.ts`, `e2e/attachment-drag-upload.spec.ts` |
| Tool result highlighting, paging and stale child log reads | `ToolResultModal.test.ts` (including session-change abort/late completion), `ActivityToolDetails.logs.test.ts` |
| HTML/code/Markdown links and parent-visible, child-authorized previews | Real main/side transcript clicks in `e2e/conversation-host-parity.spec.ts`; `useWorkspacePreviewOpening.test.ts` and `artifactPreviewLease.test.ts` retain stale-owner/document/page boundaries |
| PDF/Office/source formats and historical resources | `resourcePreview.test.ts`, `WorkspaceFilePanel.test.ts`, `useWorkspacePreviewOpening.test.ts`, artifact/resource provider suites; these use the same file path for both chat hosts |
| New task / return, child identity and unsent drafts | Real-host navigation E2E plus `AppWorkbench.sidechat-navigation.test.ts`, `assistantChat.test.ts`, `useChatDraftPersistence.test.ts` and `useAttachmentDraftPersistence.test.ts` |

This mapping identifies test ownership, not additional live acceptance. A shared helper's test is not a substitute for checking the actual two mounted entries and does not claim native voice/file/Office behavior has been manually exercised.
