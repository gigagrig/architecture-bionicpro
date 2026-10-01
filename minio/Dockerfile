# Build pinned upstream sources: published MinIO binary/image archives may disappear.
FROM golang:1.24.6-alpine AS server-build
RUN --mount=type=tmpfs,target=/tmp GOMAXPROCS=2 CGO_ENABLED=0 GOCACHE=/tmp/go-cache \
    go install -p 2 github.com/minio/minio@RELEASE.2025-04-22T22-12-26Z \
    && rm -rf /go/pkg/mod

FROM golang:1.24.6-alpine AS client-build
RUN --mount=type=tmpfs,target=/tmp GOMAXPROCS=2 CGO_ENABLED=0 GOCACHE=/tmp/go-cache \
    go install -p 2 github.com/minio/mc@RELEASE.2025-04-16T18-13-26Z \
    && rm -rf /go/pkg/mod

FROM alpine:3.22 AS server
RUN apk add --no-cache ca-certificates curl && adduser -D -u 10002 minio && mkdir /data && chown minio /data
COPY --from=server-build /go/bin/minio /usr/local/bin/minio
USER minio
EXPOSE 9000
ENTRYPOINT ["minio"]

FROM alpine:3.22 AS client
RUN apk add --no-cache bash ca-certificates
COPY --from=client-build /go/bin/mc /usr/local/bin/mc
ENTRYPOINT ["mc"]
