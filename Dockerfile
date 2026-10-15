FROM python:3.12-slim

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Node.js 22 (official build) for npm-installed CLIs and Node-based workspace
# tools; the children's book engine in tools/childrens-books requires Node 22+.
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version && npm --version

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Pin the current Copilot CLI so model updates cannot reuse a stale `latest`
# install layer. The version can also be overridden with an ACR build argument.
ARG COPILOT_CLI_VERSION=1.0.95
RUN npm install -g "@github/copilot@${COPILOT_CLI_VERSION}" paper-search-mcp-nodejs

# Create workspace directory
RUN mkdir -p /workspace/.github/agents /workspace/.github/skills /workspace/.copilot /workspace/reports

# Initialize workspace as a git repo (copilot CLI discovers agents from git root)
RUN git -C /workspace init

WORKDIR /app

# Install Python dependencies
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend code
COPY backend/ ./backend/

# Copy frontend
COPY frontend/ ./frontend/

# Copy agents, skills, and tools into workspace (dirs may be empty if submodule not available)
COPY workspace/.github/agents/ /workspace/.github/agents/
COPY workspace/.github/skills/ /workspace/.github/skills/
COPY workspace/tools/ /workspace/tools/

# Copy MCP server configuration
COPY workspace/.copilot/ /workspace/.copilot/

# Install tool dependencies if present
RUN if [ -f /workspace/tools/job-scanner/requirements.txt ]; then pip install --no-cache-dir -r /workspace/tools/job-scanner/requirements.txt; fi
RUN if [ -f /workspace/tools/news-scanner/requirements.txt ]; then pip install --no-cache-dir -r /workspace/tools/news-scanner/requirements.txt; fi
RUN if [ -f /workspace/tools/sleeper-scanner/requirements.txt ]; then pip install --no-cache-dir -r /workspace/tools/sleeper-scanner/requirements.txt; fi
RUN if [ -f /workspace/tools/earnings-calendar-scanner/requirements.txt ]; then pip install --no-cache-dir -r /workspace/tools/earnings-calendar-scanner/requirements.txt; fi
RUN if [ -f /workspace/tools/card-generator/requirements.txt ]; then pip install --no-cache-dir -r /workspace/tools/card-generator/requirements.txt; fi
RUN if [ -f /workspace/tools/youtube-ranking-scanner/requirements.txt ]; then pip install --no-cache-dir -r /workspace/tools/youtube-ranking-scanner/requirements.txt; fi

# The card-generator tool renders Jinja2 templates to PNG via headless Chromium.
# Install Chromium + its system libraries (libglib2.0-0, libnss3, libdbus-1-3,
# etc.) at build time as root so the non-root `appuser` can launch the browser
# at runtime without needing sudo. PLAYWRIGHT_BROWSERS_PATH puts the browser
# binaries in a world-readable location instead of /root/.cache.
#
# Without this step, card-generator silently falls back to a Pillow-based
# renderer that's hardcoded to a subset of card types — visibly fewer cards
# get produced than the cards-config declares.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN if [ -f /workspace/tools/card-generator/requirements.txt ]; then \
        mkdir -p /opt/playwright && \
        playwright install --with-deps chromium && \
        chmod -R a+rX /opt/playwright ; \
    fi

# The children's book engine (storybook-creator agent) needs its locked npm
# packages and the Chromium build its own Playwright version expects.
# --ignore-scripts skips ffmpeg-static's download: narrated video export needs
# macOS voices, so it never runs in this container.
RUN if [ -f /workspace/tools/childrens-books/package-lock.json ]; then \
        cd /workspace/tools/childrens-books && \
        npm ci --ignore-scripts --no-audit --no-fund && \
        mkdir -p /opt/playwright && \
        npx playwright install --with-deps chromium && \
        chmod -R a+rX /opt/playwright ; \
    fi

# gh auth: at runtime, GH_TOKEN env var will authenticate automatically
# Create a gh wrapper that uses GH_REPO_TOKEN for repo operations
# The real gh is at /usr/bin/gh; wrapper goes in /usr/local/bin (higher PATH priority)
RUN printf '#!/bin/sh\nGH_TOKEN="${GH_REPO_TOKEN:-$GH_TOKEN}" exec /usr/bin/gh "$@"\n' \
    > /usr/local/bin/gh && chmod +x /usr/local/bin/gh

# Configure git to use GH_REPO_TOKEN for HTTPS auth to GitHub
RUN git config --system credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_REPO_TOKEN:-$GH_TOKEN}"; }; f'

ENV WORKSPACE_DIR=/workspace
ENV AGENTS_DIR=.github/agents
ENV SKILLS_DIR=.github/skills

# Create non-root user and set ownership
RUN useradd -m -s /bin/bash appuser && \
    chown -R appuser:appuser /workspace /app && \
    mkdir -p /home/appuser/.npm && chown -R appuser:appuser /home/appuser/.npm

EXPOSE 8000

# Run as non-root user
USER appuser

# Run from the backend directory so imports work
WORKDIR /app/backend
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
