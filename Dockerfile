# syntax=docker/dockerfile:1
# Override PYTHON_IMAGE to build from a registry mirror; both stages must use the
# same image because the virtualenv is copied between them.
ARG PYTHON_IMAGE=python:3.14-slim

FROM ${PYTHON_IMAGE} AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m venv /opt/venv && /opt/venv/bin/pip install .

FROM ${PYTHON_IMAGE}
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN groupadd --system --gid 10001 exporter \
    && useradd --system --uid 10001 --gid exporter --no-create-home --shell /usr/sbin/nologin exporter
COPY --from=build /opt/venv /opt/venv
USER 10001:10001
EXPOSE 9469
ENTRYPOINT ["gcp-capacity-exporter"]
CMD ["--config", "/etc/gcp-capacity-exporter/config.yaml"]
