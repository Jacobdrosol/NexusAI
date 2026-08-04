# GitHub Actions workflows

Automated GitHub Actions workflows are currently disabled to avoid unnecessary CI usage while testing is performed outside GitHub Actions.

CI was disabled because testing is being performed locally at this time.

The previous CI configuration is preserved in `ci.yml.md`. GitHub only recognizes workflow files with a `.yml` or `.yaml` extension in this directory, so the preserved file does not run.

To re-enable CI, review the configuration and rename `ci.yml.md` to `ci.yml`.
