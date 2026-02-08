# --- STAGE 1: Frontend Build ---
# Builds the SvelteKit SPA into static files
FROM node:20-alpine AS frontend-builder

WORKDIR /frontend

COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --ignore-scripts 2>/dev/null || npm install --ignore-scripts

COPY frontend/ .
RUN npm run build && rm -rf node_modules

# --- STAGE 2: Python Builder ---
# Builds the Python package and installs dependencies
FROM python:3.11-slim AS builder

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
RUN pip install --no-cache-dir . --prefix=/install

# --- STAGE 3: Final Image ---
# This stage creates the final, lean image
FROM python:3.11-slim-bookworm

# Create a non-root user named "greenkube"
# -m creates the home directory (/home/greenkube)
# -s /bin/bash sets the default shell
RUN useradd -m -s /bin/bash greenkube

# Copy the installed application and its dependencies from the builder stage
# This copies the 'greenkube' executable to /usr/local/bin
# and the Python libraries to /usr/local/lib/python3.11/site-packages
COPY --from=builder /install /usr/local

# Copy the SPA frontend build into /app/frontend
COPY --from=frontend-builder /frontend/build /app/frontend

# Set the working directory to the user's home
WORKDIR /home/greenkube

# Switch to the non-root user
USER greenkube

# The 'pip install .' in the builder stage already created the
# 'greenkube' entrypoint in /usr/local/bin
ENTRYPOINT ["greenkube"]

# Set a default command to run if no other is specified
CMD ["--help"]