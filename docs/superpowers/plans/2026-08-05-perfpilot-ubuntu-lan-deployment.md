# PerfPilot Ubuntu LAN Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Use superpowers:test-driven-development before implementation and superpowers:verification-before-completion before each commit.

**Goal:** Install a reproducible, HTTPS-only PerfPilot stack on `rivotek@10.166.0.125`, persist all customer data under `/data/perfpilot`, and keep SmartPerfetto and Android Memory independently updateable.

**Architecture:** Docker Compose runs one edge gateway and private backend services. Browser traffic enters `perfpilot.lan`; S3 presigned traffic enters `objects.perfpilot.lan`; both resolve to `10.166.0.125` and terminate at Caddy on port 443. PostgreSQL, Redis, MinIO, SmartPerfetto, API, and workers have no host-published ports. Platform and engine releases are immutable directories selected by symlink and image digest. Secrets are mounted as files and never committed or placed in image layers.

**Tech Stack:** Ubuntu 24.04, Docker Engine/Compose v2, Caddy, PostgreSQL 17, Redis 8, MinIO, Python 3.12, Node 22 for the web image, Node 24 inside SmartPerfetto, Bash, OpenSSL.

---

## Fixed deployment names

```text
Primary URL:       https://perfpilot.lan
Object URL:        https://objects.perfpilot.lan
Fixed address:     10.166.0.125
LAN CIDR:          10.166.0.0/24
Mac admin address: 10.160.0.219/32
Compose project:   perfpilot
Release root:      /opt/perfpilot
Data root:         /data/perfpilot
```

The deployment CA certificate includes DNS SANs `perfpilot.lan` and `objects.perfpilot.lan`, plus IP SAN `10.166.0.125`. Until local DNS owns those names, the browser/Agent bootstrap adds both names to the OS hosts file after administrator confirmation.

## Task 1: Build production images and a closed Compose topology

**Files:**
- Create: `infra/lan/images/api.Dockerfile`
- Create: `infra/lan/images/web.Dockerfile`
- Create: `infra/lan/Caddyfile`
- Create: `infra/lan/compose.yaml`
- Create: `infra/lan/images.lock.json`
- Create: `infra/lan/compose.env.example`
- Create: `infra/lan/README.md`
- Modify: `services/api/src/perfpilot_api/config.py`
- Modify: `services/api/src/perfpilot_api/main.py`
- Create: `services/api/tests/unit/test_lan_production_config.py`
- Create: `tests/lan-compose.test.mjs`
- Modify: `package.json`
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Write failing production-topology tests**

```javascript
test("only the gateway publishes a host port", async () => {
  const compose = await loadCompose("infra/lan/compose.yaml");
  const published = Object.entries(compose.services)
    .filter(([, service]) => Array.isArray(service.ports) && service.ports.length > 0)
    .map(([name]) => name);
  assert.deepEqual(published, ["gateway"]);
  assert.deepEqual(compose.services.gateway.ports, ["443:443"]);
});

test("persistent services bind only beneath /data/perfpilot", async () => {
  const compose = await loadCompose("infra/lan/compose.yaml");
  assertPersistentSources(compose, "/data/perfpilot/");
});
```

Python tests instantiate production settings from mounted secret files and prove that a development password, HTTP origin, loopback dependency, tag-only engine image, or writable secret file is rejected.

- [ ] **Step 2: Run RED**

```bash
node --test tests/lan-compose.test.mjs
.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_lan_production_config.py -q
```

Expected: FAIL because `infra/lan` is absent.

- [ ] **Step 3: Build the API/worker image**

`api.Dockerfile` uses a pinned Python 3.12 slim base by digest, installs the locked uv workspace, copies contracts and migrations, runs as UID/GID `10001`, has a read-only root filesystem, and provides the same image for `api`, `provisioner`, `scheduler`, `dispatcher`, `reconciler`, `trace-worker`, and `synthesis-worker`. Commands differ only in Compose.

The image health command is:

```text
python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/v1/health', timeout=2)"
```

- [ ] **Step 4: Build the web image**

`web.Dockerfile` uses pinned Node `22.13.0`, runs `npm ci`, `npm run build`, prunes development dependencies, and starts `npm run start -- --host 0.0.0.0`. It runs as an unprivileged UID with `PERFPILOT_API_ORIGIN=http://api:8000`. The browser still uses `/api/v1/*`; Agent paths do not traverse the Vinext proxy.

