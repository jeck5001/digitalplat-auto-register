# NAS Deployment

The service is a Web manager at `http://NAS_IP:8400`. It accepts at most one
registration task at a time and keeps only redacted job history in the Docker
named volume `digitalplat-register-data`. It does not use a host bind mount.

## Files And Image

Use the public `linux/amd64` image `ghcr.io/jeck5001/digitalplat-auto-register:latest`.
No GitHub PAT, registry login, account password, verification code, or proxy
credential belongs in `.env`.

Upload `docker-compose.yml` and `.env.example` to the fixed NAS directory, then
copy `.env.example` to `.env` there. The default referral code is `4qn8iw8r1o`.
Only change the solver endpoint when the NAS cannot reach it.

```bash
cd /vol1/1000/docker/digitalplat-auto-register
cp .env.example .env
docker compose --env-file .env pull
docker compose --env-file .env up -d --force-recreate
curl -fsS http://127.0.0.1:8400/health
```

The entrypoint creates and repairs the named-volume ownership before it drops to
the `app` user. `restart: unless-stopped` preserves the Web service across NAS
restarts; unfinished tasks are recorded as failed after a process restart.

## Verification

Open `http://NAS_IP:8400` and start the single registration task. The page polls
every two seconds and shows the redacted task status, steps, username, temporary
email, duration, and any safe error text. After success, restart the service and
confirm the same history remains visible. Inspect container logs through the NAS
Docker UI or `docker compose logs register`; do not export browser console logs.
