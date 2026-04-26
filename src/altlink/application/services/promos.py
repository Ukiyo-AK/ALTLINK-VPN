from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from altlink.application.services.accounts import AccountService
from altlink.application.services.base import BaseService, ConflictError, NotFoundError
from altlink.domain.billing import quantize_money
from altlink.domain.enums import BalanceTransactionType, PromoRewardKind, SystemEventLevel
from altlink.infrastructure.db.models import PromoCode, PromoCodeRedemption
from altlink.utils.time import utc_now


class PromoService(BaseService):
    source = "promos"

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
            },
            actor_admin_id=admin_id,
        )
        return item

    async def redeem_code(self, user_id: str, code: str) -> tuple[PromoCode, PromoCodeRedemption, str]:
        user = await self.accounts.get_user(user_id)
        promo = await self.find_by_code(code)
        if promo is None or not promo.is_active:
            raise ConflictError("Промокод не найден или уже отключён.")
        now = utc_now()
        if promo.expires_at and promo.expires_at <= now:
            raise ConflictError("Срок действия этого промокода уже истёк.")
        if promo.usage_limit is not None and promo.used_count >= promo.usage_limit:
            raise ConflictError("Лимит использований этого промокода уже исчерпан.")
        existing = await self.session.scalar(
            select(PromoCodeRedemption).where(
                PromoCodeRedemption.promo_code_id == promo.id,
                PromoCodeRedemption.user_id == user.id,
            )
        )
        if existing is not None:
            raise ConflictError("Этот промокод уже был использован на вашем аккаунте.")
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
                f"Скидка {Decimal(promo.reward_value):.2f}% применится при следующей покупке тарифа."
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
