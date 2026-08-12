# Ubuntu user deployment

This deployment runs the current server test stack under the `rivotek` user and
does not require Docker or `sudo`. Disposable analysis data lives under
`~/perfpilot/data`; persistent users, Agents, and source workspaces live under
private `~/perfpilot/state`. SmartPerfetto, the API, and the production web
server run as user systemd services behind one target.

Clone the platform at `~/perfpilot/platform`, copy the SmartPerfetto `.env` to
`~/perfpilot/engines/SmartPerfetto/backend/.env` when provider configuration is
required, and run:

```bash
cd ~/perfpilot/platform
bash scripts/bootstrap-ubuntu-user.sh
```

Open `http://10.166.0.125:3000`. Every test-stack restart permanently deletes
all analysis data without creating a backup or archive. Persistent state stays
unchanged. Always use the wrapper so all services stop before its reset gate reruns:

```bash
bash scripts/restart-ubuntu-perfpilot.sh
```

Initial credentials for newly created `ray_wu` (administrator) and `user01`
through `user05` are written once to
`~/perfpilot/state/local-control/bootstrap-users.txt` with mode `0600`.
`ray_wu` may instead use a pre-created owner-only file configured by
`PERFPILOT_BOOTSTRAP_ADMIN_PASSWORD_FILE`. Existing accounts, passwords,
roles, and teams are never overwritten. Every new user must change the password
on first login.

This is an HTTP server-test deployment for the trusted private network. The
Docker Compose, HTTPS, PostgreSQL, Redis, object storage, multi-user isolation,
and distributed Agent production topology remains the production deployment
described in `docs/superpowers/plans/2026-08-05-perfpilot-ubuntu-lan-deployment.md`.