- [ ] **Step 5: Define the private Compose graph**

Create these services with health checks and `restart: unless-stopped`:

```text
gateway, web, api, postgres, redis, object-store, smartperfetto,
provisioner, scheduler, dispatcher, reconciler, trace-worker, synthesis-worker
```

Use two internal networks: `edge` for gateway/web/object-store and `backend` for API/data/workers. Only gateway joins a non-internal network. Bind PostgreSQL, Redis, MinIO objects, SmartPerfetto data, reports, and logs to explicit `/data/perfpilot/*` directories. Set memory/CPU limits so PostgreSQL, Redis, gateway, web, and API retain capacity while the single heavy worker uses at most 8 GiB and 4 CPUs.

`smartperfetto` uses a locally built digest-pinned image from commit `1508f99788bfcf18cc861e4bf4f8b472e84240c3`. Android Memory uses the existing `infra/engines/android-memory/Dockerfile` at commit `d5514972ced78c3faa7fc17589c1ea9231645056`. Neither engine source is copied into the platform repository.

- [ ] **Step 6: Route HTTPS and S3 hostnames**

`Caddyfile` serves the deployment certificate/key without automatic ACME:

```caddyfile
perfpilot.lan, 10.166.0.125 {
    tls /run/secrets/tls_cert /run/secrets/tls_key
    @agent path /v1/agent/*
    handle @agent { reverse_proxy api:8000 }
    handle /v1/health { reverse_proxy api:8000 }
    handle { reverse_proxy web:3000 }
}

objects.perfpilot.lan {
    tls /run/secrets/tls_cert /run/secrets/tls_key
    reverse_proxy object-store:9000
}
```

Set S3 path-style public signing endpoint to `https://objects.perfpilot.lan` and internal administration endpoint to `http://object-store:9000`. The application must use separate validated settings for those endpoints so presigned URLs never contain a container hostname.

- [ ] **Step 7: Lock images and verify Compose**

`images.lock.json` records immutable digest references for Caddy, PostgreSQL, Redis, MinIO, the platform image, web image, SmartPerfetto image, and Android Memory image. Add `scripts/perfpilot-lan lock-images` in Task 3 to update the file explicitly; CI rejects tag-only references.

```bash
docker compose -f infra/lan/compose.yaml config --quiet
node --test tests/lan-compose.test.mjs
.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_lan_production_config.py -q
npm run lint
git add infra/lan services/api/src/perfpilot_api services/api/tests tests package.json .github/workflows
git commit -m "build: add PerfPilot LAN containers"
```

## Task 2: Add one-time Ubuntu bootstrap, PKI, secrets, and administrator creation

**Files:**
- Create: `infra/lan/bootstrap-ubuntu.sh`
- Create: `infra/lan/versions.env`
- Create: `infra/lan/pki/openssl.cnf`
- Create: `infra/lan/secrets/README.md`
- Create: `scripts/lib/perfpilot-lan-common.sh`
- Create: `tests/lan-bootstrap.test.mjs`
- Modify: `services/api/src/perfpilot_api/cli.py`
- Create: `services/api/tests/unit/test_admin_cli.py`

- [ ] **Step 1: Write failing bootstrap safety tests**

Test that bootstrap:

- refuses non-root execution with a clear local command;
- refuses a host IP other than `10.166.0.125` unless `--allow-ip-change` is explicit;
- creates only `/opt/perfpilot` and `/data/perfpilot` descendants;
- never reads a sudo password from stdin or arguments;
- adds firewall rules for `443/tcp` from `10.166.0.0/24` and `10.160.0.219/32`, and `22/tcp` only from `10.160.0.219/32`;
- does not enable the firewall until the current SSH source is proven allowed.

- [ ] **Step 2: Run RED**

```bash
node --test tests/lan-bootstrap.test.mjs
.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_admin_cli.py -q
```

- [ ] **Step 3: Implement an idempotent root bootstrap**

The operator runs this directly on Ubuntu and types the sudo password only into Ubuntu's own prompt:

```bash
sudo bash /opt/perfpilot/platform/current/infra/lan/bootstrap-ubuntu.sh \
  --server-ip 10.166.0.125 \
  --lan-cidr 10.166.0.0/24 \
  --admin-cidr 10.160.0.219/32
```

