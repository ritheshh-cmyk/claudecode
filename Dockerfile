# Use official python-alpine base image for minimum size
FROM python:3.14.0a2-alpine3.20

# Install system dependencies
RUN apk add --no-repeat --no-cache curl git build-base libffi-dev openssl-dev

# Install astral uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# Set working directory
WORKDIR /app

# Copy lock files and project configs
COPY pyproject.toml uv.lock ./

# Pre-install dependencies to cache layers
RUN uv sync --no-dev --no-install-project

# Copy package source files
COPY api/ ./api/
COPY cli/ ./cli/
COPY config/ ./config/
COPY core/ ./core/
COPY messaging/ ./messaging/
COPY providers/ ./providers/
COPY .env.example ./

# Install project
RUN uv sync --no-dev

# Expose proxy server port
EXPOSE 8082

# Start proxy server using uv run
CMD ["uv", "run", "fcc-server"]
