# Recovering album upload verification

An upload acknowledgement and a successful thumbnail read are separate events.
Previously, a failed first thumbnail read left an album upload permanently
unverified: later polls checked a fingerprint that had never been saved.

Album verification now uses Samsung's batch thumbnail transport, also for a
single image. This is compatible with older Frames whose single-thumbnail
requests fail. It distinguishes an unavailable read from a demonstrated identity
mismatch: a temporary outage preserves a previously verified claim rather than
causing another upload.

## Recovering an unverified acknowledgement

- Require a fresh thumbnail from the TV, not the HTTP thumbnail cache, and a
  safe pairing token that remains unchanged throughout the read.
- Require the same cached source version, acknowledged content ID and current
  album mapping. Recheck these inside the final database transaction.
- Compare the cached photo with the TV thumbnail, allowing scale, centered black
  padding, Samsung's standard fill crop and small JPEG/resampling differences.
  Samsung thumbnails can be noncentrally cropped, even for portrait uploads.
  The fill geometry must preserve a complete source axis and at least 25% of its
  area; only its position along the other axis varies, with at most 513 offsets
  and seven subpixel refinements around the single best position.
  Require the same tight RGB agreement globally and in every spatial tile. Do
  not search arbitrary two-axis patches, zoom levels, rotations or stretches,
  or treat a small perceptual hash, content ID or inventory entry as proof.
- Low-detail, tiny, corrupt, excessively cropped or nonmatching thumbnails remain
  uncertain. This conservative visual comparison is not a full-resolution
  cryptographic identity guarantee; false negatives intentionally defer recovery.
- Only after the checks pass, atomically save the pairing-bound SHA-256 thumbnail
  fingerprint and remove that exact unverified receipt. Ordinary subsequent
  verification compares the fresh thumbnail checksum with this baseline.

New unverified acknowledgements also retain the pre-upload pairing-token hash
in an owner-private companion table. A changed or initially unavailable pairing
epoch cannot be silently rebound. Old five-column receipt tables remain intact
and usable by the previous release; legacy receipts need positive source-image
evidence rather than an invented historical token hash.

Recovery runs during the normal album poll, after Art Mode is positively
confirmed, or when explicitly sending a cached photo to the TV. Reconciliation
itself never uploads, deletes or selects artwork. It does not manufacture a
new-photo event or a Daily Art hold. The usual final Art Mode guards remain on
every upload, selection and cleanup operation.

If evidence cannot be established, the warning remains and the upload is not
blindly repeated. Do not clear receipts, adopt old thumbnail caches, or delete
TV files merely to make the status green. Inspect the actual evidence first.
