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
cd /vol1/docker/digitalplat-register
cp .env.example .env
```

The default `.env` is ready to run. Change `TURNSTILE_REMOTE_ENDPOINT` only when
the NAS cannot reach the existing solver address. Each run generates its own
username, password, name, phone number, and US address. The supplied referral
code is already included.

To use a proxy, add these optional values to `.env`:

```text
PROXY_ENABLED=true
PROXY_SERVER=http://proxy-host:port
PROXY_USERNAME=
PROXY_PASSWORD=
```

## Private GHCR Login

The package follows the repository's private access. Create a GitHub classic
personal access token with only the `read:packages` scope, then log the NAS in
interactively:

```bash
docker login ghcr.io -u jeck5001
```

Do not put the token in `.env` or commit it to the NAS project folder.

## Feiniu NAS Management UI

1. Open the `Docker` app, then open the `Compose` project named
   `digitalplat-auto-register` and select `Edit`.
2. Replace the Compose content with this repository's `docker-compose.yml`.
   Keep only the three values from `.env.example` in the project's environment
   variable editor. Do not add a host folder mapping for `/app/data`.
3. In the Docker app's `Images` page, open `Image registry` or `Registry
   authentication` (the label varies by fnOS version), then add a registry with:
   server `ghcr.io`, username `jeck5001`, and the GitHub Classic PAT as the
   password. The PAT needs `read:packages`.
4. Return to the Compose project and select `Pull image` followed by `Deploy`
   or `Rebuild`.
5. Open the completed container's `Logs` tab in the Docker app to view the
   registration output.

## Pull And Run

Validate the rendered settings, pull the published image, then run the task:

```bash
docker compose --env-file .env config
docker compose --env-file .env pull
docker compose --env-file .env run --rm register
```

Successful output ends with `Registration completed successfully`. View the
complete run output in the NAS Docker management page under the container's
logs; no host folder permissions are required.
