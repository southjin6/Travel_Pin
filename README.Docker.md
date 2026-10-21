# Running this with Docker

### 1. Create the environment file

```bash
cp .env.example .env
```

Fill in two values:

- **`SECRET_KEY`** — required on every start. Generate one with
  `python -c "import secrets; print(secrets.token_hex(32))"`. Without it the
  container exits rather than sign cookies with a guessable default.
- **`ADMIN_PASSWORD`** — required on the **first** start only, to create the
  recovery account. Once that row exists the variable is never read again, so
  it can be emptied afterwards.

A fresh volume with either value missing does not start quietly: the container
exits and `restart: unless-stopped` retries it, which looks like a crash loop.
`docker compose logs server` prints the reason.

### 2. Start it

```bash
docker compose up --build
```

The app is on http://localhost:8000. The `cloudflared` service also publishes it
at a random `https://<something>.trycloudflare.com` hostname:

```bash
docker compose logs cloudflared | Select-String trycloudflare
```

That hostname changes on every start, and anyone holding it can register an
account — treat it as public.

### 3. Where the data lives

Accounts, pins, lockout state and the API cache are all in the `travel-data`
volume (full name `travel_pin_travel-data`), mounted at `/data`. It survives
rebuilds and `docker compose down`.

It does **not** survive `docker compose down -v`, which deletes the volume. To
take a copy, stop the app first so SQLite checkpoints its write-ahead log:

```powershell
docker compose stop server
docker compose cp server:/data/travel.db .\travel-backup.db
docker compose start server
```

### 4. Keeping the image patched

The base image (`python:3.14.8-slim`) is rebuilt by its maintainers whenever
Debian publishes security updates, but the image you built last month keeps
whatever it shipped with. Nothing here rebuilds it for you, so do it on a
schedule — monthly is reasonable for a project this size:

```bash
docker compose build --pull
docker compose up -d
```

`--pull` is the part that matters: without it Docker reuses the base image
already on disk. The volume is untouched, so there is no migration.

Check what actually landed, rather than assuming:

```bash
# OS packages — anything listed as "Debian-Security" here is missing from the image
docker run --rm --user root --entrypoint sh travel_pin-server:latest \
  -c "apt-get update -qq && apt-get -s dist-upgrade | tail -3"
# installed Python packages — must match requirements.txt exactly
docker run --rm --entrypoint python travel_pin-server:latest -m pip freeze
```

A rebuild reinstalls exactly what `requirements.txt` pins, so an outdated pin
stays outdated until it is edited. Bumping a pin and rebuilding is one step, not
two.

### 5. When something is wrong

| Symptom | Cause |
| --- | --- |
| Container restarts every few seconds | Missing `SECRET_KEY`, or a fresh volume with no `ADMIN_PASSWORD`. Read `docker compose logs server`. |
| `Bind for 127.0.0.1:8000 failed` | Something else holds port 8000; change the left-hand side of the `ports` entry. |
| Login "works" but every page bounces to sign in | `SESSION_COOKIE_SECURE` is on while you browse over plain `http://localhost`. Unset it for local use; set it to `1` only behind HTTPS. |
| Tunnel URL 502s | The server is not healthy yet; `docker compose ps` shows the health state, and `cloudflared` waits for it. |

### Deploying to a registry

```bash
docker build --platform=linux/amd64 -t myregistry.com/myapp .
docker push myregistry.com/myapp
```

### References

* [Docker's Python guide](https://docs.docker.com/language/python/)
