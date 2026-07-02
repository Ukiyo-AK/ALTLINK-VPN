from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload

from altlink.application.services.accounts import AccountService
from altlink.application.services.base import BaseService, ConflictError, NotFoundError
from altlink.domain.billing import quantize_money
from altlink.domain.enums import BalanceTransactionType, PromoRewardKind, SystemEventLevel
from altlink.infrastructure.db.models import PromoCode, PromoCodeRedemption
from altlink.utils.time import utc_now


class PromoService(BaseService):
    source = "promos"
    PERSONAL_DISCOUNT_NAME = "Персональная скидка ALT10"

    def __init__(self, *, session, settings, remnawave, accounts: AccountService) -> None:
        super().__init__(session, settings, remnawave)
        self.accounts = accounts

    async def list_codes(self, limit: int = 50) -> list[PromoCode]:
        return list(
            (
                await self.session.scalars(
                    select(PromoCode)
                    .options(joinedload(PromoCode.created_by_admin))
                    .order_by(PromoCode.created_at.desc())
                    .limit(limit)
                )
            ).all()
        )

    async def find_by_code(self, code: str) -> PromoCode | None:
        normalized = self.normalize_code(code)
        if not normalized:
            return None
        return await self.session.scalar(select(PromoCode).where(PromoCode.code == normalized))

    async def create_code(
        self,
        *,
        code: str,
        name: str,
        reward_kind: PromoRewardKind,
        reward_value: Decimal,
        usage_limit: int | None,
        expires_at: datetime | None,
        new_users_only: bool,
        admin_id: str | None,
        assigned_user_id: str | None = None,
    ) -> PromoCode:
        normalized = self.normalize_code(code)
        if not normalized:
            raise ConflictError("Код промокода не может быть пустым.")
        if reward_value <= 0:
            raise ConflictError("Вознаграждение по промокоду должно быть больше нуля.")
        if usage_limit is not None and usage_limit <= 0:
            raise ConflictError("Лимит использований должен быть больше нуля.")
        if reward_kind == PromoRewardKind.PLAN_DISCOUNT and Decimal(reward_value) > Decimal("100"):
            raise ConflictError("Скидка на тариф не может быть больше 100%.")
        if await self.find_by_code(normalized):
            raise ConflictError("Промокод с таким кодом уже существует.")

        item = PromoCode(
            code=normalized,
            name=name.strip() or normalized,
            reward_kind=reward_kind,
            reward_value=quantize_money(Decimal(reward_value)),
            usage_limit=usage_limit,
            expires_at=expires_at,
            new_users_only=new_users_only,
            assigned_user_id=assigned_user_id,
            created_by_admin_id=admin_id,
        )
        self.session.add(item)
        await self.session.flush()
        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="promo_created",
            message="Создан новый промокод.",
            payload={
                "promo_code_id": item.id,
                "code": item.code,
                "reward_kind": item.reward_kind.value,
                "reward_value": str(item.reward_value),
                "usage_limit": item.usage_limit,
                "new_users_only": item.new_users_only,
                "assigned_user_id": item.assigned_user_id,
            },
            actor_admin_id=admin_id,
        )
        return item

    async def get_or_create_personal_discount_code(
        self,
        user_id: str,
        *,
        discount_percent: Decimal = Decimal("10"),
        campaign_key: str = "default",
    ) -> PromoCode:
        user = await self.accounts.get_user(user_id)
        normalized_percent = quantize_money(Decimal(discount_percent))
        candidates = list(
            (
                await self.session.scalars(
                    select(PromoCode)
                    .options(selectinload(PromoCode.redemptions))
                    .where(
                        PromoCode.assigned_user_id == user.id,
                        PromoCode.name.like(f"{self.PERSONAL_DISCOUNT_NAME}%"),
                        PromoCode.reward_kind == PromoRewardKind.PLAN_DISCOUNT,
                        PromoCode.reward_value == normalized_percent,
                        PromoCode.is_active.is_(True),
                    )
                    .order_by(PromoCode.created_at.desc())
                )
            ).all()
        )
        now = utc_now()
        for candidate in candidates:
            if candidate.expires_at and candidate.expires_at <= now:
                continue
            redemption = next(
                (item for item in candidate.redemptions if item.user_id == user.id),
                None,
            )
            if redemption is not None and redemption.applied_at is None:
                return candidate
            if redemption is None and (
                candidate.usage_limit is None or candidate.used_count < candidate.usage_limit
            ):
                return candidate

        normalized_campaign = str(campaign_key or "default").strip() or "default"
        campaign_name = f"{self.PERSONAL_DISCOUNT_NAME} · {normalized_campaign}"[:255]
        existing = await self.session.scalar(
            select(PromoCode)
            .where(
                PromoCode.assigned_user_id == user.id,
                PromoCode.name == campaign_name,
                PromoCode.reward_kind == PromoRewardKind.PLAN_DISCOUNT,
                PromoCode.reward_value == normalized_percent,
            )
            .order_by(PromoCode.created_at.desc())
        )
        if existing is not None:
            return existing

        secret = str(self.settings.secret_key or "altlink-personal-promo").encode("utf-8")
        digest = hmac.new(
            secret,
            f"personal-discount:{user.id}:{normalized_percent}:{normalized_campaign}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:12].upper()
        code = f"ALT10-{digest}"
        existing_by_code = await self.find_by_code(code)
        if existing_by_code is not None:
            if existing_by_code.assigned_user_id == user.id:
                return existing_by_code
            raise ConflictError("Не удалось подготовить персональный промокод.")

        return await self.create_code(
            code=code,
            name=campaign_name,
            reward_kind=PromoRewardKind.PLAN_DISCOUNT,
            reward_value=normalized_percent,
            usage_limit=1,
            expires_at=None,
            new_users_only=False,
            admin_id=None,
            assigned_user_id=user.id,
        )

    async def redeem_code(self, user_id: str, code: str) -> tuple[PromoCode, PromoCodeRedemption, str]:
        user = await self.accounts.get_user(user_id)
        promo = await self.find_by_code(code)
        if promo is None or not promo.is_active:
            raise ConflictError("Промокод не найден или уже отключён.")
        now = utc_now()
        if promo.expires_at and promo.expires_at <= now:
            raise ConflictError("Срок действия этого промокода уже истёк.")
        if promo.assigned_user_id is not None and promo.assigned_user_id != user.id:
            raise ConflictError("Этот промокод выпущен для другого пользователя.")
        existing = await self.session.scalar(
            select(PromoCodeRedemption).where(
                PromoCodeRedemption.promo_code_id == promo.id,
                PromoCodeRedemption.user_id == user.id,
            )
        )
        if existing is not None:
            if existing.applied_at is None and promo.reward_kind == PromoRewardKind.PLAN_DISCOUNT:
                result = (
                    f"Промокод {promo.code} уже активирован. "
                    "Скидка применится при следующей оплате тарифа, включая автоматическое продление."
                )
                return promo, existing, result
            raise ConflictError("Этот промокод уже был использован на вашем аккаунте.")
        if promo.usage_limit is not None and promo.used_count >= promo.usage_limit:
            raise ConflictError("Лимит использований этого промокода уже исчерпан.")
        if promo.new_users_only and not await self.accounts.is_new_account_for_promo(user.id):
            raise ConflictError("Этот промокод доступен только новым пользователям без транзакций.")

        redemption = PromoCodeRedemption(promo_code_id=promo.id, user_id=user.id)
        self.session.add(redemption)
        promo.used_count += 1

        if promo.reward_kind == PromoRewardKind.BALANCE:
            amount = quantize_money(Decimal(promo.reward_value))
            await self.accounts.adjust_balance(
                user_id=user.id,
                amount_rub=amount,
                transaction_type=BalanceTransactionType.PROMO_BONUS,
                description=f"Промокод {promo.code}: начисление на баланс",
            )
            redemption.applied_at = now
            redemption.reward_value_applied = amount
            result = f"Промокод применён. На баланс зачислено {amount:.2f} ₽."
        else:
            result = (
                f"Промокод {promo.code} активирован. "
                f"Скидка {Decimal(promo.reward_value):.2f}% применится при следующей оплате тарифа, "
                "включая автоматическое продление."
            )

        await self.log_event(
            level=SystemEventLevel.INFO,
            event_type="promo_redeemed",
            message="Пользователь активировал промокод.",
            payload={"user_id": user.id, "promo_code_id": promo.id, "code": promo.code},
        )
        await self.session.flush()
        return promo, redemption, result

    async def calculate_discount(
        self,
        user_id: str,
        plan_price_rub: Decimal,
    ) -> tuple[Decimal, PromoCode | None, PromoCodeRedemption | None]:
        redemption = await self.session.scalar(
            select(PromoCodeRedemption)
            .options(joinedload(PromoCodeRedemption.promo_code))
            .join(PromoCode)
            .where(
                PromoCodeRedemption.user_id == user_id,
                PromoCodeRedemption.applied_at.is_(None),
                PromoCode.reward_kind == PromoRewardKind.PLAN_DISCOUNT,
            )
            .order_by(PromoCodeRedemption.created_at.desc())
        )
        if redemption is None or redemption.promo_code is None:
            return Decimal("0.00"), None, None

        percent = min(max(Decimal(redemption.promo_code.reward_value), Decimal("0")), Decimal("100"))
        discount = quantize_money((Decimal(plan_price_rub) * percent) / Decimal("100"))
        if discount > Decimal(plan_price_rub):
            discount = quantize_money(Decimal(plan_price_rub))
        return discount, redemption.promo_code, redemption

    async def consume_discount(
        self,
        redemption: PromoCodeRedemption | None,
        *,
        subscription_id: str,
        applied_amount: Decimal,
    ) -> None:
        if redemption is None:
            return
        redemption.applied_at = utc_now()
        redemption.applied_subscription_id = subscription_id
        redemption.reward_value_applied = quantize_money(Decimal(applied_amount))
        await self.session.flush()

    @staticmethod
    def normalize_code(code: str | None) -> str:
        raw = (code or "").strip().upper()
        return "".join(ch for ch in raw if ch.isalnum() or ch in {"_", "-"})
