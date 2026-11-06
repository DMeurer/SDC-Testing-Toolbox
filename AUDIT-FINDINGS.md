# Audit Findings Implementation Plan

> Agentic workers: use `subagent-driven-development` for the isolated batches below; use `executing-plans` for the coordinator's small fixes. Record regression failures before implementation, then passing checks and review evidence.

**Completed:** 2026-11-06 — all **27 findings fixed and Verified** within the checks and limits below. Final review corrections and fresh post-fix verification are complete; no original finding is parked or deferred.

**Publication follow-up:** 2026-11-06 — the user authorized a commit and normal push on `develop`. Earlier no-publication rulings below record the development phase; no PR or binary release is requested.

**Goal:** Fix the repository audit findings without changing the toolbox's intended small-lab scope.

**Architecture:** Keep the existing provider, consumer, Qt GUI, and direct-script tests. Fix each fault in its current owner, reuse existing validation and lifecycle machinery, and add narrow regression tests. Larger batches use separate worktrees and exclusive file ownership; the coordinator authorizes and verifies integration and alone edits this ledger.

**Tech stack:** Python 3.12 (CI target), Python 3.13.17 (available local interpreter), sdc11073 3.0.0, PySide6 6.11.2, cryptography 50.0.0, standard-library test tooling.

**Spec:** The numbered findings and acceptance cases in this file preserve the audit report. Findings 21–27 are the seven lower-priority items omitted from its 20-item summary.

**Started:** 2026-11-06. **Baseline:** `56c5a1bf65d5fb2a1af6de45fdef7cada3ada92c`; clean working tree; all 20 existing deterministic/policy suites passed during the audit.

## Global constraints

- No new dependencies, broad rewrites, speculative options, or changes to the documented lab-only limitations.
- Preserve profile versions 1–3, public provider/consumer APIs, error handling, and compensating import transactions.
- Strict TLS remains TLS 1.2+, explicit trust, hostname checks, and client authentication, with no HTTP fallback. Existing explicit lab relaxations remain opt-in.
- Validate user values before writes. Do not silently truncate numbers or store XML-invalid strings.
- Preserve active user drafts, but never display missing or different readings as current values.
- During development, subagents edit only their assigned worktree and owned files. After review approval, the coordinator may authorize mechanical application of that exact owned patch to the original checkout; no behavioral edits or shared-file changes are allowed in that handoff. No commits, merges, pushes, or publication are requested.
- Use the original checkout's existing interpreter; do not install packages into its shared virtual environment.
- Serialize tests that start providers/discovery using the shared Windows mutex `Local\SDCToolboxAuditNetworkTests`; parallel code editing remains isolated. The coordinator runs the final combined suite.
- Status `Verified` requires integration, a passing regression, relevant suite checks, and review. An implementation report alone is not completion.

## Status

`Open` → `In progress` → `Implemented` → `Verified`. Use `Blocked` only with a concrete reason and next action.

