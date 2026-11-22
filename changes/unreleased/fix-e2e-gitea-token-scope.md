### Fixed
- The CI e2e Gitea token has the `write:issue` scope, so GreenKube can add labels and its status comment to the pull request (they failed with a 403 `read:issue` error).
