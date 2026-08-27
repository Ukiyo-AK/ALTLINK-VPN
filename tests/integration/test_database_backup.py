from __future__ import annotations

from decimal import Decimal
import json

import pytest
from sqlalchemy import select

from altlink.domain.enums import BalanceTransactionType
from altlink.infrastructure.db.models import ServerMetricSnapshot


@pytest.mark.asyncio
async def test_database_backup_roundtrip_restores_users_referrals_and_admins(test_services):
    async with test_services.hub() as hub:
        admin = await hub.accounts.create_admin(
            username="backup-admin",
            password="secret",
            full_name="Backup Admin",
            telegram_id=9001,
        )
        referrer = await hub.accounts.get_or_create_user(
            telegram_id=101,
            username="referrer",
            first_name="Ref",
            last_name=None,
            language_code="ru",
        )
        await hub.accounts.complete_registration(referrer.id)

        invited = await hub.accounts.get_or_create_user(
            telegram_id=202,
            username="invited",
            first_name="Invited",
            last_name=None,
            language_code="ru",
        )
        await hub.accounts.bind_referrer(invited.id, referrer.referral_code)
        await hub.accounts.adjust_balance(
            user_id=referrer.id,
            amount_rub=Decimal("123.45"),
            transaction_type=BalanceTransactionType.TOPUP,
            description="seed balance",
            admin_id=admin.id,
        )
        await hub.dashboard.capture_server_metrics(force=True)
        artifact = await hub.backups.export_database()
        backup_payload = json.loads(artifact.content)
        referrer_id = referrer.id
        invited_id = invited.id
        referrer_code = referrer.referral_code

    async with test_services.hub() as hub:
        extra_user = await hub.accounts.get_or_create_user(
            telegram_id=303,
            username="temporary",
            first_name="Temp",
            last_name=None,
            language_code="ru",
        )
        await hub.accounts.adjust_balance(
            user_id=extra_user.id,
            amount_rub=Decimal("50"),
            transaction_type=BalanceTransactionType.TOPUP,
            description="temporary balance",
        )

        summary = await hub.backups.import_database(artifact.content)
        restored_referrer = await hub.accounts.get_user(referrer_id)
        restored_invited = await hub.accounts.get_user(invited_id)
        missing_extra_user = await hub.accounts.get_user_by_telegram_id(303)
        restored_admin = await hub.accounts.get_admin_by_telegram_id(9001)
        restored_metric_snapshots = list((await hub.session.scalars(select(ServerMetricSnapshot))).all())

    assert summary["format"] == "altlink-db-backup-v1"
    assert "server_metric_snapshots" not in backup_payload["tables"]
    assert summary["table_counts"]["users"] >= 2
    assert restored_admin is not None
    assert restored_referrer.referral_code == referrer_code
    assert restored_invited.referred_by_user_id == restored_referrer.id
    assert Decimal(restored_referrer.balance_rub) == Decimal("123.45")
    assert missing_extra_user is None
    assert restored_metric_snapshots == []
