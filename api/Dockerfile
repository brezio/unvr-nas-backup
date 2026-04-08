FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-client \
        python3 \
        curl \
        ca-certificates \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Install AWS CLI v2 (needed for S3 inventory queries in /api/backups)
ARG TARGETARCH
RUN case "${TARGETARCH}" in \
        amd64) AWS_ARCH="x86_64" ;; \
        arm64) AWS_ARCH="aarch64" ;; \
        *)     echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_ARCH}.zip" -o /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/awscliv2.zip /tmp/aws

COPY api.py /usr/local/bin/api.py
RUN chmod +x /usr/local/bin/api.py

RUN mkdir -p /archive /shared

CMD ["python3", "/usr/local/bin/api.py"]
