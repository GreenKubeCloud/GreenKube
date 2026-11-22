### Fixed
- The CI end-to-end loop installs OpenCost (chart 2.5.33, reading the kube-prometheus-stack Prometheus) and exports `OPENCOST_URL`. Without it every post-change row stayed priced `opencost_pending`, the verifier counted 0 trusted savings samples, and each verdict closed `inconclusive` (`insufficient_trusted_savings_samples (0 < 6)`).
