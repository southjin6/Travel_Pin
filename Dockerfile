# syntax=docker/dockerfile:1

# Comments are provided throughout this file to help you get started.
# If you need more help, visit the Dockerfile reference guide at
# https://docs.docker.com/go/dockerfile-reference/

# Want to help us make this template better? Share your feedback here: https://forms.gle/ybq9Krt8jtBL3iCk7

ARG PYTHON_VERSION=3.14.8
FROM python:${PYTHON_VERSION}-slim as base

# Prevents Python from writing pyc files.
ENV PYTHONDONTWRITEBYTECODE=1

# Keeps Python from buffering stdout and stderr to avoid situations where
# the application crashes without emitting any logs due to buffering.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Create a non-privileged user that the app will run under.
# See https://docs.docker.com/go/dockerfile-user-best-practices/
ARG UID=10001
RUN adduser \
    --disabled-password \
    --gecos "" \
    --home "/nonexistent" \
    --shell "/sbin/nologin" \
    --no-create-home \
    --uid "${UID}" \
    appuser

# Download dependencies as a separate step to take advantage of Docker's caching.
# Leverage a cache mount to /root/.cache/pip to speed up subsequent builds.
# Leverage a bind mount to requirements.txt to avoid having to copy them into
# into this layer.
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=bind,source=requirements.txt,target=requirements.txt \
    python -m pip install -r requirements.txt

# SQLite lives outside /app on purpose: COPY creates root-owned files, so the
# non-privileged user cannot create a database beside the code. This directory is
# mounted as a volume so accounts and pins survive a rebuild.
RUN mkdir -p /data && chown appuser:appuser /data
ENV TRAVEL_DB=/data/travel.db

# Switch to the non-privileged user to run the application.
USER appuser

# Copy the source code into the container.
COPY . .

# Expose the port that the application listens on.
EXPOSE 8000

# Two things are checked, because either one alone is misleading: the HTTP probe
# proves a worker is accepting requests, and the SQLite query proves the schema
# exists — without it, a container with an uninitialised database would still
# report healthy while every page 500s. There is no curl or wget in this image,
# so the probe uses the stdlib.
HEALTHCHECK --interval=30s --timeout=6s --start-period=20s --retries=3 \
    CMD python -c "import os,sqlite3,sys,urllib.request; sqlite3.connect(os.environ['TRAVEL_DB']).execute('SELECT 1 FROM users LIMIT 1').fetchone(); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/login', timeout=4).status == 200 else 1)"

# init_db() only runs from the __main__ block, which gunicorn never executes, so
# the schema, the migrations and the recovery admin are created by this import
# first. A missing SECRET_KEY or ADMIN_PASSWORD then fails loudly here, before the
# server starts accepting traffic.
# --timeout clears a landmark scan's 35s budget (OVERPASS_BUDGET in app.py); the
# gunicorn default of 30s would kill the worker mid-response.
# --no-control-socket: gunicorn defaults it to $HOME/.gunicorn/gunicorn.ctl, and
# appuser deliberately has no home directory, so it logs a permission error on
# every boot. The control interface is for live worker management, which is not
# something this container does.
CMD ["sh", "-c", "python -c 'import app; app.init_db()' && exec gunicorn app:app --bind 0.0.0.0:8000 --workers 2 --threads 4 --timeout 120 --no-control-socket --access-logfile -"]
