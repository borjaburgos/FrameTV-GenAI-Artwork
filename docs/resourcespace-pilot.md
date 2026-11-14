# ResourceSpace artwork catalog and Daily Art

This is an optional, local-only artwork catalog alongside FrameArt. It does not
replace the existing generated-art library. Browsing and metadata refresh do not
change TV playback. The importer defaults to the original 20-work pilot, while
an explicit expected count supports full annual collections up to 1,000 works.
Sending artwork and Daily Art scheduling are separate, opt-in FrameArt controls.

## Storage and safety boundaries

- Permanent originals belong on the NAS, under the household's `Media/Artwork`
  library. Set the actual mounted directory explicitly; do not assume a NAS path
  is already mounted on the FrameArt host.
- ResourceSpace receives **read-only** mounts for originals and approved TV
  renditions. Docker refuses to create missing host paths.
- ResourceSpace manages **copies of the approved TV JPEGs only** (20 in the
  initial pilot, 365 in an annual library). Metadata links each to the original's relative path
  and verified SHA-256. Originals are **not** uploaded, moved, indexed by
  StaticSync, or re-encoded. Do not describe this as original-file ingestion.
- ResourceSpace's SQL database, installer configuration and writable managed
  filestore use separate local volumes. They are not placed on the NAS mount.
- The importer opens input files read-only. It verifies all checksums before
  network access, immediately before uploading each rendition, and after import.
- No scheduled StaticSync job is added. Missing NAS files cause import validation
  to fail, never deletion of catalog entries or images. The safety policy also
  disables ingest, autorotation, source deletion and missing-file reconciliation.
- The database is not exposed on a host port. ResourceSpace listens on loopback
  by default; publish it through an existing LAN/Tailscale reverse proxy only
  after setup. There is no need to expose it to the public Internet.

This makes the service useful before choosing a permanent catalog host. A test
with local fixture/original directories validates the application but **does not
prove that the NAS share has been deployed or mounted read-only**. Record that
distinction in the pilot handoff.

## Start the isolated catalog

