# V5.0.0 publication gate (Owner review)

This file is a publication checklist, not proof that a GitHub Release already exists.

## Verified repository baseline
- Repository: Righteous1776/ZeoStudio-TenGate
- Reviewed V5 main SHA: `6808c187f8456fb590f56e7345fbc480e104d7ca`
- Prior latest GitHub Release at review: `v4.1.0`
- Project metadata: `pyproject.toml` version `5.0.0`, Python >=3.11, Apache-2.0
- CI for the reviewed V5 commit: Python 3.11/3.12/3.13 successful, full reliability job successful.
- V5 tag and Release were absent when the publication proposal was created.
- `AGENTS.md`, `SKILL.md` and `AGENT_INBOX/` are not present on the reviewed main tree. No authority was inferred from nonexistent files.

## Gate before publication
- [ ] Review this PR and verify no unreviewed source changes relative to the reviewed SHA.
- [ ] CI passes on this PR and on main after any merge.
- [ ] Review workflow permissions: the release job alone requests `contents: write`.
- [ ] Inspect `.github/workflows/publish-v5.yml` before merging; it is **workflow_dispatch only**.
- [ ] Owner explicitly selects `Publish ZeoStudio TenGate V5.0.0` in Actions on main and clicks Run workflow.
- [ ] The job executes full release validation and reliability suites again.
- [ ] The job builds and checks source ZIP, wheel, SPDX SBOM, SHA256SUMS and manifest.
- [ ] The job refuses publication if `v5.0.0` already exists, or unreviewed source changes are present.
- [ ] Confirm the release is public, assets are correct, and checksum validation succeeds after download.

## Handling security and licensing
- All runtime dependencies are from Python standard library; build tooling includes setuptools and wheel (MIT licensed, build-time only).
- Do not include private key files, actual secrets, production SQLite databases, decrypted private artifacts or personal information in a release.
- Release assets are not independently signed in this workflow; SHA-256 verifies integrity when the checksum list comes from a trusted channel.
- V5 runtime source is different from previous A/B/C Skill packages. Do not market the knowledge packages as included in the V5 source ZIP unless separately added and verified.
- A green internal CI run does not establish an independent security audit or universal reliability.
- Failure or cancellation must not be described as a completed release.

## Rollback
- Before publishing: close PR or disable the dispatch workflow.
- After publishing: preserve evidence, publish a corrective release or mark the release as deprecated; do not silently delete or rewrite tags and pretend past downloads do not exist.