| ID | Finding | Owner | Status | Regression / verification evidence |
|---|---|---|---|---|
| 1 | Certificate pins are not checked after reconnect. | TLS | Verified | Production SDK callback-pool regressions RED 2/2 → GREEN 2/2; synchronous/reconnect and async zero-payload rejection pass in the 34-check TLS suite. Integrated review and both post-fix 28-suite runs pass. |
| 2 | An unfinished TLS connection blocks the listener and shutdown. | TLS | Verified | Raw pre-hook handshake, second-client, bounded-stop, and local setup/shutdown-order regressions pass. Narrow EOF handling retains unrelated-error negative controls; TLS/lifecycle suites and review pass. |
| 3 | A failed profile export destroys the previous file. | Provider | Verified | `tests/config_roundtrip.py` injects ENOSPC/replace failures and checks prior bytes and temporary cleanup. Final config suite passes 229 checks on both interpreters; review approved. |
| 4 | Participant names overwrite generated certificate/key files. | TLS | Verified | Reserved, cross-file, initial/add-batch and case collisions reject before writes; valid -key-key identities remain supported. All 25 helper checks and re-review pass. |
| 5 | The local table editor can overwrite a newer value. | GUI | Verified | `tests/gui_audit_regressions.py` covers unfocused refresh, focused drafts, and open dropdowns. All 23 methods pass on both interpreters; final broad GUI smoke passes 339 checks. |
| 6 | Losing only the measurement feed leaves a frozen connected display. | TLS | Verified | Real SubscriptionEnd, periodic alternative, initial rejection and already-lost pane attachment regressions pass; operation-subscription/connection-loss suites and integrated review pass. |
| 7 | Cards retain or invent current values. | GUI | Verified | None and unmatched-current choices stay truthful without placeholder writes or spurious requests. GUI regression/widget suites pass in both complete selections; reviewed and integrated. |
| 8 | XML-invalid text actions poison the running device model. | Provider | Verified | Scalar/action/profile and mutable-spec preflight regressions preserve values, versions and readable MDIBs. Final provider-core 298 and config 229 checks pass on both interpreters; review approved. |
| 9 | Accepted decimals silently change on the wire. | Provider | Verified | Shared lossless guards and preflight cover scalar/spec/sample values, derived distribution steps, mutable alarm limits and generated sample ranges. Final 8-case/16-check derived-range matrix preserves exact snapshots; both complete runs and scoped re-review pass. |
| 10 | Remote writes ignore the operation's full allowed range. | Provider | Verified | Registered-handler tests cover live range unions, open bounds, step alignment and large aligned values with unchanged state on rejection. Provider-core 298 checks and both complete runs pass; review approved. |
| 11 | Synchronous cleanup can freeze disconnect/window close. | GUI | Verified | Event-gated disconnect/reconnect/shutdown and failed-handoff cases retain exactly-once off-thread ownership and retryability. Consumer lifecycle/GUI regressions, broad smoke and scoped reviews pass. |
| 12 | Workers report completion before cleanup ends and emit after close. | GUI | Verified | Tracked retirement and real bridge close/emission suppression cover all 9 families, 18 acquired callbacks and in-flight Qt deletion. RED→GREEN on both interpreters; final GUI regressions 23 methods, complete runs and scoped re-review pass. |
| 13 | Duplicate alarm labels cannot round-trip through profiles. | Provider | Verified | Duplicate labels/pending signal allocation, round-trip and connected-consumer compensation regressions pass in the final 229-check config suite on both interpreters; review approved. |
| 14 | An out-of-range console write stops the provider. | Coordinator | Verified | Console regression RED→GREEN; all 4 command-loop tests pass on both interpreters. A real Python 3.12 session rejects 80, accepts following 9 and exits normally. |
| 15 | Console conversion changes numeric-looking text. | Coordinator | Verified | Both console loops preserve text/choice `001` while numeric strings still convert at the service boundary. All 4 console tests, both complete runs and review pass. |
| 16 | Linux legal generation produces notices its validator rejects. | Build/tests | Verified | Maintained fixtures accept complete Debian-referenced common notices and reject missing/unrelated notices or bad paths/hashes. Licensing and review pass; native Linux generation remains unrun. |
| 17 | Ubuntu instructions use unsupported default Python 3.10. | Coordinator | Verified | Reviewed README requires Python 3.12 with venv support and invokes `python3.12`, without claiming Ubuntu's default 3.10 works. Actual Python 3.12 checks pass; native Ubuntu setup remains unrun. |
| 18 | Exited test parents leave children and stdout pipes alive after timeout. | Build/tests | Verified | Real native tests cover exited leaders, inherited pipes, delayed readiness, interrupted partial creation, retry/cleanup and exact stable handle counts. Infrastructure passes 59 checks on both interpreters; scoped review approves. |
| 19 | PDF text cleaning drops all but one code row per page. | Coordinator | Verified | Row separation, cross-line false joins and blank-page diagnostics each demonstrated RED→GREEN. All 3 extraction tests pass on both interpreters and review; real PDF layout quality is not claimed. |
| 20 | Three declared network suites never run in CI. | Build/tests | Verified | Isolated Linux jobs select passive-discovery, operation-subscription and connection-loss; workflow-selection policy and all 5 network suites pass locally in both complete runs. Hosted Actions/native Linux execution remains unrun. |
| 21 | Export loses an explicitly detached patient. | Provider | Verified | Detached exports clear fresh associated providers; deliberately omitted patient instructions leave context unchanged across versions 1–3. Final config 229 checks and review pass. |
| 22 | A local advertised DNS name is used to authenticate the remote peer. | TLS | Verified | Distinct alpha.lab/beta.lab verification regression and both complete selections pass; CLI help describes local advertisement. Reviewed production callback wiring preserves this distinction. |
| 23 | Adding an identity rewrites older commands with the new IP. | TLS | Verified | Commands use each certificate's own SAN address; add-at-different-IP and valid -key-key cases preserve prior PEM bytes. All 25 helper checks, both complete selections and re-review pass. |
| 24 | Remote sample plots discard their advertised vertical range. | GUI | Verified | Waveform/distribution snapshot regressions retain 0–100 technical plot bounds while scalar write domains remain separate. Final GUI regression/widget suites and review pass. |
| 25 | Scope-only discovery updates leave stale location details. | GUI | Verified | Same-EPR OR1→OR2 regression refreshes stored scope/tooltip without consuming connect-next or losing selection. Final GUI regressions and both complete runs pass; review approved. |
| 26 | Qt source inventory omits bundled image-format module sources. | Build/tests | Verified | Conditional generation/validation tests cover required, absent and wrong-version qtimageformats sources. Fresh Windows build and final ZIP validation pass all 99 artifact checks; review approved. |
| 27 | The PDF helper retains an unused row-normalization chain. | Coordinator | Verified | Whole-tree reference checks preceded removal of dead normalization/header code and inlining of the remaining validated conversion. All 3 extraction tests, both complete runs and review pass. |

