FROM python:3.14-slim
RUN pip install --no-cache-dir fastapi==0.143.1 uvicorn==0.54.0 pydantic==2.14.1 httpx==0.28.1 certifi==2026.7.22
COPY probe.py /opt/proof/probe.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /workspace
USER 65534:65534
