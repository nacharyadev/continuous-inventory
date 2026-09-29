# Continuous inventory project

- Use this directory as the canonical project folder, including artifacts.
- The editable strategy paper is `continuous-inventory-technology-strategy.md`.
- Final PDFs belong in `output/pdf/`; supporting generation scripts belong in `scripts/`.
- Keep future edits in this project rather than the original ChatGPT project mirror.
- Implementation architecture has been requested. Maintain `continuous-inventory-implementation-architecture.md` as the review draft; label proposed decisions and unresolved source-service contracts explicitly.
- Keep diagrams in architecture and strategy documents where they clarify responsibilities or flows.

## Implementation principles

- Keep workers stateless and persistence replaceable. SQLite is the only durable persistence implementation in the current scope; a future shared backend remains unselected. In-memory storage is only a test/simulation backend.
- Existing IOM and 1TS services remain authoritative for operational and financial transactions. The local domain adapter is explicitly a simulator, not a new production ledger.
- Code lives in `src/continuous_inventory`; tests in `tests`; generated reports in `output/reports`; ignored local state in `output/runtime`.
- Run `PYTHONPATH=src python3 -m unittest discover -s tests -v` for meaningful code changes. Do not claim live-model or production-integration success from fixture tests.
- Keep source architecture documents aligned with confirmed contracts and explicitly separate implemented behavior from future work.
