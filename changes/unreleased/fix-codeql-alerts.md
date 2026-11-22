### Security
- `DELETE /api/v1/auth/api-keys/{key_id}` refuses a key id that is not 8 lowercase letters or digits with a 404 before it reaches the audit log line (CodeQL log injection).
- Dashboard durations (`5m`, `7d`...) and Prometheus steps accept at most 6 digits and 32 characters: an oversized value is a clean 400-class error instead of a slow regex, a 4300-digit `int()` error or a `timedelta` overflow (CodeQL polynomial regex).
- The SPA fallback checks the resolved path with `os.path.realpath` and a prefix test on the frontend directory (behaviour unchanged, path traversal still refused), and the Flux `HelmRelease` match compares the `apiVersion` group exactly.
