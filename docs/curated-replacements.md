# Dislike and replace curated artwork

Library → Curated artwork has a **👎 Dislike & replace** button on each card and
in artwork details. It immediately removes that work from the active collection
and queues a genuinely new replacement. An administrator or trusted local user
is required; an automation/read-only token cannot reject artwork.

The active count can temporarily fall below 365 while replacements are pending.
Each successful replacement adds exactly one new piece, returning the collection
to its previous size. Duplicate clicks are idempotent. Progress and retry status
are shown separately; neither an offline museum nor a failed request leaves a
button stuck in a loading state. Failed jobs retry automatically, with a manual
Retry now action. The queue and prepared images survive restarts.

## What “delete” means

- The rejected piece is removed from FrameArt's active catalog and preview cache.
- Its unshared local TV-rendition cache is removed by the background worker.
- Rejected locally acquired replacements also lose their image blobs.
- A small durable metadata/rejection record remains so metadata refresh cannot
  silently reintroduce the artwork, and to retain feedback evidence.
- ResourceSpace collection membership and read-only NAS masters are **not**
  changed. This feature never deletes a protected original or writes to the NAS.
- The currently displayed TV image and TV inventory are not changed by dislike.
  A delivery already in progress cannot be retroactively undone, but removed
  artworks are checked again before subsequent upload/selection operations.

## Replacement selection

Automatic replacements use the existing Art Institute of Chicago public-domain
integration. Rights are checked again immediately before download. Search terms
are coarse visual-profile genres and available seasonal tags, not household free
text. No paid image generator is invoked and no provider credentials are sent.

The replacement must be new by museum object identity and artist/title, with
additional source and rendition checksum deduplication for acquired images.
Previously rejected works are not eligible again. Candidates unsuitable for a
landscape display are skipped. The TV copy is an edge-to-edge 3840×2160 RGB JPEG,
cropped and scaled without padding or a matte. Source bytes, attribution, license,
thumbnail and the TV derivative are stored locally with FrameArt's data and are
included in full application backups. Automatic selection is an approximation of
the household profile, not a guarantee of subjective taste or native 4K detail.

Prepared images stay hidden until calendar reconciliation completes. Every
unattempted calendar slot for the rejected ID is redirected to the replacement.
Already attempted dates, dispatch history, manual/album holds, configured TVs and
Daily Art timing are preserved. Normal delivery retains its existing Art Mode
guards and family-album priority. A rejection itself never forces a TV change.

Metadata refresh merges the read-only ResourceSpace collection with the local
replacement overlay, filtering rejected IDs. This keeps 365 active pieces after
successful replacement without requiring a ResourceSpace writer credential or
changing NAS master permissions.

Museum integration reference: [Art Institute API documentation](https://api.artic.edu/docs/).
