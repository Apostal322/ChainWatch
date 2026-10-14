# Release readiness: ChainWatch v0.4.0

Updated 2026-10-13. Owner decisions: **MIT licence; project name ChainWatch; default
branch `main`; Render Free hosting; release v0.4.0; public demo on synthetic data only.**
Every "verified" item below was executed, either in CI or locally, and its evidence is
named. Items marked **needs owner approval** have **not** been done.

## 1. Git history safety

### 1.1 Reference audit (every ref on the remote, `git ls-remote`)

| Ref | Commit | Contains real-data screenshots? |
|---|---|---|
| `refs/heads/main` | `0e190d2` (initial commit, README only) | no |
| `refs/heads/claude/chainwatch-ml-system-vxq2wa` | development branch | **yes, in its history** |
| tags, `refs/pull/*`, notes, other refs | none exist | n/a |
| forks | 0 (GitHub API) | n/a |

### 1.2 What is affected

The only paths ever removed from the tree are four screenshots of a local run on the real
Elliptic++ data: `docs/images/{replay,performance,models,limitations}.png`. Together they
show about 25 transaction IDs with labels and scores, and one transaction's augmented
features. They occur in 3 commits, as 8 distinct blobs:

| Commit | Screenshot blobs |
|---|---|
| `f2aa2fb` (Phase 3) | `b877f7e` replay, `52872ea` performance, `600cb14` models, `ddcdcb2` limitations |
| `f556f6a` (Phase 3 docs) | same four blobs |
| `98f3452` (Phase 4) | `5dc25ae` replay, `c82148e` performance, `601c906` models, `ec8121e` limitations |

Every other historical text file was scanned for transaction-ID patterns and secrets:
none were found. Reports have only ever held aggregate statistics.

**Public accessibility, measured with unauthenticated requests on 2026-10-13:**
`https://raw.githubusercontent.com/Apostal322/ChainWay/98f3452…/docs/images/replay.png`
returned **HTTP 200 with the full 361,895-byte file**, and the commit page for `f2aa2fb`
returned HTTP 200. The content is publicly downloadable today.

### 1.3 What GitHub's guidance says

From GitHub's [Removing sensitive data from a repository](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)
(read through search results, because docs.github.com is blocked in the build
environment):
- Rewriting history (`git filter-repo --sensitive-data-removal --invert-paths --path …`)
  and force-pushing is not enough on its own. The old commits stay reachable through
  **cached views by SHA**, through **pull requests** that reference them, and through
  **forks**.
- Only **GitHub Support** can remove cached views and pull-request references, and only
  after every other reference has been cleaned. Support **does not remove non-sensitive
  data**, and focuses on sensitive data (for example credentials) whose risk cannot be
  mitigated another way. Whether these screenshots would qualify is uncertain.
- Clones that others already made keep the old history; nobody can recall them.

### 1.4 Options

| | A. Clean the existing repository in place | B. Fresh public repository (recommended) |
|---|---|---|
| Steps | push a clean `main`; delete the development branch; delete the CI runs that point at the three commits; ask GitHub Support to purge cached views; rename `ChainWay` → `ChainWatch` | make `ChainWay` **private** at once; create an empty public `ChainWatch`; push only the clean `main`; later delete `ChainWay` |
| Are the screenshots publicly reachable afterwards? | **Unknown.** It depends on Support, which may decline for non-credential data; `curl` by SHA would keep returning 200 until GitHub garbage-collects | **No, verifiable.** A private repository returns 404 to unauthenticated requests; re-run the `curl` check in §1.2 |
| What is lost | nothing visible | the old repository's CI history (11 runs) and creation date; it has 0 stars, forks, issues and pull requests |
| Destructive? | yes: branch deletion and run deletion | visibility change (reversible); deletion is a later, separate decision |
| Approval needed | yes | yes |

**Recommendation: option B.** It is the only option whose result can be verified, it
costs nothing of value (no stars, forks, issues or pull requests), and it gives the
release a clean repository named after the project, without depending on a rename
redirect. **Neither option can claim permanent deletion.** Copies made while the
repository was public (clones, and archives such as Software Heritage) are outside the
owner's control, and GitHub may retain data internally after deletion (its documentation
describes restoring recently deleted repositories).

## 2. The clean `main` branch (prepared)

`main` now = `0e190d2` (initial commit) + **one** release commit whose tree is the
verified v0.4.0 project. It has a single parent and does not reference any development
commit, so pushing `main` alone cannot carry the old screenshots. The release commit is
authored with the owner's GitHub no-reply identity and has a Claude co-author trailer.

Verified before pushing (commands in §6):
- the release commit's tree is identical to the verified development-branch tree;
- `git rev-list main` contains none of `f2aa2fb`, `f556f6a`, `98f3452`;
- none of the 8 screenshot blobs is reachable from `main`;
- no path under `data/`, `artifacts/`, `mlruns/` and no `*.pkl`, `*.joblib`, `*.parquet`,
  `*.db` or `.env` file is in the tree;
- every image in `docs/images/` is a `demo_*` (synthetic) screenshot.

## 3. Licence

