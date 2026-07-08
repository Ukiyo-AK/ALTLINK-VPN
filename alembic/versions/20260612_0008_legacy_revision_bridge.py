"""legacy revision bridge

Revision ID: 20260612_0008
Revises: 20260610_0006
Create Date: 2026-06-12 00:00:00.000000
"""

from __future__ import annotations

revision = "20260612_0008"
down_revision = "20260610_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Compatibility bridge for databases stamped with a removed legacy revision."""


def downgrade() -> None:
    """Compatibility bridge has no schema changes to roll back."""
