FROM python:3.12-slim

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install --no-install-recommends -y git openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY . .

RUN python -m pip install --no-cache-dir -e ".[dev]" -r requirements-train.txt

CMD ["python", "-m", "pytest", "-q"]