## Review focus

1. TLS rejection must happen on every actual connection, before metadata, subscription, or control bytes are sent; callbacks must keep their transport policy.
2. Timeout and cleanup tests must cover already-exited parents, slow closers, late Qt destruction, and failed thread starts without leaking resources.
3. Profile recovery must preserve prior files and live graph state, including empty patient contexts and duplicate generated names.
4. GUI refresh must distinguish a current value, no current value, and an active draft; writable choices are not a substitute for the current reading.
5. Numeric/XML fixes must reject bad values before writes across local setters, remote handlers, action effects, and sample publication without losing valid precision.

## Execution and ownership

All worktrees start at the baseline above. Shared interfaces may be read, but only the listed owner writes each file. A new helper/test file is allowed only inside that owner's subsystem; report its exact path before integration.

| Task | Worktree | Exclusive production ownership | Exclusive test/config ownership |
|---|---|---|---|
| 1 TLS/consumer health | `C:\Users\meurdode\.local\share\opencode\worktree\fb5564\audit-tls` | `sdctoolbox/security.py`, `sdctoolbox/http_server.py`, `sdctoolbox/consumer_service.py`, `helpers/tls/generate_certificates.py` | `tests/security.py`, `tests/tls_helper.py`, `tests/tls_acceptance.py`, `tests/service_lifecycle.py`, `tests/operation_subscription.py`; new TLS-specific tests |
| 2 GUI/worker lifecycle | `C:\Users\meurdode\.local\share\opencode\worktree\fb5564\audit-gui` | `sdctoolbox/gui/async_call.py`, `provider_pane.py`, `consumer_pane.py`, `widgets/base.py`, `widgets/controls.py`, `widgets/plot.py` | `tests/consumer_lifecycle.py`, `tests/widget_controls.py`, `tests/gui_cards_plots.py`, `tests/gui_provider_structure.py`, `tests/gui_layout.py`; new GUI-specific tests |
| 3 Provider/profiles | `C:\Users\meurdode\.local\share\opencode\worktree\fb5564\audit-provider` | `sdctoolbox/model.py`, `config.py`, `handlers.py`, `provider_service.py`, `sample_generation.py` | `tests/provider_core.py`, `tests/config_roundtrip.py`; new provider-specific tests |
| 4 Build/test execution | `C:\Users\meurdode\.local\share\opencode\worktree\fb5564\audit-build-tests` | `legal_payload.py`, `tests/run_suites.py`, `tests/script_support.py` | `tests/licensing.py`, `tests/test_infrastructure.py`, `tests/suites.json`, `.github/workflows/build-app.yml` |
| 5 Small fixes/integration | Original checkout | `examples/console.py`, `helpers/Coding Systems/export_11073_tables.py`, `README.md`, `.gitignore`, this ledger | New `tests/console_commands.py`, `tests/coding_export.py`; only coordinator edits shared files after batch integration |

### Task 1: TLS connections, certificate generation, and consumer feed health

**Findings:** 1, 2, 4, 6, 22, 23. **Interfaces:** Preserve `TlsConfig.from_paths`, `TlsConfig.create_contexts`, `ConsumerService.connect`, `RemoteDevice` health callbacks, and the request-handler hook. Do not edit provider/model/GUI or manifest files; report any required caller change to the coordinator.

