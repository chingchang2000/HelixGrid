FROM rust:1.89-bookworm AS build
WORKDIR /src
COPY Cargo.toml ./
COPY src ./src
RUN cargo build --release --locked || cargo build --release

FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/target/release/helix-worker /usr/local/bin/helix-worker
RUN useradd --system --uid 10001 --create-home helix
USER helix
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/helix-worker"]
