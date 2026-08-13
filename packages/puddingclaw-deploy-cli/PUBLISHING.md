# PuddingClaw npm publishing runbook

The public package name is reserved as `@puddingai/puddingclaw`. Publishing remains
intentionally blocked: `package.json` is still `private: true`, the repository root
license is not yet present, and `verify:publish` rejects those states.

## Account setup

1. Create the npm account and enable two-factor authentication for authorization
   and publishing.
2. Confirm the authenticated npm account owns the `puddingai` user scope, or has
   publish permission if `puddingai` is later converted to an organization.
3. Perform the first release manually with 2FA. Configure npm Trusted Publishing
   for the GitHub Actions workflow after the package exists, then remove long-lived
   automation tokens.

## Repository release gates

The following gates must all pass before removing `private: true`:

- Root `LICENSE` and third-party notices exist.
- Final npm package name and `puddingclaw` bin ownership remain confirmed.
- Backend extension gating really consumes the manifest contract; disabled
  Knowledge/Analytics tools and workers are absent at runtime.
- Python dependencies are split into Core and extension groups.
- A production runtime is built from `uv.lock`; its exported requirements contain
  hashes and `manifest.install.python.require_hashes` is true.
- The pinned uv bootstrap is smoke-tested on clean macOS, Linux, and Windows
  machines before changing the package from private to public.
- New and existing CLI test suites pass.
- The npm tarball contains no `.env`, `.npmrc`, `.pypirc`, private key, or local
  credential material.

## Build and inspect

From this package directory:

```bash
npm run build:runtime
npm test
npm run verify:publish
npm pack --dry-run
```

`build:runtime` creates an ignored `runtime-bundle/` directory containing:

```text
runtime-bundle/
├── backend/
│   ├── puddingclaw_backend-<version>-py3-none-any.whl
│   ├── requirements-harness.lock
│   ├── requirements-knowledge.lock
│   ├── requirements-analytics.lock
│   └── requirements-full.lock
├── web/
│   ├── server.js
│   ├── .next-runtime-build/
│   ├── node_modules/
│   └── public/
└── manifest.json
```

The build removes Next standalone environment files, changes the Backend rewrite
to use the runtime-selected port, rejects symlinks/sensitive filenames, and records
the SHA-256 of every shipped file.

`--skip-build` is only for local packaging diagnostics. It reuses existing build
outputs and copies the unhashed development requirements file, so
`verify:publish` deliberately rejects it.

## First publication

After the remaining release gates pass, confirm the CLI/runtime version, then run:

```bash
npm login
npm whoami
npm publish --access public
```

Publishing is never executed from a developer machine until `verify:publish`
reports `publish_ready`. Later releases should use npm staged/trusted publishing
with maintainer approval and provenance.