- [x] Add failing regressions for all six acceptance cases, including pins on new SOAP transports/reconnects, an incomplete handshake with a second valid client, and partial feed loss with a surviving periodic alternative.
- [x] Run those regressions on baseline and record the expected failures.
- [x] Implement the smallest local fixes. Use per-instance transport/server hooks, not global monkeypatches; do not weaken TLS or relax invalid output-name checks.
- [x] Run TLS/helper/lifecycle focused suites and relevant acceptance checks where supported. Serialize live provider/discovery suites with the shared mutex.
- [x] Self-review ownership, security boundaries, shutdown paths, and cleanup. Write the task report and leave changes uncommitted in this worktree for coordinator review/integration.

### Task 2: Truthful GUI values and tracked nonblocking cleanup

**Findings:** 5, 7, 11, 12, 24, 25. **Interfaces:** Preserve keyed managed calls, single-close resource ownership, cancellation, stale-generation guards, and consumer APIs. Read but do not edit `consumer_service.py`; Task 1 owns it. Public widget requests retain `Decimal | str` values and original handles.

- [x] Add failing regressions for unfocused versus focused provider edits, cleared/unmatched card values, blocked cleanup, post-close emissions, remote sample bounds, and location-only updates.
- [x] Run the new cases against baseline and record the expected failures; use real Qt objects with controlled external transport where needed.
- [x] Update existing refresh and worker paths without a new lifecycle framework. Keep closers tracked off the GUI thread until cleanup completes and recheck shutdown before emission.
- [x] Run consumer lifecycle, widget, card/plot, layout, and provider-structure suites offscreen. Serialize tests starting real providers/discovery.
- [x] Self-review failure cleanup, single-close guarantees, thread-start failure, and active-draft preservation; report changed paths and tests, leaving worktree changes uncommitted.

### Task 3: Safe profile files and provider write validation

**Findings:** 3, 8, 9, 10, 13, 21. **Interfaces:** Preserve `save(...) -> Path`, versioned profile parsing, local/remote value types, transactional import/compensation, generated naming, and context omission semantics. An explicit empty patient export must remain distinct from an omitted patient instruction.

- [x] Add failing tests for partial save failure/temporary-file cleanup, invalid XML in scalar/action writes, lossy decimal rejection, operation step/range checks, duplicate alarm round-trip, and detached-patient export.
- [x] Run regressions against baseline and record the expected failures, including unchanged prior state/file after each rejected input.
- [x] Use same-directory temporary output plus atomic replacement; reuse existing XML/range/decimal checks, preserving all valid values and import rollback behavior.
- [x] Run provider-core and config-roundtrip suites, serialized with the shared mutex. Check numeric/sample boundaries, consumer-observed compensation, and failure cleanup.
- [x] Self-review callers and mutable specs, report changed files and exact regression evidence, and leave changes uncommitted for integration.

### Task 4: Packaging evidence, bounded test processes, and CI coverage

**Findings:** 16, 18, 20, 26. **Interfaces:** Keep `validate_payload` independent from untrusted inventory claims, manifest/direct-script exit codes and output, per-suite timeouts, process-tree cleanup, and existing native build targets. Own all manifest/CI edits while running; the coordinator registers new cross-batch suites only after this task releases ownership, so no worktree manifest references files it does not yet contain.

- [x] Add failing tests for referenced Debian common notices, required Qt image-format source archives, already-exited parents with surviving children/output pipes, and selection of all intended CI network suites.
- [x] Run the failing cases on baseline. All spawned children must have bounded cleanup even if a test assertion fails.
- [x] Fix notice/module derivation without weakening path/hash checks. Own descendants independently of parent exit using native OS process grouping/Windows Job Objects as needed, and bound final pipe draining without new dependencies.
- [x] Run licensing, infrastructure, and workflow policy suites. Add isolated Linux network jobs with Qt runtime/offscreen support where required; preserve existing job isolation and failure logs.
- [x] Self-review native resource handles, interrupted/spawn-failure paths, and manifest compatibility; report required new test registrations from other tasks and leave changes uncommitted.

### Task 5: Small console, documentation, and PDF-helper fixes; integrate and verify

**Findings:** 14, 15, 17, 19, 27. **Interfaces:** Console services already accept raw numeric strings; PDF extraction cleans individual lines; helper PDF dependencies remain optional to the application. No source changes outside coordinator ownership until an owner finishes its batch.

