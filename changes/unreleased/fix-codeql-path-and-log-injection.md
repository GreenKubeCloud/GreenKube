### Security
- The SPA fallback serves only the files listed once from the frontend build at start (symlinks skipped): a request path is a key into that list and never becomes a filesystem path. Traversal attempts still get a 404.
- Log lines that quote request data (API key revocation, single sign-on failures, user updates, the timeseries cache error) escape CR and LF with the new `greenkube.utils.log.log_safe`, so a forged line break cannot start a fake entry in the text log format.
