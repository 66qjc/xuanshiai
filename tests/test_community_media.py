"""Community media upload, ownership and publish binding."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETUP = (ROOT / "database_setup_marriage.py").read_text(encoding="utf-8")


def test_community_media_tables_are_defined() -> None:
    assert "CREATE TABLE IF NOT EXISTS `community_media`" in SETUP
    assert "CREATE TABLE IF NOT EXISTS `community_media_attachment`" in SETUP
    assert "`purpose` varchar(32)" in SETUP
    assert "`status` varchar(16)" in SETUP
    assert "target_type" in SETUP
    assert "media_id" in SETUP
