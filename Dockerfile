FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 PYTHON_CPU_COUNT=2 SKIP_INTEGRATION=0 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
ENV OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
WORKDIR /app
COPY requirements.lock fetch_upstream.py ./
RUN python -m pip install --no-cache-dir -r requirements.lock && python fetch_upstream.py /opt/sqlglot
COPY build_index.py observations.py dependencies.py ./
RUN python build_index.py /opt/sqlglot /opt/dependencies.json && chmod a+r /opt/dependencies.json
COPY index_store.py ./
RUN python index_store.py /opt/dependencies.json /opt/dependencies.sqlite
COPY selector.py patches.py replay.py schedule.py verify.py ./
USER 65534:65534
CMD ["python", "/app/selector.py"]
