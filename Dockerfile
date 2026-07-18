FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY delapan ./delapan

RUN pip install --no-cache-dir -e ".[cloud]"

EXPOSE 8000

CMD ["python", "-m", "delapan.mcp.cloud_server"]