- [x] Add and run failing console regressions for rejection without shutdown and preservation of `001` in provider/consumer commands.
- [x] Remove the redundant console conversions and handle expected set validation failures; run the new script.
- [x] Add and run a two-line PDF text regression without requiring optional PDF packages in the application environment; preserve page line breaks and run it green.
- [x] Recheck whole-tree dynamic/string references, then delete only the unused normalization chain; rerun the extraction test.
- [x] Correct Linux Python prerequisites/invocations to the tested Python 3.12 target; do not introduce a Python installer dependency or unsupported package command.
- [x] After Task 4 releases ownership, register new console/PDF and other batch regression scripts in the combined manifest on both supported platforms where applicable.
- [x] Review each completed batch against its requirements and diff, integrate only owned paths, and address review defects before marking findings verified.
- [x] Run the combined manifest suites (including platform-excluded deterministic cases explicitly), applicable network regressions, and packaging/legal checks where the environment permits. Record unrun platform checks honestly.
- [x] Complete an independent final review, resolve actionable findings, update every status/evidence row, and leave the final changes ready for user review without committing or publishing.

## Pre-flight conflict scan and rulings

| Tasks | Shared file/interface | Check and ruling |
|---|---|---|
| 1 / 2 | `RemoteDevice`, consumer health callbacks | Task 1 alone edits `consumer_service.py`; Task 2 uses unchanged public callbacks and edits only GUI consumers. Integrate and rerun lifecycle tests together. |
| 1 / 3 | Numeric/model types and provider request-handler hook | Task 3 owns provider/model. Task 1 must preserve public signatures and report caller changes; coordinator applies any provider hook change only after Task 3 finishes. |
| 2 / 3 | Provider/model APIs, remote snapshot fields | Signatures and snapshot fields stay compatible; GUI edits do not change provider semantics. Combined widget/provider suites verify the boundary. |
| 4 / all | Test registration and CI | Only Task 4 edits suites/CI while agents run. Other tasks report new test paths; coordinator reconciles registration after integration. |
| 5 / all | Ledger, README, original checkout | Coordinator-only edits. Agents never update this shared ledger or copy files into the original checkout. |
| 1 | Six TLS/health cases | Security and lifecycle tests must test real policy/transport behavior, not mock existence; baseline regressions precede fixes. |
| 2 | Six GUI/lifecycle cases | Tests distinguish user drafts from stale data and keep resource cleanup observable; no new GUI framework. |
| 3 | Six file/model cases | All rejections preserve prior state; historical import semantics and numeric precision remain in scope. |
| 4 | Four build/runner cases | Native process ownership is justified by the existing timeout guarantee; no generic process-management dependency. |
| 5 | Five small fixes | Documentation needs no logic test; behavior changes get narrow executable regressions; dead-code removal preserves outputs. |

Ruling: Use four isolated, exclusively owned worktrees in parallel — the user requested noninterfering subagents for this large task — interface mismatches are caught at integration, without concurrent edits to the same file.

Ruling: Keep the plan and public status ledger in this requested file rather than adding separate public planning documents — one recovery map avoids duplicated status — task briefs/review artifacts remain ignored scratch files.

Ruling: Do not create commits or merge agent branches into the user's branch — the user asked for fixes, not version-control publication — integrate reviewed file patches while preserving the original checkout's unrelated changes.

Ruling: After a batch is approved, its original worker may mechanically apply only its reviewed, exclusively owned files to the original checkout on explicit coordinator authorization — this avoids retyping large patches — the coordinator checks the untouched baseline before handoff and exact source equality afterward, then owns caller/manifest integration and combined verification. Any unexpected original edit blocks that handoff.

## Execution log

Chronological record: later updates supersede earlier work-in-progress states. The table above and final post-fix results below are the current outcome.

