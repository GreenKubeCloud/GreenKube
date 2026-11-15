### Changed
- A memory decrease whose OOM history could not be read (`oom_history_unknown`) is now an eligibility warning instead of a blocker: a person may open its pull request. The warning names the cause (`oom_unknown_no_usage_summary`, `oom_unknown_signal_failed`, `oom_unknown_signal_unavailable`) and what to check before merging; the UI shows it on the recommendation, the detail page and the plan review, and the pull-request summary repeats it.

### Fixed
- `automation.autoApprove` never approves a plan whose recommendation carries `oom_history_unknown` (new `auto_approve_blockers` code `oom_history_unknown`); before, a `maxRisk: medium` policy could approve such a decrease automatically.
