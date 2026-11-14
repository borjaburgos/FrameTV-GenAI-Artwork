<?php
// ResourceSpace pilot policy. NAS originals and approved renditions are also
// mounted read-only at the container boundary; these flags are defense in depth.
$syncdir = '/artwork-originals';
$staticsync_ingest = false;
$staticsync_ingest_force = false;
$autorotate_no_ingest = false;
$autorotate_ingest = false;
$staticsync_allow_syncdir_deletion = false;
$staticsync_delete_resources = false;
$search_engine_noindex = true;
$system_download_config = false;

// The official installer verifies its base URL from inside the container. Use
// http://127.0.0.1 (internal port 80) during setup, then this canonical external
// address after the documented restart. Do not place credentials in the URL.
$frameart_pilot_base_url = getenv('RESOURCESPACE_BASE_URL');
if ($frameart_pilot_base_url !== false && trim($frameart_pilot_base_url) !== '') {
    $frameart_pilot_base_url = trim($frameart_pilot_base_url);
    $frameart_pilot_url_parts = parse_url($frameart_pilot_base_url);
    if (
        filter_var($frameart_pilot_base_url, FILTER_VALIDATE_URL) === false ||
        $frameart_pilot_url_parts === false ||
        !in_array($frameart_pilot_url_parts['scheme'] ?? '', ['http', 'https'], true) ||
        empty($frameart_pilot_url_parts['host']) ||
        isset($frameart_pilot_url_parts['user']) ||
        isset($frameart_pilot_url_parts['pass']) ||
        isset($frameart_pilot_url_parts['query']) ||
        isset($frameart_pilot_url_parts['fragment'])
    ) {
        throw new RuntimeException('ResourceSpace base URL must be a plain HTTP(S) server URL');
    }
    $baseurl = rtrim($frameart_pilot_base_url, '/');
}

// Never run StaticSync automatically in this pilot. Only generated TV JPEGs are
// uploaded via multipart API; originals are provenance links, not managed assets.
$valid_upload_paths = [];
