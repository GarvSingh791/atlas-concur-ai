# README visuals

Screenshots (`screenshots/`), the demo GIF (`atlas-demo.gif`, about 73 s) and its poster frame (`atlas-demo-poster.png`, the planner frame from the GIF) were captured on 2026-11-19 from the live staging demo (https://atlas-concur-ai.vercel.app), signed in as the published demo accounts, which hold only seeded synthetic data. Staging uses Gemini through OpenRouter (`LLM_PROVIDER=openrouter`, `LLM_MODEL=google/gemini-2.5-flash`) for model calls and receipt OCR (`OCR_PROVIDER=openrouter`), as set in `render.yaml`. Nothing is edited after capture.

What is in them:

- The receipt is `sample-receipts/receipt-policy-tripping.png`, read by the vision OCR. The "Deterministic OCR" chip in screenshot 02 is the processing path (OCR fields used as returned, no agent gap-fill), not the OCR provider.
- The OCR'd hotel rate trips HOTEL-CAP in the pre-submit check; the GIF adds a justification, submits, and the manager approves that report. Screenshot 03 is the seeded "Q3 Conference & Travel" report, which carries the same flag.
- Screenshot 05 is a grounded answer to "What is the nightly hotel cap in New York?" with clause citations. The GIF then asks an out-of-scope question ("What is the company stock price today?") and the assistant abstains.
- Timestamps and the demo notice's reset time are in UTC (the browser context runs with `timezoneId: "UTC"`).

To regenerate (live):

1. Wake the free-tier backend: request `https://atlas-backend-tsf8.onrender.com/health` until it returns 200 (a cold start can take about two minutes).
2. `ATLAS_LIVE=1 ATLAS_PLAYWRIGHT=<frontend>/node_modules/playwright node capture/capture.mjs out sample-receipts/receipt-policy-tripping.png` (needs Chromium for Playwright). If a step fails, its screenshot and page text are written to `out/debug/`.
3. `python capture/make_gif.py out/frames atlas-demo.gif atlas-demo-poster.png 1200 256 1.15` (needs Pillow 9.1 or later).

Run it once per demo reset (daily, about 04:00 UTC): the uploaded receipt stays in the demo organisation until then, and uploading the same receipt again is checked as a possible duplicate.

Local mode (no `ATLAS_LIVE`) runs the same script against a local stack with the no-key local providers (`LLM_PROVIDER`, `OCR_PROVIDER`, `EMBEDDINGS_PROVIDER`, `STORAGE_PROVIDER`, `RERANK_PROVIDER` all `local`, `HYDE_ENABLED=false`) and seeded data. There OCR returns canned fields, so use the receipt `capture/make_receipt.py` draws, and the policy assistant has no model, so both policy questions abstain. Set up the stack by migrating a scratch database to head, running `database.seeds.seed_all` and the repair steps listed under `policy_corpus` in `docker/entrypoints/backend-entrypoint.sh`, then starting the backend and `next dev`.
