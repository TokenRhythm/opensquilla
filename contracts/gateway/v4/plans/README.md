# Plans Contract slice

The Plans domain keeps collaboration mode, revisions, runs, and their
legacy event names behind one adapter. `PlanCenter` is the WebUI-facing
interface; generated wire payloads and v4 method names are not imported by
Vue components or composables. This slice preserves the existing
`plans.setMode`, `plans.revise`, `plans.implement`, and `plans.cancelRun`
semantics while the backend implementation remains the source of truth.
