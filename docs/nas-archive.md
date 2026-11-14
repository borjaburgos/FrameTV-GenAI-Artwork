# Generated artwork NAS archive

This is an append-only **image archive**, not a mirror or a complete FrameArt
backup. It protects generated artwork against loss of the application host;
it does not replace NAS snapshots or an off-site backup. Application credentials,
configuration, databases, uploaded family photos and curated Daily Art are not
copied. ResourceSpace keeps its existing read-only mount and can catalog this
separate collection later.

## Storage and metadata

NAS layout: `/Media/Artwork/Generated/FrameArt/YYYY/MM/DD/<job-id>/`:

- `source.png`: exact original provider output (or original input for an edit).
- `edited.png`: exact raw provider edit, when available for new edits.
- `final.png`: exact full-frame TV derivative.
- `meta.json`: allowlisted generation metadata, including prompt, provider/model,
  dimensions, style and available generation parameters. Known credentials and
  signed URL query strings are redacted. Arbitrary provider responses, nested
  metadata, TV addresses, delivery errors and configuration are excluded.
- `manifest.json`: versioned generation identity, byte lengths and SHA-256 hashes.

Names preserve existing FrameArt storage conventions; `source.png` may contain
original JPEG/WebP bytes from a provider. No image is re-encoded. Historical
edits cannot recover raw provider edit bytes that older FrameArt never saved.
Historical parameters absent from metadata are not invented.

Linux is required for the archive's `renameat2(RENAME_NOREPLACE)` atomic
publication. There is no unsafe overwrite fallback. No additional copy tool or
LLM/API call is required.

## Failure and deletion guarantees

Generation only snapshots to the local `data_dir/archive/outbox` and SQLite queue;
it never probes or writes to the NAS. NAS I/O runs in a separate CLI process.
Enqueue errors are logged without failing generation; timer discovery retries
from completed artifacts. Both API deletion and CLI cleanup first ensure an
eligible generation has a durable local snapshot or verified receipt. A local
staging failure refuses that deletion. Pending images survive removal from the
working library and are retained until verified on the NAS.

The worker requires an existing CIFS/NFS mount with the exact configured source
and archive marker. It does **not** mount storage, create a missing destination,
or fall back to writing into an empty local mountpoint. It pins the opened NAS
directory for the transfer. Provision the mount with permissions limited to the
generated-art subtree; never reuse/broaden ResourceSpace's read-only identity.

Each file is staged and re-read for SHA-256 verification, then published with
atomic no-replace rename. Existing identical files are accepted; different bytes
fail visibly and are never overwritten. Manifest publication is last, and all
NAS files are re-read before recording a local verified receipt. Retries can
resume an interrupted generation. Only the worker's own unpublished temporary
transfer files are removed; it never deletes archived files or generations.
An abrupt process kill can leave an uncommitted `.frameart-partial-*` file;
inspect these separately, never use a blanket NAS cleanup.

One worker lock prevents overlapping runs. Failures retain the outbox and retry
after five minutes (or explicitly with `--retry-now`). The minute timer targets
copying ordinary new generations within five minutes while storage is available;
outages, very large backlogs and insufficient local disk space cannot promise
that latency. Monitor queue age and free disk space. The local outbox and
receipts are not protection against host loss before a successful NAS copy.

## Provision and enable

1. Create only `Media/Artwork/Generated/FrameArt` using an approved NAS control
   plane and a dedicated folder-scoped writer. Leave curated originals/readers
   unchanged. Mount that subtree at, for example, `/mnt/frameart-generated`.
   Do not put credentials in Git, command arguments or the image archive.
2. Create a non-secret identity marker `.frameart-archive-id` inside that NAS
   directory; its exact short identifier must match `archive_id`. The worker
   never creates or repairs the marker itself.
3. Set the following for both FrameArt and the separate worker, using the
   deployment's supported environment/configuration mechanism:

   ```ini
   FRAMEART_DATA_DIR=/data/frameart
   FRAMEART_ARCHIVE_ENABLED=true
   FRAMEART_ARCHIVE_PATH=/mnt/frameart-generated
   FRAMEART_ARCHIVE_MOUNT_SOURCE=//nas.example.home/Media
   FRAMEART_ARCHIVE_ID=your-dedicated-archive-identifier
   ```

   The mount source is the exact kernel mountinfo source, not a guessed hostname
   or merely a matching directory name. Keep NAS authentication in a protected
   OS mount credential file; no NAS password is an application setting.
4. Review/adapt `deploy/archive/frameart-archive.service` and `.timer` to the
   installation. Pre-create the local archive/log directories as the service
   identity. The template permits writes only to them and the dedicated NAS
   mount. Start the timer **after** the pilot passes. Inspect:
   `systemctl status frameart-archive.timer` and
   `journalctl -u frameart-archive.service`.

## Pilot, backfill and restore

Run against the real production **data directory** but never change live TV
playback. The restore destination must be a new directory outside live data.

```sh
frameart archive backfill --dry-run
frameart archive backfill --limit 1
frameart archive run --no-discover --limit 1
frameart archive status
frameart archive verify --key YYYY/MM/DD/job-id
frameart archive restore --key YYYY/MM/DD/job-id --output /var/tmp/frameart-restore-pilot
```

Compare original and restored image hashes, inspect the safe metadata and test
opening the restored images. Restore never changes the live library, database,
TV selection, Daily Art, album priorities or any NAS file. It refuses existing
destinations, symlinks, traversal, malformed manifests and checksum mismatches.
With the same archive configuration it also works from a fresh data directory
after host loss: the NAS manifest is self-contained; old local receipts are not
required.

After pilot acceptance:

```sh
frameart archive backfill
frameart archive run --no-discover --limit 250
frameart archive status
```

Enable the minute timer and FrameArt's enqueue configuration using the approved
deployment control plane. Verify a real completed generation is archived without
forcing a paid generation or a TV change merely for testing. `GET /archive/status`
and Settings → Storage show local verified/pending/failed counts, last verified
time and oldest pending time. Status requests never contact the NAS; a quiet
queue is not proof of current NAS availability. Settings highlights failed work
or backlog older than 15 minutes. A worker exits nonzero on copy/discovery errors
so systemd monitoring can detect failures as well.

Use `archive verify --key ...` for explicit later integrity checks. The ordinary
incremental worker does not repeatedly re-hash previously verified NAS entries.
Disabling the timer and `archive_enabled` stops future archive work without
deleting any local originals, pending snapshots, receipts or NAS contents.
