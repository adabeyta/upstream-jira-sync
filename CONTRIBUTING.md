# Contributing to upstream-jira-sync

## Fixes flow upstream

This framework exists so that every team running it benefits from every fix.
If you hit a bug or need a behavior change while operating your private
deployment repo, do not patch around it in your deployment: fix it here, get
it released, and bump your pinned tag. Deployment repos hold configuration
only; anything that smells like code belongs in this repo, generalized behind
config.

Before adding a feature, ask whether it is team-specific. If it only makes
sense for one team's Jira setup, it should be expressible through existing
config (status_map, custom field ids, prompt overrides via `skills_dir`)
rather than new code paths. New features must ship disabled by default and
support `shadow` mode before `auto`.

## Versioning and upgrades

- Releases follow **SemVer**. Breaking changes to config keys, the state-file
  schema, CLI flags, or the `upstream_jira_sync.llm` provider protocol require a
  major version bump and a migration note in the release notes.
- Deployment repos pin an exact release tag
  (`uses: .../upstream-jira-sync@vX.Y.Z`, or the same tag via
  `pip install "upstream-jira-sync @ git+https://...@vX.Y.Z"`). Never ask operators
  to track a branch. Teams upgrade by bumping the tag in a PR, where their
  `check-config` workflow validates their config against the new version
  before merge.
- Additive config keys must have safe defaults so an unmodified `config.yaml`
  keeps working across minor versions.
- `action.yml` input names are part of the public API and covered by the
  SemVer promise, alongside config keys, CLI flags, the state-file schema,
  and the LLM provider protocol.

## Cutting a release

1. Bump `version` in `pyproject.toml` and move the `Unreleased` notes in
   `CHANGELOG.md` under the new version.
2. Merge, then tag the release commit `vX.Y.Z` and push the tag.
3. `.github/workflows/release.yml` re-runs the test suite at the tag, fails
   if the tag and `pyproject.toml` version disagree, creates the GitHub
   release with generated notes, and force-moves the floating major tag
   (`vX`) so `uses: .../upstream-jira-sync@vX` consumers pick the release up.
   Deployment repos pinning exact tags upgrade via their own PR.

## The tenant-literal rule

This repo is public and data-free. **No team-specific literals are accepted
in PRs**: no company or product names, no real Jira hostnames, project keys,
or board ids, no concrete custom field ids as code defaults, no Atlassian
team UUIDs, no personal names or email addresses, no internal GCP project
ids, no team-specific bot logins or merge labels.

CI enforces this with a leak-grep step that fails the build on known tenant
literals and on any email-shaped string outside `@example.com`-style
placeholders. The tenant literal list itself lives in the
`TENANT_LEAK_PATTERNS` repo Actions variable (space-separated regexes — use
`\s` instead of literal spaces), not in the workflow file — the public
workflow must not reveal the very strings it guards against. If your PR trips it, replace the literal with:

- a config key (with no default, validated when the owning feature is
  enabled), or
- a neutral placeholder in docs/examples: `https://yourcompany.atlassian.net`,
  `PROJ`, `customfield_XXXXX`, `yourorg/your-upstream-repo`,
  `someone@example.com`.

The same applies to AI prompts: packaged `skills/*.md` files use neutral
examples and template variables (`{teams_section}`, `{pr_title}`, ...);
team-specific guidance belongs in a deployment repo's `skills_dir` override.

Privacy rules are part of this: never log Jira emails (GitHub handles only),
never store per-person attribution outside the state file's `digest`
namespace, and never rank team members by activity volume in any output.
`tests/test_privacy.py` guards these; do not weaken it.

## Tests

- Run the full suite before pushing: `python -m pytest tests/ -v`. All tests
  must pass.
- New behavior needs targeted tests: typically the happy path plus the key
  regression case, not an exhaustive matrix.
- Bug fixes include a test that fails without the fix.
- Anything touching logging, the state file, or the digest should extend the
  privacy tests when it handles roster data.
- For end-to-end behavior, use the mock server
  (`python tests/mock_server.py`, then
  `upstream-jira-sync sync --config tests/fixtures/config.test.yaml --mock-url
  http://localhost:9999 --dry-run`) rather than real APIs. Never test against
  a live Jira project you do not own.

## Style

- `ruff check .` and `ruff format .` must be clean; CI runs the test suite
  and the leak-grep on every PR.
- Minimal comments; only non-obvious logic. Prefer expressive names over
  explanatory prose. No verbose docstrings.
- Modules stay under ~500 LOC where feasible; split by functional cohesion.
- No dead code: every function, import, and constant must be used.
- All AI prompts live in `upstream_jira_sync/skills/*.md`, never hardcoded in
  Python. New prompts register their template variables in
  `SKILL_TEMPLATE_VARS` so override validation covers them.
- Jira writes use `notifyUsers=false`; Jira transitions are resolved by
  target status, never by transition name.

## Dev setup

```bash
pip install -e ".[dev]"          # add ,vertex for the vertex provider
python -m pytest tests/ -v
ruff check . && ruff format --check .
```