The script verifies Ubuntu 24.04 x86-64, `/data` capacity, clock synchronization, and required kernel features; installs the exact Docker/Compose package versions from `versions.env`; adds `rivotek` to the Docker group; creates service directories with explicit owners/modes; enables Docker; stages UFW rules; and prints that a new login is required for group membership. Rerunning it must not destroy data or regenerate secrets.

- [ ] **Step 4: Generate deployment PKI and bootstrap assets**

`scripts/perfpilot-lan pki-init` creates an Ed25519 root CA and server key/certificate with the fixed SANs, all under `/data/perfpilot/secrets/pki`. Key files are `0600`; public CA/certificate files are `0644`. It prints the SHA-256 certificate fingerprint and never prints a private key.

`scripts/perfpilot-lan bootstrap-bundle` creates `/data/perfpilot/bootstrap/perfpilot-agent-bootstrap.tar.gz` containing only:

```text
perfpilot-ca.crt
perfpilot-agent-config.json
hosts-entry.txt
device-agent-macos.pkg
device-agent-windows.msi
device-agent-linux.deb
SHA256SUMS
INSTALL.txt
```

It verifies every package against the CI manifest before inclusion and writes no registration code.

- [ ] **Step 5: Generate secret files**

`scripts/perfpilot-lan secrets-init` uses `openssl rand` locally on Ubuntu to create independent proxy, session, Agent HMAC, JWS Ed25519, PostgreSQL, Redis, MinIO, backup, and service credentials beneath `/data/perfpilot/secrets/runtime`, mode `0600`. AI and SmartPerfetto provider credentials are not invented; the command creates empty required files and reports them as a blocking configuration item when AI is enabled.

Compose mounts each secret into `/run/secrets`. Update `Settings` to read Pydantic secret files and reject environment plaintext for sensitive production fields. Add only non-secret toggles and paths to `/data/perfpilot/config/compose.env`.

- [ ] **Step 6: Add an interactive production-admin command**

Implement:

```text
perfpilot-admin create-admin --username ray_wu
```

It reads the password twice with `getpass`, rejects `ray_wu`, breached development defaults, username inclusion, fewer than 14 characters, or mismatch, hashes with the existing Argon2 helper, and is idempotent only when `--rotate-password` is explicit. The password never enters shell history, environment, stdout, audit metadata, or Compose files.

- [ ] **Step 7: Run GREEN and commit**

```bash
node --test tests/lan-bootstrap.test.mjs
.venv/bin/pytest -p no:cacheprovider services/api/tests/unit/test_admin_cli.py -q
.venv/bin/ruff check services/api/src services/api/tests
git add infra/lan scripts services/api/src/perfpilot_api/cli.py services/api/tests tests
git commit -m "ops: bootstrap Ubuntu LAN host safely"
```

## Task 3: Add immutable platform/engine releases and daily operator commands

**Files:**
- Create: `scripts/perfpilot-lan`
- Create: `scripts/lib/perfpilot-lan-release.sh`
- Create: `scripts/lib/perfpilot-lan-engines.sh`
- Create: `scripts/lib/perfpilot-lan-compose.sh`
- Create: `tests/perfpilot-lan-cli.test.mjs`
- Modify: `infra/engines/engine-lock.yaml`
- Modify: `infra/engines/engine-lock.schema.json`
- Modify: `README.md`

- [ ] **Step 1: Write failing command tests**

```javascript
for (const command of ["status", "doctor", "start", "stop", "restart", "update", "rollback", "reset", "logs", "engine-update", "bootstrap-bundle", "lock-images"]) {
  test(`LAN CLI documents ${command}`, async () => {
    const result = await execFile(script, [command, "--help"]);
    assert.equal(result.exitCode, 0);
  });
}
```

Use a fake Docker/Git/OpenSSL harness. Tests must prove `restart` never deletes data, `reset` refuses without interactive confirmation, and `update` never changes `current` before build/migration/health gates pass.

- [ ] **Step 2: Run RED**

```bash
node --test tests/perfpilot-lan-cli.test.mjs
```

- [ ] **Step 3: Implement platform release updates**

`scripts/perfpilot-lan update --ref GIT_SHA` performs:

1. Require a full 40-character commit and a clean destination.
2. Fetch the commit into `/opt/perfpilot/platform/releases/GIT_SHA` without local edits.
3. Validate contracts, engine lock, Compose, and image lock.
4. Build platform images tagged by commit and record their digests.
5. Start data services, run control and tenant migrations once, then start application services.
6. Require PostgreSQL, Redis, object-store, API, web, SmartPerfetto, trace-worker, and synthesis-worker health.
7. Atomically switch `/opt/perfpilot/platform/current` and write a deployment receipt.
8. Keep the previous release and images for rollback.

