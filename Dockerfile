# ---- Base image ----
FROM python:3.13-slim

# Environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1 \
    POETRY_HOME="/opt/poetry"

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl build-essential gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Poetry
RUN curl -sSL https://install.python-poetry.org | python3 -

ENV PATH="$POETRY_HOME/bin:$PATH"

WORKDIR /app

# ---- Install dependencies ----
COPY pyproject.toml poetry.lock* ./
RUN poetry install --no-root --no-dev

# ---- Copy source code ----
COPY . .

# Install your bot package (if structured as a Poetry package)
RUN poetry install --only-root

# ---- Run bot ----
CMD ["python", "-m", "telegram_bot"]
