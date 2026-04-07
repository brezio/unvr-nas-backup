FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-client \
        cron \
        curl \
        ca-certificates \
        procps \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Install AWS CLI v2 — pick the right binary for the build platform
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

# Install unifi-protect-remux v4.1.4 — pick the right binary for the build platform
RUN case "${TARGETARCH}" in \
        amd64) REMUX_ARCH="x86_64" ;; \
        arm64) REMUX_ARCH="aarch64" ;; \
        *)     echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL "https://github.com/petergeneric/unifi-protect-remux/releases/download/v4.1.4/unifi-protect-remux-linux-${REMUX_ARCH}.tar.gz" \
        -o /tmp/remux.tar.gz \
    && tar -xzf /tmp/remux.tar.gz -C /tmp \
    && mv /tmp/remux /usr/local/bin/remux \
    && chmod +x /usr/local/bin/remux \
    && rm -f /tmp/remux.tar.gz

COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY scripts/backup.sh /usr/local/bin/backup.sh
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/backup.sh

RUN mkdir -p /staging /archive

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
