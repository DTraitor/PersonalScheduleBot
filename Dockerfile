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
    curl build-essential gcc locales \
    && rm -rf /var/lib/apt/lists/* \
    && echo "uk_UA.UTF-8 UTF-8" >> /etc/locale.gen \
    && locale-gen \
    && update-locale LANG=uk_UA.UTF-8

ENV LANG=uk_UA.UTF-8 \
    LANGUAGE=uk_UA:uk \
    LC_ALL=uk_UA.UTF-8

# Install Poetry
RUN curl -sSL https://install.python-poetry.org | python3 -

ENV PATH="$POETRY_HOME/bin:$PATH"

WORKDIR /app

# ---- Install dependencies ----
COPY pyproject.toml poetry.lock* ./
RUN poetry install --no-root

# ---- Copy source code ----
COPY . .

# Install your bot package (if structured as a Poetry package)
RUN poetry install --only-root

# ---- Run bot ----
CMD ["python", "-m", "personalschedulebot"]
