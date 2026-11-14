"""Read-only artwork browsing and an explicit administrator cache refresh."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Request, Response

from frameart.art_catalog import (
    ArtCatalogStore,
    CatalogSyncBusyError,
    integration_configured,
    sync_catalog,
)
from frameart.resourcespace import ResourceSpaceError

_ARTWORK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


def create_art_catalog_router(settings_loader: Callable) -> APIRouter:
    """Mount behind FrameArt's existing authentication and admin middleware."""
    router = APIRouter()

    @router.get("/art-catalog")
    def catalog(response: Response):
        response.headers["Cache-Control"] = "no-store"
        settings = settings_loader()
        return ArtCatalogStore(settings.data_dir).snapshot(settings)

    @router.post("/art-catalog/sync")
    def refresh(response: Response):
        response.headers["Cache-Control"] = "no-store"
        settings = settings_loader()
        if not integration_configured(settings):
            raise HTTPException(
                503, "Configure the ResourceSpace server, reader key and collection first."
            )
        try:
            return sync_catalog(settings)
        except CatalogSyncBusyError:
            raise HTTPException(409, "An artwork catalog refresh is already running.") from None
        except ResourceSpaceError as exc:
            raise HTTPException(502, str(exc)) from None

    @router.get("/art-catalog/{artwork_id}/thumbnail")
    def thumbnail(artwork_id: str, request: Request):
        if not _ARTWORK_ID.fullmatch(artwork_id):
            raise HTTPException(400, "Invalid artwork ID.")
        settings = settings_loader()
        payload = ArtCatalogStore(settings.data_dir).thumbnail(artwork_id)
        if payload is None:
            raise HTTPException(404, "No cached preview is available for this artwork.")
        etag = '"' + hashlib.sha256(payload).hexdigest() + '"'
        headers = {"ETag": etag, "Cache-Control": "private, max-age=0, must-revalidate"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(content=payload, media_type="image/webp", headers=headers)

    return router
