FROM python:3.12-slim

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    gnupg \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Install GitHub Copilot CLI (standalone)
RUN npm install -g @github/copilot

# Create workspace directory
RUN mkdir -p /workspace/.github/agents /workspace/.github/skills

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

# Copy agents and skills into workspace
COPY workspace/.github/agents/ /workspace/.github/agents/
COPY workspace/.github/skills/ /workspace/.github/skills/

# gh auth: at runtime, GH_TOKEN env var will authenticate automatically
ENV WORKSPACE_DIR=/workspace
ENV AGENTS_DIR=.github/agents
ENV SKILLS_DIR=.github/skills

# Create non-root user and set ownership
RUN useradd -m -s /bin/bash appuser && \
    chown -R appuser:appuser /workspace /app

EXPOSE 8000

# Run as non-root user
USER appuser

# Run from the backend directory so imports work
WORKDIR /app/backend
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
