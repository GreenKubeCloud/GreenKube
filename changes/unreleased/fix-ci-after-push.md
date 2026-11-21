### Changed
- **CI: the Python tests run in shards.** The single job that ran Gitleaks, Ruff, Pyrefly and the whole test suite with coverage exceeded its 20-minute timeout. Lint, Gitleaks and Pyrefly now run once; the tests run in five shards per Python version (3.13 and 3.14), each with its own PostgreSQL service, the PostgreSQL-ran check and a 30-minute timeout. `.github/scripts/test-shard.sh` defines the shards: its `rest` shard runs every `tests/` directory the named shards do not list, so each test directory runs in exactly one shard. A coverage job combines the shards of each Python version and enforces `fail_under` from `pyproject.toml`. The release test job timeout is 90 minutes.
- **Frontend: SvelteKit 3** (`@sveltejs/kit` 3.4, `@sveltejs/adapter-static` 4, `@sveltejs/vite-plugin-svelte` 7, Vite 8, Svelte 5.59). The configuration moved from `svelte.config.js` into the `sveltekit()` plugin options in `vite.config.js`; `$lib` imports are `#lib` (`package.json` `imports`); `$app/stores` is replaced by `$app/state`; Tailwind runs through `@tailwindcss/vite` instead of PostCSS. The build output (`build/`, served by the API from `/app/frontend`) keeps its layout. The image builds the frontend with Node 22.23.5 (SvelteKit 3 needs Node 22.17 or later).

### Removed
- Unused frontend dev dependencies `svelte-jester` (the tests run on Vitest), `postcss` and `@tailwindcss/postcss`, and the stale `cookie`, `@babel/core` and `js-yaml` npm overrides.

### Fixed
- **CI: the promtool alert tests run on Linux runners.** Without a local promtool, `.github/scripts/promtool-check.sh` runs the Prometheus image, whose `nobody` user could not read the `mktemp -d` work directory (mode 0700); the directory and rule files are now world-readable.

### Security
- The frontend no longer depends on vulnerable `@sveltejs/kit` 2.x (GHSA-pfhg-q3qv-9q6r, GHSA-27mw-82g8-phjj, GHSA-3r8g-jcqq-3p5j, GHSA-6cf5-gc4q-fm3x) or on the jest, glob and minimatch chain of `svelte-jester`: `npm audit` reports no vulnerabilities.
