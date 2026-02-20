FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-client \
        cron \
        curl \
        ca-certificates \
        jq \
        procps \
    && rm -rf /var/lib/apt/lists/*

# Install unifi-protect-remux v4.1.4
RUN curl -fsSL "https://github.com/petergeneric/unifi-protect-remux/releases/download/v4.1.4/unifi-protect-remux-linux-x86_64.tar.gz" \
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
