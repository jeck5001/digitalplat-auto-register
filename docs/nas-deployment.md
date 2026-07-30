# NAS Deployment

The Compose service runs one registration and then exits. It deliberately uses
`restart: "no"`, so a NAS reboot cannot repeat a registration unexpectedly.

## Files And Image

Copy `docker-compose.yml` and `.env.example` to a persistent NAS directory,
then rename the latter to `.env`. The published `linux/amd64` image is:

```text
ghcr.io/jeck5001/digitalplat-auto-register:latest
```

On the NAS, prepare the directory and edit the environment file:

```bash
mkdir -p /vol1/docker/digitalplat-register/data
cd /vol1/docker/digitalplat-register
cp .env.example .env
chown 10001:10001 data
```

Set every required value in `.env`. `TURNSTILE_REMOTE_ENDPOINT` must be
reachable from the NAS container. Use a Docker service name when the solver is
on the same Compose network, or the solver's LAN address when it runs elsewhere.

## Private GHCR Login

The package follows the repository's private access. Create a GitHub classic
personal access token with only the `read:packages` scope, then log the NAS in
interactively:

```bash
docker login ghcr.io -u jeck5001
```

Do not put the token in `.env` or commit it to the NAS project folder.

## Pull And Run

Validate the rendered settings, pull the published image, then run the task:

```bash
docker compose --env-file .env config
docker compose --env-file .env pull
docker compose --env-file .env run --rm register
```

Successful output ends with `Registration completed successfully`. The detailed
log is saved to `data/digitalplat-register.log`. Before starting another run,
replace the one-time registration values in `.env`.
