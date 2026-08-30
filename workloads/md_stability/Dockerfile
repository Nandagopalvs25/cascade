FROM mambaorg/micromamba:2.0-debian12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MAMBA_DOCKERFILE_ACTIVATE=1 \
    OPENMM_CPU_THREADS=8 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    PATH=/usr/local/nvidia/bin:$PATH \
    LD_LIBRARY_PATH=/usr/local/nvidia/lib64

COPY --chown=$MAMBA_USER:$MAMBA_USER environment.yml /tmp/environment.yml
RUN micromamba install -y -n base -f /tmp/environment.yml \
    && micromamba clean --all --yes

WORKDIR /app

COPY --chown=$MAMBA_USER:$MAMBA_USER *.py ./

ENTRYPOINT ["/usr/local/bin/_entrypoint.sh", "python", "run.py"]
