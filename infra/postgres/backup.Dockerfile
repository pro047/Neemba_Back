# pg-backup sidecar: pg_dump from the postgres base + aws cli for S3 upload.
# Baked at build time (not `apk add` at container start) so a registry/network
# hiccup at boot can't leave the backup loop dead.
FROM postgres:16-alpine
RUN apk add --no-cache aws-cli
