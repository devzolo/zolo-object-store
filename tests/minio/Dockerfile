# Official MinIO source release RELEASE.2025-10-15T17-29-55Z.
# Build tools and service remain inside Docker; both bases and source are pinned.
FROM golang:1.25.1-bookworm@sha256:c423747fbd96fd8f0b1102d947f51f9b266060217478e5f9bf86f145969562ee AS build
WORKDIR /src
RUN git init . && git remote add origin https://github.com/minio/minio.git && git fetch --depth 1 origin 9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a && git checkout --detach FETCH_HEAD
RUN CGO_ENABLED=0 go build -mod=readonly -trimpath -o /minio .
FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251
LABEL org.opencontainers.image.source="https://github.com/minio/minio" org.opencontainers.image.revision="9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a"
COPY --from=build /minio /usr/local/bin/minio
ENTRYPOINT ["/usr/local/bin/minio"]
