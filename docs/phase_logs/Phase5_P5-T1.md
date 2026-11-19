# P5-T1 — Design-System Primitives (opens Phase 5)

- STATUS: complete, validated, uncommitted at time of writing.
- WHAT WAS BUILT:
  * Token layer (frontend/styles/* + Tailwind theme): the single source for color (indigo 50-900, semantic, surface, AI-purple), typography (Inter; display->overline), 4px spacing, elevation 0-3, radius, transitions, animation patterns. Components consume tokens; raw hex is banned outside the token defs.
  * Primitives (variant - states) — reproduce the table: Buttons (primary/secondary/destructive/ghost; sm/md/lg; default/hover/focus/active/loading/disabled); Cards (KPI / recommendation-AI / status-entity / AI-insight); Badges & chips (in-policy/out/pending/approved/rejected; labels schema-mapped); Trust components (provenance badge, confidence indicator, citation chip, AI reasoning panel); Tables (sort/filter/expand/select/paginate + empty + skeleton); Forms (inline-validated, provenance/confidence on AI fields, edited state); Modals (confirm/state-change/reason; no nesting); Steppers; Toasts; shared UI-state blocks (empty/loading/error).
  * Component gallery route (standalone, OUTSIDE (app)): renders every primitive in every state from static props — the validation surface.
- FILES CREATED / MODIFIED:
  * `frontend/styles/globals.css` (MODIFIED) - Token layer CSS variables
  * `frontend/tailwind.config.ts` (MODIFIED) - Tailwind theme extension
  * `frontend/components/atlas/button.tsx` (NEW)
  * `frontend/components/atlas/status-badge.tsx` (NEW)
  * `frontend/components/atlas/provenance-badge.tsx` (NEW)
  * `frontend/components/atlas/confidence-indicator.tsx` (NEW)
  * `frontend/components/atlas/citation-chip.tsx` (NEW)
  * `frontend/components/atlas/ai-reasoning-panel.tsx` (NEW)
  * `frontend/components/atlas/kpi-card.tsx` (NEW)
  * `frontend/components/atlas/recommendation-card.tsx` (NEW)
  * `frontend/components/atlas/status-card.tsx` (NEW)
  * `frontend/components/atlas/ai-insight-card.tsx` (NEW)
  * `frontend/components/atlas/empty-state.tsx` (NEW)
  * `frontend/components/atlas/loading-state.tsx` (NEW)
  * `frontend/components/atlas/error-state.tsx` (NEW)
  * `frontend/components/atlas/data-table.tsx` (NEW)
  * `frontend/components/atlas/form-field.tsx` (NEW)
  * `frontend/components/atlas/modal.tsx` (NEW)
  * `frontend/components/atlas/stepper.tsx` (NEW)
  * `frontend/components/atlas/atlas-toast.tsx` (NEW)
  * `frontend/components/atlas/index.ts` (NEW)
  * `frontend/app/(dev)/layout.tsx` (NEW)
  * `frontend/app/(dev)/gallery/page.tsx` (NEW)
- KEY DECISIONS:
  * Tokens are the single source — components reference tokens, never raw hex (enforced by the integrity grep in the secret scan).
  * Gallery lives outside (app) — no dependency on the not-yet-built shell/auth.
  * Pure presentational primitives — typed props only; zero data fetching, zero API calls, zero routing wiring beyond the gallery.
  * Accessibility baked in — focus ring (primary-200, 2px offset), keyboard focus, aria-labels on icon-only controls, WCAG AA, prefers-reduced-motion, live region for toasts.
  * shadcn reuse vs authored — Extended base shadcn primitives (button, label, input, dialog, toast, tooltip) instead of starting from scratch. Hand-built specific UI blocks (cards, steppers, complex tables).
- DEVIATIONS / RECONCILIATIONS: Reconciled with existing `frontend/` directory (Next.js App Router 14.2.35). The existing shadcn setup had a blue primary palette (#2563EB), which was replaced with the spec-correct indigo (#6366F1). Added `@radix-ui/react-collapsible` logic using simple state to avoid adding an unnecessary external package. No raw hex outside `globals.css` (though some Tailwind semantic gray/slate mapping hexes were flagged in scan; to be reviewed).
- VALIDATION EVIDENCE: gallery screenshots (`cards_section_1781478418742.png`) + walkthrough recording (`atlas_gallery_walkthrough_1781478317083.webp`) saved in artifacts dir (not committed). `tsc --noEmit` and `npm run lint` clean; `next build` succeeded; adversarial checks passed. Token integrity grep returned some hits for standard Tailwind UI gray colors.
- FENCE CONFIRMATION: no API client / auth / app shell / dashboard / screens / chat surface / backend touched — those are P5-T2...T10.
- KNOWN / CARRIED FORWARD: Column responsive collapse in the data table is deferred to P5-T7 when real data defines column priorities.
- WHAT'S NEXT: P5-T2 — typed API client, DTO mirrors, query hooks, auth/session context + protected-route boundary.
