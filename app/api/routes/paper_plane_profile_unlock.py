"""Backward-compatible import shim for the profile unlock route."""

from app.api.routes.paper_plane_unlock import unlock_profile as unlock_paper_plane_profile

__all__ = ["unlock_paper_plane_profile"]

