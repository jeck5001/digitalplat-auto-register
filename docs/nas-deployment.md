# NAS Deployment

This project runs one registration per container execution. It is deliberately
not a long-running service, and the Compose service has `restart: "no"` so a
NAS reboot cannot repeat a registration unexpectedly.

## Publish The Image

The GitHub workflow publishes `linux/amd64` images to GHCR whenever `main` or a
`v*` tag is pushed. The current checkout has no Git remote, so first create or
connect the GitHub repository, then push the files in this project. The image
name is:

```text
ghcr.io/<github-owner>/digitalplat-auto-register:latest
```

For a private GitHub package, log the NAS in to GHCR with a token that has the
minimum `read:packages` scope before pulling the image.

## Configure The NAS

Copy these files to a persistent NAS folder, for example
`/vol1/docker/digitalplat-register`:

- `docker-compose.yml`
- `.env.example`, renamed to `.env`

Create a writable log folder for the image's unprivileged user:

```bash
mkdir -p data
chown 10001:10001 data
```

Set every required value in `.env`. `TURNSTILE_REMOTE_ENDPOINT` must be
reachable from the NAS container. Use a Docker service name when the solver is
on the same Compose network, or use the LAN address when it runs elsewhere.

## Run And Verify

Render the effective configuration before executing it, then pull and run one
task:

```bash
docker compose --env-file .env config
docker compose --env-file .env pull
docker compose --env-file .env run --rm register
```

Successful output ends with `Registration completed successfully`. The detailed
log is written to `data/digitalplat-register.log`. Run a new registration only
after replacing the one-time profile values in `.env`.
