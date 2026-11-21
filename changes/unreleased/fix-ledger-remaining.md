### Fixed
- Savings attribution refuses to run when the process clock is more than 5 minutes from the PostgreSQL clock (`greenkube_attribution_errors_total{reason="clock_skew"}`), never books a slot after the current hour, exposes a watermark left in the future as `greenkube_accrual_watermark_ahead_seconds`, and keeps accruing recommendations while that watermark is ahead (LED-06).
- Each recommendation accrues from its own cursor: a record that was invalid for some runs gets the missed slots back once valid (LED-12 c).
