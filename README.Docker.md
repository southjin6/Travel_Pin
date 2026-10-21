### Building and running your application

When you're ready, start your application by running:
`docker compose up --build`.

Your application will be available at http://localhost:8000.

### Deploying your application to the cloud

First, build your image, e.g.: `docker build -t myapp .`.
If your cloud uses a different CPU architecture than your development
machine (e.g., you are on a Mac M1 and your cloud provider is amd64),
you'll want to build the image for that platform, e.g.:
`docker build --platform=linux/amd64 -t myapp .`.

Then, push it to your registry, e.g. `docker push myregistry.com/myapp`.

Consult Docker's [getting started](https://docs.docker.com/go/get-started-sharing/)
docs for more detail on building and pushing.

### Keeping the image patched

The base image (`python:3.14.8-slim`) is rebuilt by its maintainers whenever
Debian publishes security updates, but the image you built last month keeps
whatever it shipped with. Nothing here rebuilds it for you, so do it on a
schedule — monthly is reasonable for a project this size:

```bash
docker compose build --pull
docker compose up -d
```

`--pull` is the part that matters: without it Docker reuses the base image
already on disk. Accounts and pins live in the `travel-data` volume, so a
rebuild does not touch them and needs no migration.

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

### References
* [Docker's Python guide](https://docs.docker.com/language/python/)