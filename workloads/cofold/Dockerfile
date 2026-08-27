FROM pytorch/pytorch:2.7.1-cuda12.6-cudnn9-runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PROTENIX_ROOT_DIR=/opt/protenix

RUN apt-get update \
    && apt-get install -y --no-install-recommends git g++ gcc libc6-dev make \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p ${PROTENIX_ROOT_DIR}

COPY *.py ./

ENTRYPOINT ["python", "run.py"]