- Baseline rechecked clean; four native worktrees created from `56c5a1b`. No implementation changes yet.
- `.gitignore` now explicitly permits this requested ledger, which otherwise matched the existing `*-FINDINGS.md` ignore rule.
- Tasks 1-4 are in progress in their isolated worktrees. Agent identities and recovery details are in the ignored task ledger.
- Task 5: console tests failed 3/4 on baseline, then passed 4/4 after removing duplicate type guesses and catching expected provider set errors.
- Task 5: PDF text tests failed 2/2 on baseline, then passed 2/2 after preserving line breaks; both still pass after removing the unused parsing chain. Documentation now targets Python 3.12 explicitly.
- Task 5 local diff check passed. The coordinator's new test scripts will be registered after the build/test batch releases manifest ownership.
- Task 5 review caught and corrected a whitespace-only PDF diagnostic regression; the new test failed first, then all 3 PDF tests passed. Console 4/4 and PDF 3/3 also pass in a separate Python 3.12 environment using the existing requirements; the original virtual environment is unchanged.
- Task 5 scoped re-review passed: spec and quality approved, R1 addressed. All 20 existing deterministic/policy suites also pass on Python 3.12 with the local fixes; this is a pre-integration baseline, not a result for the other batches.
- Task 2 implementation returned: all 6 GUI findings addressed, 14 regressions and 5 focused GUI suites pass, plus 8 default Windows suites. Independent GUI review is running; include its untracked regression script and register it after Task 4 releases the manifest.
- Task 4 implementation returned and released ownership: 58 licensing, 56 infrastructure, and 73 workflow policy checks pass, plus targeted 3/3 and default 8/8 suites. Independent native-process/build review and Python 3.12 runtime checks are running; include the new process-control helper on integration.
- Task 2 independent review requested two lifecycle corrections (late health-callback emission and pane cleanup-start retry). Its original worker is implementing regression-first fixes; no GUI files have been integrated yet. Initial 14 GUI regressions also passed Python 3.12 before those additional cases.
- Task 4 Python 3.12 runtime check: licensing 58/58 and workflow 73/73 pass; infrastructure failed to observe startup of its inherited-pipe fixture. Full log retained; independent review has the evidence. No timeout assertion has been relaxed and this batch is not verified.
- Tasks 1 and 3 implementations returned with passing author regressions/suites; independent security and provider reviews are running. Task 1 requires coordinator wiring of the verified async callback component into the provider, after preserving Task 3's provider changes.
- Task 3 Python 3.12 provider/config suites pass 2/2, but review identified two additional lossless-decimal preflight paths that still cause avoidable replacement/compensation. Its original worker is adding exact-snapshot regressions and early checks.
- Task 4 review confirmed a real interrupted-constructor process leak and a separate readiness-fixture race that can reject correct cleanup; the original worker is fixing both with bounded startup diagnostics and unchanged timeout assertions.
- Task 1 Python 3.12 focused TLS/health/network checks pass 6/6. Task 5 also passed a real Python 3.12 console session: `001` preserved, invalid 80 rejected, following 9 accepted, normal shutdown and exit 0.
- Task 1 review requested actual-certificate discovery for valid -key-key names and a narrow deferred-handler local-shutdown correction. The initial-feed GUI caller correction is already covered by Task 2. TLS-owned corrections are in fix round 1.
- Tasks 2 and 3 passed scoped re-review after their corrections. Their original workers have explicit, non-overlapping mechanical integration grants after untouched-original guards; no behavioral changes are permitted during application. The coordinator will compare reviewed source and run combined checks.
- Task 4 fix round 1 passes native Python 3.12 interruption/readiness checks (25/25), infrastructure (59/59), targeted (3/3), and default (8/8), with exact stable handle counts; Python 3.13 infrastructure also passes. Scoped re-review is running before integration.
- Tasks 1 and 4 scoped re-reviews now approve their corrections. GUI/provider mechanical integrations were independently compared against all 13 reviewed files; build/test integration was compared against all 8 reviewed files before the coordinator's intentional registrations.
- The coordinator registered console-commands, coding-export, and gui-audit-regressions on both platforms and documented them in README. The combined deterministic/policy manifest now contains 23 suites; this registration check is not a combined runtime result.
- CLI help now describes --tls-server-name as the local advertised provider/event-callback name. TLS-owned mechanical application is authorized; the async provider component still needs its coordinator wiring and a production-factory wire regression before completion.
- All four approved batches have been mechanically applied. The coordinator independently checked all 30 owned files against reviewed worktree text before intentional caller/test/manifest integration edits; EOL-only differences were disclosed. No commits or user build-output changes were made.
- Coordinator integration: the callback-policy regression now obtains clients through the actual production ProviderService/SDK SOAP pool. It failed 2/2 before async component wiring and passed 2/2 afterward. The obsolete test-only component injection and duplicate connection run were removed; full integrated TLS acceptance passes 34/34 on Python 3.12.
- Combined Python 3.12 verification is running for all 23 deterministic/policy suites plus all 5 inspected Python network suites (28 total). An isolated source snapshot is building a native Windows bundle and validating its final archive; original build/dist outputs are untouched. No final success claim is made before those results and the final review.
- Combined original-checkout verification completed: **28/28 suites pass on Windows/Python 3.12.10 and 28/28 on Windows/Python 3.13.17**, including all five explicitly selected network suites. This is not a native Linux/hosted-CI result.
- Broad manual GUI smoke first exposed a data-only namespace being used as a managed peer and a readiness assertion treating passive discovery as completed scan. The test now uses a real RemoteDevice with controlled external data and waits for the observable Connect-enabled state; product behavior/validation was not weakened. **All 339 checks pass**, with no unhashable-peer traceback.
- Native Windows **PyInstaller build, packaged smoke, and final ZIP legal-payload validation pass** in the isolated snapshot. Packaged smoke was rerun successfully under the shared network mutex after the broad GUI checks. The snapshot matches all 46 current application/build/config files after normalizing CRLF; only unchanged requirements.txt has an EOL-only archive difference.
- Source integrity check confirms exactly 39 authorized changed/new files, valid Python 3.12 syntax, unchanged HEAD, and unchanged dependency declarations. Final independent whole-change review is running; statuses remain Implemented until that gate closes.
- Final whole-change review reproduced two Important gaps under findings 9 and 12; one fix wave corrected both with regression-first evidence on both interpreters. Real SDK/Qt bridge acquired-callback coverage replaces the missing test boundary, and shared generated sample-range validation now runs before any profile mutation. Scoped final re-review approves spec and quality, with both findings ADDRESSED and no new Important issue.
- Fresh **post-final-fix** selections pass **28/28 on Python 3.12.10 and 28/28 on Python 3.13.17**; the exact manifest IDs were checked against both logs. Config-roundtrip passes 229 checks and GUI audit regressions pass all 23 methods in each run. Broad offscreen GUI smoke passes all 339 checks.
- Fresh post-fix native Windows build, mutex-serialized packaged smoke and final ZIP validation all exit 0. The archive passes 99 legal-payload checks. Snapshot/current equality covers all 113 tracked/new project files except this evolving ledger; four unchanged text files differ only in Git-archive line endings. All 39 frozen changed-source hashes remain unchanged through final verification.
- One actual SDK multicast WinError 10051 from the fixer's earlier passing covering run is retained in its report. It did not recur in either fresh complete selection or the fresh GUI smoke. No network-cause resolution or pristine-output claim is inferred from that non-recurrence.
- Completion integrity: exactly 40 authorized changed/new paths; Python 3.12 syntax and whitespace checks pass; HEAD, index and dependency declarations unchanged. All 27 findings are Verified within the stated scope. No commit, merge, push or publication was performed; modified worktrees and ignored review/test evidence are preserved.
- Publication follow-up: the user requested commit and push. A fresh complete selection on Windows/Python 3.12.10 passes **28/28 suites**, with exact ordered project coverage verified separately from the infrastructure suite's two nested fixtures and intentionally induced timeout. All 39 frozen source hashes match; only this ledger's publication status is updated. No package rebuild or new Python 3.13/native-Linux/device check is claimed.

