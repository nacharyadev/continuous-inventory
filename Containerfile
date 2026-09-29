FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN addgroup --system app && adduser --system --ingroup app app
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
RUN mkdir /data && chown app:app /data

USER app
EXPOSE 8765
CMD ["continuous-inventory", "serve-demo", "--host", "0.0.0.0", "--port", "8765", "--workspace", "/data"]
