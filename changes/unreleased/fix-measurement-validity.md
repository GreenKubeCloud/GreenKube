### Added
- `CHANGE_MEASUREMENT_PRIOR_WEEK_CV` (`config.recommendations.changeMeasurement.priorWeekCv`, default 0.05): week-to-week variation assumed by a change measurement whose baseline holds fewer than two weeks.

### Fixed
- Change verdicts are statistically valid (MEA-01): the interval of a measured saving adds a between-week term (estimated from the baseline weeks, or the prior with a single baseline week) to a circular, studentised block bootstrap, and a decided verdict needs 5 days of aligned post-window hours. A seeded simulation keeps the false-`realized` rate of a zero saving at or below the nominal 5%. `request_rightsizing` strategy version 2.3; measurements decided by 2.2 are kept unchanged.
- A saving is `realized` only when its interval's lower bound is above the noise floor (MEA-09); a saving above zero but below the floor is reported as negligible.
