# Archive

Planning documents that shaped the build but no longer describe the system as it is.
They are kept so a reader can compare what was planned with what was built. For the
current system, start at the [docs hub](../README.md) or the
[architecture overview](../ARCHITECTURE.md).

Archived documents are not maintained. Numbers, file paths and line references in
them are as of the date they were written. Where an archived plan and the code
disagree, the code and the as-built docs win.

| Document | What it was | Why it is archived | As-built record |
|---|---|---|---|
| [RAG_MASTERPLAN.md](RAG_MASTERPLAN.md) | The phase-by-phase plan (R0 to R6) for hardening the policy assistant's retrieval: multi-document synthetic corpus, eval harness, hybrid dense + sparse retrieval with RRF, cross-encoder reranking, calibrated abstention, contextual retrieval, citation and NLI checks, audit completeness. Includes the binding fences, risk register and the 14-gate verification template. | All phases R0 to R6 are complete. Several planning premises were later measured to be wrong and are corrected in the as-built logs (for example, the New York query's cosine score). | [RAG architecture](../RAG/RAG_ARCHITECTURE.md), [final metrics](../RAG/metrics/FINAL_METRICS_SUMMARY.md), [RAG docs index](../RAG/README.md), `R0-*` to `R6-*` logs in [phase logs](../phase_logs/README.md) |
| [phase8_preflight_remediation_plan.md](phase8_preflight_remediation_plan.md) | The pre-deployment remediation plan (June 2026): receipt storage fix, provider wiring, Gemini vision OCR, receipt to draft expense to policy verdict, audit endpoint, correlation id, approval reason validation, embedder determinism. Ends with the verification results. | Executed before the Phase 8 staging deploy. Some figures (for example the $250/night New York cap of the retired stub policy) were superseded later; the document notes this at the top. | The plan's own verification results (end of the document); its follow-ups were picked up in [P8-T1 Docker images](../phase_logs/phase_8_p8_t1_docker_images.md) and [Phase 8 completion C1–C4](../phase_logs/phase_8_completion_c1_c4.md) (audit page, correlation id); current deploy steps in the [deployment guide](../guides/deployment.md) |

---

Atlas is an independent engineering study inspired by SAP Concur. It is not affiliated with, authorized by, sponsored by, or endorsed by SAP SE or its affiliates. SAP and SAP Concur are trademarks or registered trademarks of SAP SE in Germany and other countries. All data in this project is synthetic.
