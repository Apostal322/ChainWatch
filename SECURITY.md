# Security policy

ChainWatch is a research and portfolio project, not a supported product. Only the latest
commit on `main` receives fixes.

## Reporting a vulnerability

Please report it privately through GitHub's **Security → Report a vulnerability** form for
this repository, not in a public issue. Include the affected endpoint or file, steps to
reproduce, and the impact you expect.

## Trust model (what is and is not a vulnerability)

- **Model artifacts are trusted code.** Registry bundles are joblib pickles; loading one
  executes code. The service only loads bundles listed in the registry index, inside the
  registry directory, with matching SHA-256 values and provenance. Those checks guard
  integrity, not authenticity: only serve registries you built yourself
  (`docs/INFERENCE_CONTRACT.md`).
- **The public demo is read-only and synthetic.** It has no authentication, by design.
  Exposure controls (body limits, rate limiting, security headers) are described in
  `docs/DEPLOYMENT.md` §6; ways around them are in scope.
- **The real-data mode is for private use.** Exposing it publicly is a misconfiguration,
  not a vulnerability (the dataset has no redistribution licence).

Past findings and their fixes are listed in `docs/AUDIT.md`.