- **MIT** applied: `LICENSE` (Copyright (c) 2026 Apostal322; replace the handle with your
  legal name if you prefer), `pyproject.toml` (`license = "MIT"`, `license-files`;
  verified in a built wheel: `License-Expression: MIT`), README section and badge, and the
  Hugging Face Space front matter.
- **Can everything be distributed under MIT?** Yes. No third-party source code is vendored
  or copied into the repository (checked by scanning for licence, copyright and
  attribution markers). Dependencies are referenced, not committed, and all of them are
  permissive or weak-copyleft: MIT, BSD, ISC, Apache-2.0, PSF, and MPL-2.0 (`certifi`).
  None is GPL.
- **Excluded from MIT:** the Elliptic++ dataset, which is never committed. The aggregate
  statistics and figures in `reports/` are derived from it and stay subject to its
  authors' terms (the README says so).

## 4. GitHub presentation (needs owner approval to apply)

- **Name:** `ChainWatch`. Every link in the repository already points to
  `github.com/Apostal322/ChainWatch`: the CI badge, Hugging Face README and deployment
  guide. Until the repository with that name exists, the CI badge shows as broken.
- **Description:**
  > Leakage-aware ranking of illicit Bitcoin transactions under temporal distribution
  > shift (Elliptic++): retraining backtests with label latency, plus a
  > FastAPI + React replay service that reproduces research scores exactly. Synthetic
  > Docker demo.
- **Topics:** `machine-learning`, `distribution-shift`, `concept-drift`,
  `temporal-validation`, `xgboost`, `fastapi`, `react`, `typescript`, `docker`, `mlops`,
  `bitcoin`, `elliptic-dataset`
- **Website:** empty until the Render URL passes the smoke test.
- **README badges:** CI, MIT licence, Python 3.11–3.13, test count.
- **Security policy:** `SECURITY.md`. Turn on Settings → Code security → **Private
  vulnerability reporting**, which it relies on.
- **Release:** tag `v0.4.0` on the release commit; use the `v0.4.0` section of
  `CHANGELOG.md` as the release notes.

## 5. Verification evidence

| Check | Result | Where |
|---|---|---|
| Python tests | 262 passed | local; CI matrix Ubuntu/Windows × 3.11/3.13 |
| Lint and format | clean | `ruff check`, `ruff format --check` |
| Frontend | type-check, 3 tests and build pass | local; CI frontend job |
| Security | `pip-audit` (serving lock): no known vulnerabilities; `npm audit`: 0 | local |
| Secrets | none in any of the 220 historical blobs | local regex scan |
| Docker | `demo`, `runtime` and default-target images build; smoke test passes, including under 512 MB / 0.1 CPU | local; CI docker job |
| Replay consistency | 234,596 scores, max difference 0.0 | `reports/phase3/replay_consistency.json` |
| `render.yaml` | structure, `plan: free` and paths checked; **not** checked against Render's live schema (unreachable) | local |
| Hosted demo | **not deployed** | n/a |

No research result or artifact changed.

## 6. Exact remaining steps

**Owner approvals needed:** (1) option B or A in §1.4; (2) the visibility change and
repository creation that option B implies; (3) Render account creation; and, later,
(4) deleting `ChainWay`.

**Option B (recommended):**
1. `ChainWay` → Settings → Danger zone → **Change visibility → Private**. Verify from a
   logged-out terminal; the command should print `404`:
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' https://raw.githubusercontent.com/Apostal322/ChainWay/98f3452a8a9cd281b7a0b00b9b2b86b110606a93/docs/images/replay.png
   ```
2. Create an **empty** public repository `Apostal322/ChainWatch`: no README, licence or
   .gitignore.
3. Push only the clean `main` and the tag:
   ```bash
   git clone --single-branch --branch main https://github.com/Apostal322/ChainWay.git chainwatch-release
   cd chainwatch-release
   git rev-list --all | grep -E '^(f2aa2fb|f556f6a|98f3452)' && echo STOP || echo clean
   git tag -a v0.4.0 -m "ChainWatch v0.4.0"
   git push https://github.com/Apostal322/ChainWatch.git main v0.4.0
   ```
4. On `ChainWatch`: set the description, topics and private vulnerability reporting
   (§4); create release **v0.4.0** from the tag with the `CHANGELOG.md` notes; confirm
   that CI is green on `main`.
5. Render: **New → Blueprint** → `ChainWatch` → confirm **Free** → apply
   (`docs/DEPLOYMENT.md` §3).
6. `WAIT_SECONDS=300 scripts/smoke_demo.sh https://<service>.onrender.com`. Publish the
   URL (README, website field, `docs/PORTFOLIO.md`) only if it prints `PASS`.
7. Later, separately: delete `ChainWay` (Settings → Danger zone), or keep it private as an
   archive.

I can do steps 2–4 from this environment once you approve and add the new repository to
the session. Steps 1, 5 and 7 need your GitHub and Render accounts.

**Option A:** push nothing further; delete the development branch and its 11 CI runs;
rename the repository to `ChainWatch`; contact GitHub Support with the three commit SHAs
and eight blob IDs from §1.2; re-run the `curl` check until it returns 404. Then
continue with steps 4–6 above.
