FROM continuumio/miniconda3:24.11.1-0

# GROMACS from conda-forge. Override with: docker build --build-arg GROMACS_VERSION=2025.4 .
ARG GROMACS_VERSION=2025.4
ARG PYTHON_VERSION=3.12

RUN conda install -y -c conda-forge "python=${PYTHON_VERSION}" "gromacs=${GROMACS_VERSION}" \
    && conda clean -afy \
    && gmx --version | head -3

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY scripts/ ./scripts/
COPY examples/ ./examples/
COPY configs/ ./configs/
# sample.tpr is version specific, so build it with this image's GROMACS
RUN python scripts/make_example_data.py --tpr-only && chmod -R a+rX /app

# Runtime files (jobs, results, logs) live in /data. The container is usually started with
# --user `id -u`:`id -g`, so /data is world writable with the sticky bit.
RUN useradd --create-home --uid 1000 mcp \
    && mkdir -p /data && chmod 1777 /data
ENV GMX_MCP_HOME=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER mcp

HEALTHCHECK --interval=60s --timeout=20s --retries=2 \
    CMD gmx --version > /dev/null 2>&1 || exit 1

CMD ["python", "src/server.py"]