## Verification results and limits

| Check | Environment | Result |
|---|---|---|
| All deterministic/policy/network manifest IDs (including Windows-excluded IDs selected explicitly) | Native Windows, Python 3.12.10, post-final-fix | 28 passed, 0 failed |
| Same complete selection | Native Windows, existing Python 3.13.17 environment, post-final-fix | 28 passed, 0 failed |
| Fresh pre-commit complete selection | Native Windows, Python 3.12.10, publication follow-up | 28 passed, 0 failed; exact project coverage verified, with expected nested infrastructure fixtures checked separately |
| Broad offscreen GUI smoke | Native Windows, Python 3.12.10, post-final-fix | 339 checks passed |
| Actual production TLS callback pool, metadata/control/notification/shutdown acceptance | Included in both post-fix selections | 34 checks passed per run; wiring regression demonstrated RED 2/2 → GREEN 2/2 |
| Native application build and packaged startup/shutdown smoke | Fresh post-fix Windows snapshot, Python 3.12.10 | Build exit 0; packaged smoke exit 0 under the shared network mutex |
| Final Windows ZIP legal payload | Same fresh post-fix build | 99 checks passed, exit 0 |
| Source/diff integrity | Original checkout and fresh snapshot | 40 authorized changed/new paths; 39 frozen source hashes unchanged; 113 snapshot project files match after text-EOL normalization; syntax/whitespace pass; HEAD/index/dependencies unchanged |
| Final independent integrated review and scoped fix re-review | Original checkout | Both final Important findings ADDRESSED; scoped spec and quality PASS; no open Critical/Important or separate Minor findings |

