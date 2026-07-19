# Deploying Mission Control

Mission Control runs as **three cooperating processes**:

| Process | What it is | Depends on |
|---|---|---|
| **Postgres** | durable state (checkpointer + runs/notifications outbox) — via Docker | — |
| **service** | the HTTP seam, `python -m mission_control.service`, binds `127.0.0.1:8000` | Postgres |
| **Slack bridge** | `python -m mission_control.slack.bridge` — Socket Mode out to Slack, polls the outbox | the service |

This guide wires the service + bridge as long-running **launchd** agents (macOS) or
**systemd** units (Linux). Everything is **env-driven and account-agnostic** — no path,
account, workspace name, or token lives in the repo. Templates are in
[`deploy/launchd/`](../deploy/launchd/); copy each `*.example`, fill placeholders, and
the realized copies are git-ignored.

---

## Prerequisites

1. **Docker** — for Postgres:
   ```bash
   cd <repo>
   docker compose up -d postgres      # starts container mc-postgres on :5432
   docker compose ps                  # STATUS should be healthy
   ```
   State lives in the `mc_pgdata` named volume and survives restarts.

2. **Python + libpq (Homebrew, macOS).** The venv provides the app; `psycopg` links
   **libpq at runtime**, and Homebrew's libpq is *keg-only* (not on the default linker
   path), so a launchd job — which does **not** inherit your shell — needs it pointed at
   explicitly (the wrapper does this; see below).
   ```bash
   brew install libpq                 # provides libpq + pg_config
   python3 -m venv .venv && .venv/bin/pip install -e .
   brew --prefix                      # note this → __HOMEBREW_PREFIX__ (/opt/homebrew or /usr/local)
   ```
   On Linux: `apt-get install libpq5` (runtime) or `libpq-dev` (build); it's usually on
   the default path, so the `DYLD_FALLBACK_LIBRARY_PATH` step is macOS-only.

3. **The env file contract.** Copy [`deploy/launchd/mc.env.example`](../deploy/launchd/mc.env.example)
   to a path **outside the repo** (e.g. `~/.mission-control/mc.env`), fill it in, and
   `chmod 600` it — it holds Slack **secrets**. It is the single source of `MC_*` config
   for both processes; the wrapper sources it with `set -a`. Key vars:
   - `MC_POSTGRES_URL` — must match docker-compose (`postgresql://mc:mc@127.0.0.1:5432/mission_control?sslmode=disable`).
   - `MC_SLACK_REGISTRY` — path to the **non-secret** registry JSON (also outside the repo).
   - `MC_SERVICE_URL` — the seam the bridge polls (match the service bind).
   - `MC_DEFAULT_SLACK_PROFILE` *(optional)* — stamps launches that don't name a profile.
   - `MC_API_TOKEN` *(optional, recommended in prod)* — a bearer token gating the
     **mutating** run endpoints (`approve`/`reject`/`scrub`/`cancel`). Unset = open
     (localhost/dev). Set = those endpoints require `Authorization: Bearer <token>`; the
     Slack bridge reads the **same** var and sends it. Reads (GET) stay open. Generate
     with `openssl rand -hex 32`. This is the only auth on the seam today — the Slack
     identity gate is separate and additive.
   - The per-profile **bot + app tokens**, under the variable **names your registry
     declares** (`token_env` / `app_token_env`). Names are config; only values are secret.

   > **Repo-agnostic rule:** the registry JSON and tokens never enter the repo. The repo
   > knows only variable *names* and paths, all supplied at runtime.

---

## macOS — launchd

### Install