Rollback switches code/images only and refuses when the target release declares an incompatible database head. It never runs Alembic downgrade automatically.

- [ ] **Step 4: Implement independent engine updates**

`engine-update smartperfetto --commit SHA` and `engine-update android-memory --commit SHA` clone the configured source into `/opt/perfpilot/engines/NAME/releases/SHA`, verify the exact commit, run the repository's contract tests, build an image, inspect its immutable digest, smoke-test it on the private network, update `engine-lock.yaml` image digest in a platform change, and only then switch the engine symlink.

The first accepted engine commits are:

```text
SmartPerfetto:               1508f99788bfcf18cc861e4bf4f8b472e84240c3
Android-App-Memory-Analysis: d5514972ced78c3faa7fc17589c1ea9231645056
```

Do not copy either source tree into `platform-web` and do not follow upstream branches at runtime.

- [ ] **Step 5: Implement daily commands**

```text
scripts/perfpilot-lan status
scripts/perfpilot-lan doctor
scripts/perfpilot-lan restart
scripts/perfpilot-lan logs --service api --since 30m
scripts/perfpilot-lan reset
```

`status` is read-only. `doctor` checks IP/DNS/CA fingerprint, NTP, disk, mounts, container health, database migration heads, object read/write/delete, Redis ping, engine health, AI credential presence, and worker concurrency. `restart` runs `docker compose restart` and verifies health without deleting volumes or bind directories. `reset` requires the exact phrase `DELETE PERFPILOT ANALYSES`, creates a pre-reset backup, clears tenant analyses/artifacts/reports and object prefixes, and preserves users, teams, Agents, configuration, and installation.

- [ ] **Step 6: Run GREEN and commit**

```bash
node --test tests/perfpilot-lan-cli.test.mjs
docker compose -f infra/lan/compose.yaml config --quiet
git add scripts README.md infra/engines tests
git commit -m "ops: manage immutable LAN releases"
```

## Task 4: Bootstrap and smoke-test the actual Ubuntu host

**Files:**
- Create on Ubuntu during execution: `/data/perfpilot/receipts/bootstrap.json`
- Create on Ubuntu during execution: `/data/perfpilot/receipts/deployment-GIT_SHA.json`
- Do not commit either receipt; commit only sanitized acceptance fixtures in Plan 4.

- [ ] **Step 1: Push the verified implementation commit**

```bash
git push origin main
```

Do this only after Plans 1–3 code tests are green. Record the exact remote commit.

- [ ] **Step 2: Stage the release on Ubuntu**

From the Mac:

```bash
ssh -i /Users/ray/.ssh/perfpilot_ubuntu_ed25519 rivotek@10.166.0.125
```

On Ubuntu, clone/fetch the exact commit under `/opt/perfpilot/platform/releases`, then run the bootstrap command from Task 2. The operator types sudo credentials locally; no password is sent through chat, a script argument, or a file.

- [ ] **Step 3: Finish production secrets and admin creation**

Run `pki-init`, verify the printed CA fingerprint on the Mac, run `secrets-init`, place the real AI provider values into their `0600` secret files, build both engine images, then create the administrator using the interactive command. Do not reuse the development password.

- [ ] **Step 4: Start and verify the stack**

Run:

```bash
/opt/perfpilot/platform/current/scripts/perfpilot-lan start
/opt/perfpilot/platform/current/scripts/perfpilot-lan doctor
```

Install the CA and hosts entries on the Mac, open `https://perfpilot.lan`, log in, create a registration code, install the macOS Agent, and verify one real device appears. Stop here if any dependency is unhealthy; do not weaken TLS or publish backend ports to bypass a failed check.

## Plan 3 closure gate

The plan is complete only when:

- `ss -lnt` on Ubuntu shows PerfPilot externally only on `443`, plus the existing restricted SSH service;
- `docker compose ps` reports every required service healthy;
- `curl --cacert perfpilot-ca.crt https://perfpilot.lan/v1/health` succeeds;
- `curl --cacert perfpilot-ca.crt https://objects.perfpilot.lan/minio/health/ready` succeeds;
- restarting Ubuntu preserves the admin, Agent, device, analysis history, and reports;
- `scripts/perfpilot-lan doctor` exits `0` and emits no secret values.
