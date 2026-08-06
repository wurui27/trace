# Ubuntu user deployment

This deployment runs the current server test stack under the `rivotek` user and
does not require Docker or `sudo`. It keeps runtime data under
`~/perfpilot/data`, keeps both analysis engines in independent Git checkouts,
and supervises SmartPerfetto, the API, and the production web server with user
systemd services.

Clone the platform at `~/perfpilot/platform`, copy the SmartPerfetto `.env` to
`~/perfpilot/engines/SmartPerfetto/backend/.env` when provider configuration is
required, and run:

```bash
cd ~/perfpilot/platform
bash scripts/bootstrap-ubuntu-user.sh
```

Open `http://10.166.0.125:3000`. Restarts preserve analysis history:

```bash
systemctl --user restart perfpilot-smartperfetto perfpilot-api perfpilot-web
```

This is an HTTP server-test deployment for the trusted private network. The
Docker Compose, HTTPS, PostgreSQL, Redis, object storage, multi-user isolation,
and distributed Agent production topology remains the production deployment
described in `docs/superpowers/plans/2026-08-05-perfpilot-ubuntu-lan-deployment.md`.
