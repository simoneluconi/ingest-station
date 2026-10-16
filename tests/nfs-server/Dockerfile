FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        nfs-ganesha nfs-ganesha-vfs rpcbind dbus \
    && rm -rf /var/lib/apt/lists/*
COPY ganesha.conf /etc/ganesha/ganesha.conf
EXPOSE 2049
CMD mkdir -p /export /run/ganesha /run/dbus && \
    (dbus-daemon --system --fork || true) && \
    (rpcbind -w || true) && \
    exec ganesha.nfsd -F -L /dev/stdout -f /etc/ganesha/ganesha.conf
