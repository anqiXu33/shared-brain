FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY server.py .

RUN useradd --create-home --uid 10001 app
USER app

# server.py listens on $PORT (default 8000) at /$MCP_SECRET_PATH.
# Secrets come from the environment at run time, never from the image:
#   docker run --env-file .env -p 8000:8000 shared-brain
EXPOSE 8000
CMD ["python", "server.py"]
