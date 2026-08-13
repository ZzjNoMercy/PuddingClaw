# PuddingClaw CLI

`@puddingai/puddingclaw` is the single PuddingClaw command-line package. It owns the
`puddingclaw` command and combines deployment/runtime management with the Headless
Agent Harness protocol. Its default Home is `~/.puddingclaw`.

```bash
npm install -g ./packages/puddingclaw-deploy-cli
puddingclaw init
puddingclaw init --profile harness --plan --json
puddingclaw database configure
puddingclaw database show --json
puddingclaw doctor
```

Override Home for tests or an explicitly isolated installation:

```bash
PUDDINGCLAW_HOME=/absolute/path puddingclaw status --json
```

Release maintainers should follow [PUBLISHING.md](./PUBLISHING.md). `npm publish`
is a separate, explicit release action and is not performed by build or verification.

## Implemented in this slice

- One `puddingclaw` command and a CLI-managed Home.
- Separate `deploy.json` control-plane state; Backend-owned product settings keep
  their canonical `config.json` path.
- No-argument interactive `init` for Harness-only and optional extensions, including
  separate Agent and image-analysis SubAgent model bindings. Credentials are validated
  and stored independently, or safely reused when both bindings share one Provider.
- Ordered Knowledge discovery for SQLite/PostgreSQL, authenticated PostgreSQL and
  pgvector checks, Milvus/Collection checks, required Embedding configuration when
  vector indexing is selected, and optional MinerU health detection.
- Core database discovery for every Profile, with four explicit schemes: local
  PostgreSQL, Docker PostgreSQL, SQLite, or external PostgreSQL. Interactive init
  collects the applicable port/database/user/password fields; an empty password is
  generated securely. Existing local/external connections ask before creating a
  missing database. No package, container, database, or unknown process is changed
  without the user's selection and confirmation.
- Database configuration remains mutable after init. `puddingclaw database
  configure` reuses the same decision and validation flow without rerunning
  Provider, Python, port, or extension setup. The previous connection remains
  active until the new one validates and a running Backend is restarted.
- Read-only init plans covering the current Provider, Harness, Knowledge,
  Analytics, and Headless settings/probe groups; disabled extensions are removed
  from the selected plan.
- Python 3.11/3.12 and uv detection, plus explicit one-click preparation of a
  pinned user-level uv and uv-managed Python 3.12 under the isolated Home.
- Read-only port ownership probes, fail-closed non-interactive behavior, automatic or
  manually selected replacement ports, and no termination of unknown processes.
- Non-secret config editing, extension toggles, `doctor`, and `status`.
- SHA-256 verified immutable runtime bundle installation.
- Required runtime contracts for Home isolation, dynamic ports, and extension gating;
  incompatible bundles fail closed.
- Authenticated launcher ownership for `start`, `restart`, `stop`, `open`, and logs.
- Repository build tooling that embeds the Backend wheel and Next standalone Web
  into the npm package while removing `.env` files and making Backend rewrites use
  the selected runtime port.
- `runtime prepare`, which selects one of four hash-locked Harness/Knowledge/Analytics/
  Full dependency profiles and creates a release-specific uv environment under Home.
- `runtime prune`, which removes inactive Runtime releases and their matching managed
  venvs while protecting the active and recorded running versions.
- Runtime extension gates shared by GUI navigation, direct page routing, FastAPI
  routers, Agent Tool registration, middleware, background workers, and dependencies.
- The existing Headless Agent protocol is merged into this CLI under the single
  `puddingclaw agent ...` namespace: `run`, `respond`, `cancel`, `models list`,
  and `capabilities`, including sessions, JSONL progress,
  external approval continuation, and artifact export.
- `init` creates a private local Worker token under the isolated Home. It is injected
  only into the managed Backend and read by local Agent commands; it is never stored
  in `deploy.json`/`runtime.json` or printed. An explicit `PUDDINGCLAW_TOKEN` takes priority.

`stop` does not trust PID files alone. Each launcher must answer a one-time challenge
inside the CLI-managed Home before the CLI sends a signal; an unknown or stale PID is
left untouched.

## Deliberately pending

- Production runtime download, signatures, upgrade, rollback, and uninstall.
- Shared Backend Settings/Probe Service for advanced Harness tuning fields.
- Analytics data-source and Headless Worker advanced setup steps.