Prerequisites: Docker Engine with Compose v2, a reviewed checkout of the official
[ResourceSpace Docker repository](https://github.com/resourcespace/docker), and
existing NAS/rendition directories. Record the upstream checkout commit and the
resolved image digests in deployment notes. The upstream Dockerfile currently
builds ResourceSpace 11.0; review it before building. The MariaDB 11.4 LTS tag is
deliberately version-scoped but still receives patch updates; pin its resolved
digest in a permanent deployment.

The official Docker source reviewed for this pilot was commit
`07dbaffd74d22750a985e904ae86ea3f0ea4a221`. Set
`RESOURCESPACE_DOCKER_SOURCE` to that reviewed checkout, not an unrelated directory.

Create two different strong database passwords in private files outside this
repository. Restrict their parent directory and file permissions. Do not reuse
FrameArt, NAS, network-admin or Proxmox credentials.

Copy `deploy/resourcespace/.env.example` into a private environment file and set
absolute paths for the reviewed upstream Docker checkout, NAS originals, TV
renditions and both password files. Check the NAS mount's identity and availability
before starting; an ordinary empty local directory is not a valid stand-in.
Set `RESOURCESPACE_BASE_URL` to the canonical ResourceSpace URL that the FrameArt
server can reach; it defaults to `http://127.0.0.1:8187`. If changing the mapped
port, change this URL too. It must be HTTP(S), without credentials/query/fragment.
`RESOURCESPACE_BIND_ADDRESS` defaults to loopback. On an explicitly approved,
dedicated private VM, it may be set to that VM's private IP so the existing reverse
proxy and FrameArt server can reach it. Do not use `0.0.0.0` as the default.

```bash
docker compose --env-file /private/path/resourcespace.env \
  -f deploy/resourcespace/compose.yaml config --quiet
docker compose --env-file /private/path/resourcespace.env \
  -f deploy/resourcespace/compose.yaml up --build -d
```

Visit `http://127.0.0.1:8187` through a local browser or SSH tunnel. Complete the
official installer using database host `mariadb`, database `resourcespace`, user
`resourcespace_rw` and its private password. Leave the MySQL binary path empty.
For the installer's initial base URL, use **`http://127.0.0.1`** (internal port 80).
The installer checks this address from inside the container; the host's mapped
port is not available there. Do not force the obsolete `general_structure` template;
use the default schema unless an actually installed template has been reviewed.
Choose a new admin password; keep it in the household password manager. Then
restart the ResourceSpace container so the persistent installer configuration
loads `safety.php` and applies `RESOURCESPACE_BASE_URL` as the canonical address:

```bash
docker compose --env-file /private/path/resourcespace.env \
  -f deploy/resourcespace/compose.yaml restart resourcespace
```

Verify both mounts show `RW: false` in `docker inspect`, and inspect the loaded
PHP policy before any import. Prefer an inspection of mount permissions over
creating a test file in the originals directory.

The upstream image includes its normal daily maintenance cron. This pilot does
not add StaticSync or any NAS reconciliation cron. Do not run `staticsync.php`
manually: automatic alternative-file discovery requires ingest mode, which would
violate the pilot's source-preservation model.

## Portable input manifest

Keep this manifest and the catalog metadata with the NAS library, not solely in
ResourceSpace's database. Exactly 20 `items` are required by default; pass an
explicit `--expected-count` when expanding. Every item has stable
identity, explicit rights, an AI flag, original/rendition paths and both checksums:

```json
{
  "schema_version": 1,
  "collection_name": "FrameArt — 20 artwork pilot",
  "items": [
    {
      "original_path": "museum/source-file.jpg",
      "rendition_path": "museum/full-frame-tv.jpg",
      "original_sha256": "<64 hex characters>",
      "rendition_sha256": "<64 hex characters>",
      "metadata": {
        "schema_version": 1,
        "id": "stable-artwork-id",
        "title": "Verified title",
        "artist": {
          "id": "stable-artist-id",
          "name": "Verified artist name",
          "bio": "Optional verified short biography",
          "source_url": "https://example.org/verified-artist-record"
        },
        "date": "1906",
        "medium": "Oil on canvas",
        "description": "Optional sourced artwork context",
        "source_url": "https://example.org/original-artwork-record",
        "license": "Public Domain (CC0)",
        "ai_generated": false,
        "tags": ["landscape", "spring"],
        "width": 3840,
        "height": 2160
      }
    }
  ]
}
```

The example illustrates one item; a real pilot must contain 20 unique artwork IDs
and separate rendition files. Paths must be relative to their respective roots;
absolute paths, traversal and symlinks escaping those roots are rejected. Each TV
file must be an actual 3840×2160 RGB JPEG. This checks dimensions, not the aesthetic
quality of a crop; visually review the full-frame renditions separately.

Artwork dates are not photo-publication dates. Retain the latter separately as
`source_publication_date`. Do not invent missing artist biographies or grant
public-domain status to modern works. AI originals must have `ai_generated: true`
and can carry `ai_provenance` text (structured provenance is serialized to JSON
text). Keep generation tool/prompt provenance separate from a real artist credit.

## Validate, then import

Run from the project root with the FrameArt Python environment (Pillow and httpx2
are already project dependencies). Use module invocation so Python loads this
checkout's shared metadata validator, not an older installed copy. Dry-run is the
default: it reads files but makes no HTTP requests.

```bash
python -m scripts.resourcespace_pilot \
  --manifest /path/to/pilot.json \
  --originals-root /absolute/path/to/nas/originals \
  --renditions-root /absolute/path/to/tv-4k \
  --dry-run
```

For the initial import, use a dedicated bootstrap/admin ResourceSpace identity.
Set `RESOURCESPACE_PILOT_URL`, `RESOURCESPACE_PILOT_USER` and
`RESOURCESPACE_PILOT_API_KEY` in the invoking process's private environment, not
command-line arguments or committed config. Set the URL to the base installation,
without `/api/`. Repeat the validated command with `--apply` instead of `--dry-run`.

The importer creates a named collection, the `frameart_id` field, a canonical
`frameart_metadata` JSON field and readable artist/date/medium/license/source
fields. It fills the default title and caption. It uploads only the generated TV
JPEG bytes through multipart API, **never** a server-side `file_path` import (which
can move files). It uses stable IDs to resume safely and refuses foreign artworks,
ambiguous duplicates or changed file hashes. It does not delete resources or
replace an existing image. A failed import can leave partial catalog records;
correct the cause and rerun the same manifest rather than deleting originals.
ResourceSpace 11 generates shortnames without underscores (`frameartid`,
`frameartmetadata`, etc.). The importer and runtime reader normalize these known
aliases; existing canonical shortnames also work. Ambiguous duplicate definitions
are rejected rather than silently selected.

Save the successful JSON report in the private deployment record. It contains the
ResourceSpace collection ID and resource IDs, no credentials. The script also
confirms source checksums after import. Do not configure FrameArt until the report
confirms the expected resource count and the real catalog previews have been reviewed.

## Expand an existing pilot to 365

Validate the complete portable manifest against the read-only source roots first:

```bash
python -m scripts.resourcespace_pilot \
  --manifest /path/to/Artwork/collections/artwork-metadata-365.json \
  --originals-root /path/to/Artwork/originals \
  --renditions-root /path/to/Artwork/renditions \
  --expected-count 365 \
  --collection-name 'FrameArt — 20 artwork pilot' \
  --dry-run
```

The collection-name override deliberately expands the already shared pilot
collection without editing the NAS manifest or changing the reader's scope.
Use the actual existing collection name, not a guessed ID. The name can remain
historical; it does not limit the catalog's size. Repeat with `--apply` only
after validation and explicit approval for the catalog import. Existing stable
artwork IDs and matching checksums are reused; rerunning does not duplicate
uploads. The runtime reader still has no upload, edit or delete permissions.

FrameArt requests bounded `offset,count` pages and validates ResourceSpace's
reported total, ordering, identities and page lengths. A truncated, duplicate,
changing or over-limit response never replaces the previous cached snapshot.
The refresh API returns `202`; poll cached `GET /art-catalog` for its `sync`
progress. Neither polling nor browsing scans the NAS or sends commands to TVs.

## Daily Art calendar and delivery

Import a calendar in **Modes → Daily Art** using this schema (512KiB maximum):

```json
{
  "year": 2027,
  "timezone": "America/New_York",
  "entries": [
    {"date": "2027-01-01", "artwork_id": "stable-artwork-id"},
    {"date": "2027-01-02", "artwork_id": "another-stable-artwork-id"}
  ]
}
```

The example is a partial two-day calendar. A full annual file contains 365 dates,
or 366 for a leap year, with unique dates and artwork IDs within that year.
All referenced artwork must already be cached in FrameArt. Optional entry fields
are `month`, `day_of_year`, `season` and `occasion`; dates must match the declared
year. A 2027 calendar cannot authorize a 2026 selection. Adjacent years can coexist;
importing one replaces only that year's calendar. Plan movable holidays for the
actual year rather than copying their month/day from another year.

Select a configured TV/group, local timezone and change time, then explicitly
enable Daily Art. Configuration and calendar writes require an admin token.
Family albums can share a TV with Daily Art without changing their settings.
A genuinely new album photo wins over that TV's daily artwork: priority is
recorded before delivery, and shared per-TV arbitration prevents stale in-flight
or hourly daily retries from overwriting it. Once the photo is safely shown,
the hold lasts until the next local midnight, not a fixed 24 hours. If the TV is
being watched, the photo stays pending and retains priority across midnight.
Unchanged polls or falling back to an already known older photo do not renew
priority. Clearing manual holds never clears family-album priority. Detection
uses the existing polling interval; it is not an instant iCloud push.

An enabled overlapping sports feed still blocks Daily Art. No mode is silently disabled.

The durable worker considers only today's due artwork. Busy, offline and unknown
TV states defer it; they do not advance a playlist or wake the TV. The default
change time is local midnight; unsuccessful TVs retry on a fixed hourly cadence.
Successful group members are skipped on retry. A committed but uncertain dispatch is held
instead of being blindly repeated after a timeout or restart. An uncertain
per-TV selection is checked read-only each hour; only a known matching image
and confirmed TV state can authorize a safe retry. Unknown uploads remain held.
Pause/target/manual
hold changes are rechecked before upload and final selection. A manual FrameArt
selection wins for the rest of the local day; clearing a hold is not a command
to replay an already confirmed daily selection.

Only full-frame 3840×2160 RGB JPEG TV renditions are delivered, with matte `none`.
ResourceSpace downloads use the configured origin, bounded bytes, no redirects
and redacted transport logging. The browser receives only cached local previews,
never signed rendition URLs or a ResourceSpace key. Local full-resolution caching
is bounded to eight renditions, separate from NAS originals.

Daily retention considers only files uploaded and still verifiably owned by this
mode. It never deletes unrelated photos, manual selections, the actual current
image, or files with unknown identity. A factory-reset/recycled content ID is
not ownership proof. The application's actual per-TV pairing and image identity
checks must pass before reusing a mapping or deleting an old owned upload.

## Connect FrameArt

Create a second ResourceSpace account for FrameArt with **search/view/download
only**, access to this collection and the relevant metadata fields, and no upload,
edit, delete, admin or user-management capabilities. Private collections must be
shared explicitly with that account/group. Do not use the bootstrap key at runtime.
Use a dedicated group rather than assuming the default General Users group is
read-only: it can include contribution/upload permissions. The isolated acceptance
reader used `s,g,f*,D,noex,nolock`, without `a,t,u,c,d,e0,e1,A`, and the pilot
collection was explicitly shared with it. Review access again before adding other
private collections to the same ResourceSpace instance.

Configure FrameArt's ResourceSpace base URL, reader username/API key and the pilot
collection ID using its private environment or YAML settings. Environment names are
`FRAMEART_RESOURCESPACE_URL`, `FRAMEART_RESOURCESPACE_USER`,
`FRAMEART_RESOURCESPACE_API_KEY`, and `FRAMEART_RESOURCESPACE_COLLECTION`. The
optional `FRAMEART_RESOURCESPACE_TIMEOUT` is 10 seconds by default. These values
are operator-managed configuration in this first pilot, not a browser key editor.
Restart FrameArt to load the configuration, then use Art Catalog's explicit
synchronization action. All API access happens server-side; the browser never
receives the API key or signed asset URLs. FrameArt
reads the catalog metadata and caches previews. An unavailable ResourceSpace or NAS
must not clear the last good catalog or change a television.

FrameArt's Art Catalog displays artist information, artwork details, source/rights
and AI provenance. The TV images remain clean and full-frame. Readable fields in
ResourceSpace override nonempty matching values in the canonical JSON. To clear an
optional value, edit the canonical JSON as well; blank display fields alone are
treated as absent. Household ratings and device scheduling remain owned by FrameArt.

## Acceptance and recovery

Before expanding past the pilot, verify:

1. Exactly 20 genuine artworks appear in ResourceSpace and FrameArt, including
   traditional, contemporary and explicitly labeled AI originals.
2. Artist/title/date/medium/source/license information survives import and one
   metadata edit is reflected after synchronization.
3. Both source mounts are read-only and original SHA-256 values are unchanged.
4. Re-running import creates no additional records or uploads.
5. Catalog service/NAS outage preserves cached information and never deletes art.
6. Browsing and refresh send no TV commands. Opt-in delivery and scheduling leave
   active viewing and unknown TV status untouched, including status changes
   between the initial check and the actual upload/selection connection.
7. Database/configuration/filestore backups and portable metadata exports are
   recoverable independently of FrameArt.

Stopping this isolated Compose project does not alter the NAS or existing FrameArt
library. Keep its named volumes for rollback/recovery; do not use `down --volumes`
unless intentionally discarding the pilot database, credentials and rendition copies.

Primary references: [official Docker installation](https://www.resourcespace.com/knowledge-base/systemadmin/install_docker),
[API signing and POST protocol](https://www.resourcespace.com/knowledge-base/api/),
[StaticSync](https://www.resourcespace.com/knowledge-base/systemadmin/staticsync),
[metadata fields](https://www.resourcespace.com/knowledge-base/api/create_resource_type_field),
[file upload API](https://www.resourcespace.com/knowledge-base/api/upload_file).
