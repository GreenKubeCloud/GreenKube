### Added
- **Recommendation filters and ranking on cards (K2):** The recommendations page filters by namespace, source, risk level and capability through the existing `GET /recommendations/active` query parameters (the Ignored tab applies the same filters). Each card shows its ranking score and top contributing factor, with the full factor breakdown on hover.
- **Rescan with run tracking (K9, E1):** A **Rescan** button requests a scan with `POST /optimization/runs`, polls the run and reloads the results when it completes. When the server also offers the optional run list (`GET /optimization/runs?limit=1`), a page opened while a scan is running follows that run and reloads on completion; a missing list never disables Rescan. Only a `404`/`405` from `POST /optimization/runs` makes the button fall back to plain reads.
- **Sign out (F13):** A **Sign out** button in the sidebar forgets the API key for the current tab.

### Changed
- **Truthful sort options (K2, BUG-20):** The default order is labelled **Ranking score (default)** and keeps the server order. **Projected cost savings** and **Projected CO₂e savings** are explicit sorts; ranking profiles are labelled as profiles and show their own scores.
- **Plan previews are discarded on close (D6, BUG-48):** Closing the dialog of an unapproved preview calls `DELETE /automation/plans/{id}`. A plan whose approval was requested is never discarded, even when the dialog closes while the approval is in flight, and there is no fallback to the cancel endpoint (which also cancels approved plans). A `409 WORKLOAD_PLAN_ALREADY_EXISTS` now explains what blocks the preview and when to retry.
- **Connectivity popup:** Its dismissal is remembered for the browser tab session instead of reappearing on every reload (K9).

### Fixed
- **API key asked again after every reload (F13, BUG-63):** The dashboard keeps the key in `sessionStorage` for the current tab (never `localStorage`), clears it on `401` and on sign out, and tolerates blocked storage. The key dialog is a real sign-in form (read-only account `api-key`, password field with `autocomplete="current-password"`) so browsers and password managers can save and autofill it. After signing in, the current page reloads its data.
- **Page loads scheduled full scans and requests could not be cancelled (K9, BUG-25):** The dashboard and recommendations pages no longer send `refresh=true`; every read passes an `AbortController` signal that is aborted when the page is left or reloaded.
- **401 bypass in mutation helpers (K9, BUG-25):** Service configuration updates, apply, ignore, restore, dashboard refresh and report download go through the shared request path, so a `401` opens the sign-in dialog.
- **Inconsistent namespace filter (K9):** Ignored and applied recommendations now honour the selected namespace like the active list and savings.
