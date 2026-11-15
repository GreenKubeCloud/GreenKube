### Added
- Usage summaries carry `restart_increase`, the restarts over the lookback derived from the rollup's hourly restart count (SQLite and PostgreSQL), as the fallback when the scan's reliability signals are unknown.
- Risk factor and eligibility blocker `oom_history_unknown`: a memory decrease whose OOM history could not be read is at least medium risk and never becomes a pull request.
- Rightsizing source runs report a `reliability_signals_unknown` warning diagnostic naming the reliability signals that failed.

### Fixed
- Rightsizing no longer fails open when OOM kills or restarts are unknown (REC-04): a CPU or memory decrease needs explicit zero signals or a rollup restart history without restart; otherwise it is withheld with the reason `reliability_signals_unknown`, and the source run is incomplete so previous advice (such as an OOM-driven increase) is kept.
- A failed OOM or restart query is no longer reported as zero kills or restarts, and one failing reliability query no longer drops the other signals.
