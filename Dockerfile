# --- STAGE 1: Frontend Build ---
# Builds the SvelteKit SPA into static files.
# The output is pure HTML/CSS/JS — architecture-independent.
# We pin to linux/amd64 so that multi-platform builds never run this
# stage under slow QEMU emulation.
FROM --platform=linux/amd64 node:20-alpine AS frontend-builder

WORKDIR /frontend

COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --ignore-scripts 2>/dev/null || npm install --ignore-scripts

COPY frontend/ .
RUN npm run build && rm -rf node_modules

# --- STAGE 2: Python Builder ---
# Builds the Python package and installs dependencies
FROM python:3.14.3-slim-bookworm AS builder

WORKDIR /app

# Install build tools
RUN pip install --no-cache-dir --upgrade pip build

# Copy all project files required for the build
COPY pyproject.toml .
COPY README.md .
COPY LICENSE .
COPY src /app/src

# Build and install the package (and its dependencies) into a temporary prefix
# Using 'pip install .' reads pyproject.toml and runs the hatchling build backend
# We install into /install to easily copy it to the final stage
# Note: packaging is installed explicitly because limits 5.x uses it at runtime
# but pip considers it satisfied by its own vendored copy.
RUN pip install --no-cache-dir . --prefix=/install \
    && pip install --no-cache-dir --ignore-installed --force-reinstall --no-deps packaging --prefix=/install

# --- STAGE 3: Final Image ---
# This stage creates the final, lean image
FROM python:3.14.3-slim-bookworm

# Create a non-root user named "greenkube"
# -m creates the home directory (/home/greenkube)
# -s /bin/bash sets the default shell
RUN useradd -m -s /bin/bash greenkube

# Copy the installed application and its dependencies from the builder stage
# This copies the 'greenkube' executable to /usr/local/bin
# and the Python libraries to /usr/local/lib/python3.14/site-packages
COPY --from=builder /install /usr/local

# Copy the SPA frontend build into /app/frontend
COPY --from=frontend-builder /frontend/build /app/frontend

# Set the working directory to the user's home
WORKDIR /home/greenkube

# Switch to the non-root user
USER greenkube

# The 'pip install .' in the builder stage already created the
# 'greenkube' entrypoint in /usr/local/bin

# Health check for standalone Docker usage (when running the API).
# In Kubernetes, liveness/readiness probes from the Helm chart take precedence.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:' + __import__('os').environ.get('API_PORT', '8000') + '/api/v1/health')" || exit 1

ENTRYPOINT ["greenkube"]

# Set a default command to run if no other is specified
CMD ["--help"]