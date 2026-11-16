FROM quay.io/fedora/fedora:latest

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

RUN dnf -y install --setopt=install_weak_deps=False \
        python3 \
        python3-pillow \
        ghostscript \
        poppler-utils \
        psutils \
        libreoffice-writer \
        cups-libs \
        fontconfig \
        google-noto-sans-cjk-fonts \
    && dnf clean all \
    && rm -rf /var/cache/dnf

WORKDIR /app
COPY app/ /app/
COPY rastertokpsl_A3 /app/rastertokpsl_A3
RUN chmod +x /app/rastertokpsl_A3

RUN mkdir -p /data/jobs

EXPOSE 8080

ENTRYPOINT ["python3", "-u", "/app/server.py"]
