# Runtime image: everything is prebuilt by `harness build` into out/.
# Build context is the repo root, so paths below are repo-relative.
FROM ubuntu:24.04
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates iproute2 libpq5 && \
    rm -rf /var/lib/apt/lists/*
# logoscore portable bundle (bin/ lib/ modules/) and the modules dir the daemon
# scans: logoscore's built-ins plus delivery_module, libp2p_module, openmetrics.
COPY out/logoscore /opt/logoscore
COPY out/modules   /opt/modules
# The logosdeliverynode binary keeps its nix closure (glibc, openssl, ... via rpath); libpq
# is dlopen-ed at run time, hence the apt package above.
COPY out/seed-store/ /nix/store/
COPY out/seed-path   /opt/sim/seed-path
RUN ln -s "$(cat /opt/sim/seed-path)/bin/logosdeliverynode" /usr/local/bin/logosdeliverynode
COPY docker/entrypoint-module.sh /opt/sim/entrypoint-module.sh
# Every node profile a group can name; the group's `config` picks one.
COPY conf/ /opt/sim/conf/
ENV PATH=/opt/logoscore/bin:$PATH
