import logging

import requests
import vercel_blob
from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible

logger = logging.getLogger(__name__)


@deconstructible
class VercelBlobStorage(Storage):
    """
    Stores uploaded files (question pictures) in Vercel Blob instead of the
    local filesystem, which is read-only at runtime on Vercel. Same backend as
    jcda-election's.

    Vercel Blob hands back a permanent public CDN URL on upload, and that URL
    *is* this backend's storage "name" — Blob already makes the path unique
    (addRandomSuffix), so there's no separate relative path or store hostname
    to track. Students load pictures straight from Vercel's CDN, never through
    Django or the database.

    Reads BLOB_READ_WRITE_TOKEN from the environment (that's how the
    vercel_blob package finds it) — Vercel injects it once a Blob store is
    connected to the project.
    """

    def _save(self, name, content):
        result = vercel_blob.put(name, content.read(), {"addRandomSuffix": "true", "allowOverwrite": "false"})
        return result["url"]

    def _open(self, name, mode="rb"):
        response = requests.get(name, timeout=30)
        response.raise_for_status()
        return ContentFile(response.content)

    def exists(self, name):
        return False

    def url(self, name):
        return name

    def size(self, name):
        return vercel_blob.head(name).get("size", 0)

    def delete(self, name):
        # A picture that can't be deleted just stays in storage; never let
        # that break saving or deleting the question itself.
        try:
            vercel_blob.delete(name)
        except Exception:
            logger.exception("Could not delete %s from Vercel Blob", name)
