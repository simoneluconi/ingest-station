# Test NFS server (Docker)

An NFSv4 server for testing the station without a NAS: NFS-Ganesha in a
container (user space, so it runs in Docker Desktop on Windows too). It
exports a Docker volume as `/ingest`, writable by root (like
`no_root_squash`).

```sh
docker build -t ingest-nfs-test tests/nfs-server
docker run -d --name ingest-nfs --privileged -p 2049:2049 -v ingest-nfs:/export ingest-nfs-test
```

On the station: *Settings → Destination*, NFS, server = this PC's address,
export path `/ingest`. Only NFS 4 is offered (port 2049): NFS 3 is reported
as not offered.

Inspect or check the copies on the server side:

```sh
docker exec ingest-nfs ls -la /export
docker exec ingest-nfs sh -c 'apt-get update -qq && apt-get install -y -qq xxhash'
docker exec ingest-nfs sh -c 'cd /export/<folder> && xxhsum -H128 -c <folder>.xxh128'
```

Known server bug: NFS-Ganesha 4.3 accepts NFS 4.2 but its reads fail
(READ_PLUS). The station detects it when negotiating the version and uses
NFS 4.1; a forced NFS 4.2 would copy fine and fail at the full verification.

Remove: `docker rm -f ingest-nfs && docker volume rm ingest-nfs`.
