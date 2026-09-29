// Eagerly bundle every value theme's L1 token block ([data-theme="<id>"]) into
// the entry stylesheet. This keeps picker discovery (manifests) and the applied
// CSS in lockstep: dropping a `src/themes/<id>/tokens.css` makes the theme both
// selectable AND functional, with no per-theme import to maintain.
//
// Import order is deliberately NOT load-bearing: dark's tokens.css declares the
// default ground on `:where(:root)` (specificity 0), so every theme's
// `[data-theme="<id>"]` block beats it no matter where the glob emits it.
// (Explicit-import ordering is not a usable alternative — Vite hoists eager
// glob imports above literal imports, so order pinning silently fails.)
import.meta.glob('./*/tokens.css', { eager: true })
