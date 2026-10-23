# --- STAGE 1: Frontend Build ---
# Builds the SvelteKit SPA into static files.
# The output is pure HTML/CSS/JS — architecture-independent, so build it
# on the native build platform instead of running npm through emulation.
FROM --platform=$BUILDPLATFORM node:22.14.0-alpine3.21 AS frontend-builder

WORKDIR /frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts

COPY frontend/ .
RUN npm run build && rm -rf node_modules

# --- STAGE 2: Python Builder ---
# Builds the Python package and installs dependencies.
# Uses bookworm-slim and upgrades all system packages to fix known CVEs
# (openssl, zlib, ncurses, etc.) before installing Python deps.
FROM python:3.14.3-slim-bookworm AS builder

WORKDIR /app

ARG TARGETARCH

# Upgrade all system packages to pick up security patches
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update -o Acquire::Retries=5 -o APT::Update::Error-Mode=any \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends ca-certificates curl tar \
    && rm -rf /var/lib/apt/lists/*

# Install pinned, checksum-verified renderers into the builder prefix so the
# final image does not need package-manager state or network access.
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
      amd64) \
        helm_arch=amd64; \
        helm_sha256=896cf63684a42a723d61c980369d6e588330927deb13676ca0d85006dd6da0ea; \
        kustomize_arch=amd64; \
        kustomize_sha256=cb9e31198d3f63b44848bd0afc4c8efc56e6cfef9c93a4a39a211940e6decd61 ;; \
      arm64) \
        helm_arch=arm64; \
        helm_sha256=4c29e286b279d170fa048ddfc3e4e83dcd069ca9c52102766ba672d7149e694d; \
        kustomize_arch=arm64; \
        kustomize_sha256=9867b76482cfc6d25b546abc1d2b5a32bd137505aba640649b4b378f64f6d68f ;; \
      *) echo "Unsupported renderer architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    mkdir -p /install/bin /tmp/greenkube-renderers/helm /tmp/greenkube-renderers/kustomize; \
    curl --fail --silent --show-error --location --retry 5 \
      "https://get.helm.sh/helm-v3.22.1-linux-${helm_arch}.tar.gz" \
      -o /tmp/greenkube-renderers/helm.tar.gz; \
    printf '%s  %s\n' "${helm_sha256}" /tmp/greenkube-renderers/helm.tar.gz | sha256sum --check --status; \
    curl --fail --silent --show-error --location --retry 5 \
      "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize/v5.8.3/kustomize_v5.8.3_linux_${kustomize_arch}.tar.gz" \
      -o /tmp/greenkube-renderers/kustomize.tar.gz; \
    printf '%s  %s\n' "${kustomize_sha256}" /tmp/greenkube-renderers/kustomize.tar.gz | sha256sum --check --status; \
    tar -xzf /tmp/greenkube-renderers/helm.tar.gz -C /tmp/greenkube-renderers/helm; \
    tar -xzf /tmp/greenkube-renderers/kustomize.tar.gz -C /tmp/greenkube-renderers/kustomize; \
    install -m 0755 "/tmp/greenkube-renderers/helm/linux-${helm_arch}/helm" /install/bin/helm; \
    install -m 0755 /tmp/greenkube-renderers/kustomize/kustomize /install/bin/kustomize; \
    rm -rf /tmp/greenkube-renderers /var/lib/apt/lists/*

# Install uv for fast dependency resolution and installation
COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /usr/local/bin/uv

# Copy all project files required for the build
COPY pyproject.toml .
COPY uv.lock .
COPY README.md .
COPY LICENSE .
COPY src /app/src

# Build and install the package (and its dependencies) into a temporary prefix.
# uv pip install reads pyproject.toml and runs the hatchling build backend.
# We install into /install to easily copy it to the final stage.
# Note: packaging is installed explicitly because limits 5.x uses it at runtime
# but uv considers it satisfied by its own vendored copy.
RUN uv export --locked --no-dev --no-emit-project --format requirements-txt \
        --output-file=/tmp/requirements.txt \
    && uv pip install --no-cache --system --prefix=/install \
        --require-hashes -r /tmp/requirements.txt \
    && uv pip install --no-cache --system --no-deps . --prefix=/install

# --- STAGE 3: Final Image ---
# This stage creates the final, lean image.
# SECURITY: All system packages are upgraded, non-root user with high UID,
# and no shell for the service account.
ARG APT_CACHE_BUST=manual
FROM python:3.14.3-slim-bookworm

ARG APT_CACHE_BUST
ARG TARGETARCH

# Upgrade all system packages in the final image to fix CVEs
# (libssl3, zlib1g, ncurses, systemd, libc, etc.)
RUN echo "apt-cache-bust=${APT_CACHE_BUST}" \
    && sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update -o Acquire::Retries=5 -o APT::Update::Error-Mode=any \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends bubblewrap iptables util-linux \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user with UID > 10000 to avoid host UID conflicts.
# /sbin/nologin prevents interactive shell access for defense-in-depth.
RUN groupadd -g 10001 greenkube \
    && useradd -u 10001 -g greenkube -m -s /sbin/nologin greenkube

# Copy the installed application and its dependencies from the builder stage
# This copies the 'greenkube' executable to /usr/local/bin
# and the Python libraries to /usr/local/lib/python3.14/site-packages
COPY --from=builder /install /usr/local

# Copy the SPA frontend build into /app/frontend
COPY --from=frontend-builder /frontend/build /app/frontend

# Create a writable tmp directory for the non-root user (Python needs it)
RUN mkdir -p /tmp/greenkube /etc/greenkube /var/run/secrets /greenkube-render-workspace \
    && chown greenkube:greenkube /tmp/greenkube

# Set the working directory to the user's home
WORKDIR /home/greenkube

# Switch to the non-root user
USER 10001

# The 'uv pip install .' in the builder stage already created the
# 'greenkube' entrypoint in /usr/local/bin

# Health check for standalone Docker usage (when running the API).
# In Kubernetes, liveness/readiness probes from the Helm chart take precedence.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:' + __import__('os').environ.get('API_PORT', '8000') + '/api/v1/health')" || exit 1

ENTRYPOINT ["greenkube"]

# Set a default command to run if no other is specified
CMD ["--help"]