```bash
cd <repo>/deploy/launchd
mkdir -p logs

# realize the templates (replace placeholders with YOUR values; no secrets in these files)
for f in wrapper.sh com.mission-control.service.plist com.mission-control.slack.plist; do
  sed -e "s#__REPO__#$(cd ../.. && pwd)#g" \
      -e "s#__PYTHON__#$(cd ../.. && pwd)/.venv/bin/python#g" \
      -e "s#__ENV_FILE__#$HOME/.mission-control/mc.env#g" \
      -e "s#__HOMEBREW_PREFIX__#$(brew --prefix)#g" \
      "$f.example" > "$f"
done
chmod +x wrapper.sh

# install as user agents (run under your login session, no root)
cp com.mission-control.service.plist com.mission-control.slack.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mission-control.service.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mission-control.slack.plist
```

The service plist waits for Postgres (`MC_WAIT_TCP=127.0.0.1:5432`); the bridge plist
waits for the service (`MC_WAIT_HTTP=…/targets`) — so boot order is handled even though
launchd starts them concurrently.

### Verify

```bash
launchctl print gui/$(id -u)/com.mission-control.service | grep -E 'state|pid'
curl -s http://127.0.0.1:8000/targets            # service up
curl -s http://127.0.0.1:8000/slack/profiles     # your profile(s) — proves the registry loaded
tail -f deploy/launchd/logs/service.err.log deploy/launchd/logs/slack.err.log
```
A healthy bridge logs `slack bridge active profiles: [...]`. A profile with a bad/absent
token logs `slack profile '<name>' skipped: <reason>` and the rest keep running.

### Reload after a change / Teardown

```bash
# reload one job after editing its plist or the env file
launchctl bootout gui/$(id -u)/com.mission-control.slack
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mission-control.slack.plist

# full teardown
launchctl bootout gui/$(id -u)/com.mission-control.slack
launchctl bootout gui/$(id -u)/com.mission-control.service
rm ~/Library/LaunchAgents/com.mission-control.{service,slack}.plist
docker compose down                 # add -v to also delete the mc_pgdata volume
```

---

## Linux — systemd (equivalent)

Same three processes; systemd handles ordering natively (`After=`/`Requires=`) and reads
the env file with `EnvironmentFile=`. Install units under `~/.config/systemd/user/` (user
services) — no root needed; enable lingering (`loginctl enable-linger $USER`) if they must
run without an active login.

`~/.config/systemd/user/mission-control-service.service`:
```ini
[Unit]
Description=Mission Control service
# If Postgres runs via docker-compose as a systemd unit, add it here; otherwise the
# ExecStartPre below waits for the port.
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/repos/Mission-Control
EnvironmentFile=%h/.mission-control/mc.env
# libpq is on the default path on Linux; add LD_LIBRARY_PATH only if you built it keg-style.
ExecStartPre=/bin/sh -c 'until nc -z 127.0.0.1 5432; do sleep 2; done'
ExecStart=%h/repos/Mission-Control/.venv/bin/python -m mission_control.service
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

`~/.config/systemd/user/mission-control-slack.service`:
```ini
[Unit]
Description=Mission Control Slack bridge
After=mission-control-service.service
Requires=mission-control-service.service

[Service]
Type=simple
WorkingDirectory=%h/repos/Mission-Control
EnvironmentFile=%h/.mission-control/mc.env
ExecStartPre=/bin/sh -c 'until curl -fsS -o /dev/null "$MC_SERVICE_URL/targets"; do sleep 2; done'
ExecStart=%h/repos/Mission-Control/.venv/bin/python -m mission_control.slack.bridge
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

Install / verify / teardown:
```bash
systemctl --user daemon-reload
systemctl --user enable --now mission-control-service mission-control-slack
systemctl --user status mission-control-slack
journalctl --user -u mission-control-slack -f
# teardown
systemctl --user disable --now mission-control-slack mission-control-service
docker compose down
```

> The `wrapper.sh` template is macOS-flavored (it sets `DYLD_FALLBACK_LIBRARY_PATH`), but
> it is plain bash and works on Linux too — you can point systemd's `ExecStart` at it
> instead of inlining the wait, if you'd rather keep one code path. The `EnvironmentFile=`
> + `After=`/`Requires=` approach above is the idiomatic systemd equivalent.
