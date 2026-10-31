# AutoResearch workspace

Build a local, runnable research orchestration application. Never commit or print API keys.
The user's three Ruikon API credentials are authorized for this project; do not invent official provider prices.
Do not represent fixture/simulation responses as live AI research. All results must identify provenance.
Use Python 3.12, FastAPI and SQLite for the backend, React/TypeScript for the frontend.
Run CPU-sized tests by default; hardware limits, budget, and consensus are mandatory execution gates.
On Windows use cmd /c for CMD semantics, and PowerShell for native Windows operations.

## Experiment requirements

- Default to one fixed model seed per configuration and phase, with the same seed for paired baseline, proposed and ablation runs. Keep the data-split seed separate.
- Use multiple model seeds only when the user explicitly requires them in the current task. Cite that task requirement in the experiment plan; paper conventions, agent suggestions and spare or unlimited budget are not authorization for multi-seed replication.
- New plans must explicitly declare `validation_seeds` and `final_test_seeds`, each containing one seed by default. Do not turn a seed change into parameter search or claim cross-seed robustness from a single seed.
- This requirement applies to future new experiment plans. Preserve already frozen plans, dispatched inputs, completed experiments and original receipts; do not trim their approved seed coverage or rerun completed training.
- Parameter tuning must predeclare a finite single-factor grid with the control value, candidate values, bounds and rationale. Freeze one control seed, reuse exact completed configurations, and independently review one paired batch at a time. Rank candidates by the validation primary metric on that same seed; never compare a new single-seed score to an old multi-seed mean. Grid coverage alone is not stability or scientific acceptance.

## Hyperparameter configuration

- New implementation documents must require dynamically loaded YAML hyperparameters, with a versioned default YAML, a JSON Schema, and the host runtime contract. The actual model and optimizer must consume those values. Depth, width and weight decay must have CPU behavior checks; listing keys or changing an unused config is insufficient.
- Use `hyperparameters.read` and `hyperparameters.revise` for supported parameter changes. Revisions may replace/remove keys or create a complete new version; retain the original version and bind the proposed revision and SHA to the independently approved single-factor batch. YAML edits do not authorize execution or alter dataset, split, seed, metric, budget, or scientific criteria.
- Dispatch source capability and architecture changes to Codex, followed by independent code review and checks. Supported YAML-only adjustments must not dispatch Codex. Migrate existing implementations through an explicit incremental work item; never rewrite frozen documents, plans, paid requests, or completed receipts.

## Temporary test artifacts

- Prefer pytest's default OS temporary directory; never put test workspaces in a drive root or create ad-hoc temporary directories beside project source.
- When an explicit `--basetemp` is necessary, use a unique directory under `.pytest_cache/tmp/`. Keep concurrent test runs in separate directories and remove the completed run's temporary directory after retaining its test summary.
- Keep disposable databases, fixture copies and scratch files separate from source tests, real `data/`, and saved validation reports. Clean only verified temporary paths; never traverse a junction or symlink when cleaning them.

## Dataset optimization requirements

- After measured experiments, independently assess architecture before tuning parameters. Review and check necessary architecture changes, then validate the changed version.
- Advance to another dataset only after actual parameter search and independent stability review. Coverage, calibration, a stop decision, or report completion alone never establishes stability.
- The architecture-first optimization workflow has no early `stop -> report` branch. Resolve evidence gaps in the current stage; genuine blockers remain there. Neither an old stop vote nor a saved report can bypass architecture assessment, real parameter search, or stability review.
- Freeze the first dataset's confirmed model architecture for later datasets; data adaptation cannot silently revise model source. A new architecture concern requires explicit reopening.
- Preserve frozen plans, completed experiments and original receipts. Do not rerun completed training. New experiment plans use one fixed model seed unless the task explicitly requires multiple seeds; existing frozen seed lists remain unchanged.