The default runner remains platform-filtered; the complete 28-suite checks explicitly selected all manifest IDs with role deterministic, policy, or network. Final logs in the ignored `.superpowers/sdd/AUDIT-FINDINGS` workspace are `postfix-combined-py312.log`, `postfix-combined-py313.log`, `postfix-gui-smoke-py312.log`, `postfix-windows-build.log`, `postfix-windows-packaged-smoke.log`, and `postfix-windows-artifact-validation.log`. Final source review is `final-fix-review.md`; regression RED/GREEN evidence is indexed in `final-fix-report.md`.

The fresh pre-commit selection is recorded in `commit-push-py312.log` in the same ignored workspace. The initial coordinator-only log check counted nested fixture headers as project suites; the corrected check confirms all 28 project IDs in order, the two expected fixtures inside the passing 59-check infrastructure suite, and the outer runner's zero-failure result. No project source or test change was needed.

The current isolated Windows archive is `C:\Users\meurdode\AppData\Local\Temp\opencode\sdc-audit-postfix-build-20261106\SDC-Testing-Toolbox-windows-x86_64.zip`, not a published release. The earlier `sdc-audit-build-20261106` archive predates the final production fixes and is retained only as historical evidence. Original repository build/dist outputs were not used for the rebuild.

Not checked: native Linux/hosted GitHub Actions execution, independent medical-device interoperability, real PDF layout extraction, physical full-disk/power-loss durability, or a full legal/vulnerability-advisory audit. Existing SDK/CPython/aiohttp integration ceilings are documented with shortcut comments. This remains a learning fixture, not a medical device or conformance claim.

Final review update: findings 9 and 12 are now Verified after the approved fix wave and all fresh post-fix checks. The earlier nonfatal multicast diagnostic remains disclosed above; the passing results do not expand the platform, legal or conformance claims below.

## Final review scope rulings

The reviewer explicitly set these existing boundaries aside. The coordinator accepts them; they are not silently treated as newly verified capabilities.

| Ruling | Decision and reason | Cost if that assumption changes |
|---|---|---|
| 1. TLS lab modes and authorization | Keep documented opt-in relaxations, unsecured discovery, and the SDK's per-operation identity limit; do not add an authentication product. | A production or finer-permission deployment needs separate security work. |
| 2. Version/layout-specific hooks | Accept the documented current SDK/CPython/aiohttp hooks and current CI layout; their present versions are tested. | Dependency or workflow-layout upgrades must revalidate/replace those hooks. |
| 3. Scale and hostile-process sandboxing | Keep the few-process, one-peer lab assumption; do not add a cleanup framework or hostile-child sandbox. | Larger/hostile deployments need new resource/containment requirements. |
| 4. Forced termination | Orderly shutdown/interruption is tested; arbitrary OS/interpreter termination is not a cleanup guarantee. | Forced termination may require OS-level recovery/cleanup. |
| 5. Filesystem durability/policy | Atomic replacement covers ordinary write/replace failures, not fsync durability, ACL preservation, multiwriter editing, or exotic filesystems. | Those guarantees require separately specified persistence behavior. |
| 6. Profile versus live snapshot | Preserve the documented profile subset and monotonic compensating recovery, not an identical live-MDIB replay. | Full replay needs a separate snapshot contract/format. |
| 7. Range conformance | Preserve tested unions/open bounds/steps and no invented origin for unanchored steps. | Independent IEEE/foreign-range conformance still needs validation. |
| 8. Editing affordances | Preserve active drafts and a missing slider's editing thumb while clearing its reading. | Full visual/accessibility assessment remains separate from offscreen checks. |
| 9. Certificate-folder scope | Support generated identities and the reported overlap/IP cases, not a general mixed-folder/concurrent certificate manager. | Arbitrary foreign layouts/device-name/filesystem cases need further hardening. |
| 10. PDF scope/private helpers | Verify line handling/diagnostics and remove unreachable private code; do not claim real PDF/OCR extraction or external private-helper compatibility. | Validate actual source layouts before relying on extracted terminology. |
| 11. Unchanged code/SDK | Fix the recorded findings and actual affected invariants rather than start another audit of every unrelated command/SDK behavior. | Other unreported bugs may remain; this is not a bug-free guarantee. |
| 12. Legal/release/advisory conclusions | Validate maintained payload rules/paths/hashes and local artifacts, without legal advice or authorizing publication. | Releases still need source-offer, legal/advisory, and hosted-platform checks. |
