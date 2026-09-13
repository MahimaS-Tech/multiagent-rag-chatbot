FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
ARG INSTALL_LANGGRAPH=false
COPY requirements.txt requirements-langgraph.txt ./
RUN pip install --no-cache-dir -r requirements.txt && \
    if [ "$INSTALL_LANGGRAPH" = "true" ]; then pip install --no-cache-dir -r requirements-langgraph.txt; fi && \
    groupadd --gid 10001 rag && useradd --uid 10001 --gid rag --create-home rag && \
    chown rag:rag /app
COPY --chown=rag:rag app/ ./app/
COPY --chown=rag:rag sample_data/ ./sample_data/

FROM base AS test
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY --chown=rag:rag conftest.py pyproject.toml ./
COPY --chown=rag:rag tests/ ./tests/
COPY --chown=rag:rag integration_tests/ ./integration_tests/
USER 10001:10001
CMD ["python", "-m", "pytest", "-q"]

FROM base AS runtime
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "100"]
