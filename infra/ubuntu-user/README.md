# Ubuntu user deployment

This deployment runs the current server test stack under the `rivotek` user and
does not require Docker or `sudo`. Disposable analysis data lives under
`~/perfpilot/data`; persistent users, Agents, and source workspaces live under
private `~/perfpilot/state`. SmartPerfetto, the API, the production web server,
and the HTTPS gateway run as user systemd services behind one target.

Clone the platform at `~/perfpilot/platform`, copy the SmartPerfetto `.env` to
`~/perfpilot/engines/SmartPerfetto/backend/.env` when provider configuration is
required, and run:

```bash
cd ~/perfpilot/platform
bash scripts/bootstrap-ubuntu-user.sh
```

Install `~/perfpilot/config/perfpilot-agent-ca.crt` on each browser and Agent
host after verifying its SHA-256 fingerprint over SSH. Open
`https://10.166.0.125:8443`; configure Agents with the same origin and CA file.
The API and web processes listen only on loopback. Ordinary restarts preserve
analysis data as well as persistent users, Agents, and source workspaces:

```bash
bash scripts/restart-ubuntu-perfpilot.sh
```

Use the explicit destructive wrapper only when all analysis data should be
permanently deleted without creating a backup or archive. Persistent users,
Agents, and source workspaces remain unchanged:

```bash
bash scripts/reset-and-restart-ubuntu-perfpilot.sh
```

Initial credentials for newly created `ray_wu` (administrator) and `user01`
through `user05` are written once to
`~/perfpilot/state/local-control/bootstrap-users.txt` with mode `0600`.
`ray_wu` may instead use a pre-created owner-only file configured by
`PERFPILOT_BOOTSTRAP_ADMIN_PASSWORD_FILE`. Existing accounts, passwords,
roles, and teams are never overwritten. Every new user must change the password
on first login.

This is a private-CA HTTPS server-test deployment for the trusted private
network. The Docker Compose, PostgreSQL, Redis, object storage, and distributed
production topology remains the deployment described in
`docs/superpowers/plans/2026-08-05-perfpilot-ubuntu-lan-deployment.md`.
