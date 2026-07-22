from __future__ import annotations

from altlink.domain.enums import TrafficLimitStrategy

BYTES_PER_GIB = 1024**3
TRAFFIC_LIMIT_STRATEGY_LABELS = {
    TrafficLimitStrategy.NO_RESET: "Без автоматического сброса",
    TrafficLimitStrategy.DAY: "Каждый день",
    TrafficLimitStrategy.WEEK: "Каждую неделю",
    TrafficLimitStrategy.MONTH: "Каждый месяц",
}


def parse_traffic_limit_strategy(value: object) -> TrafficLimitStrategy:
    if isinstance(value, TrafficLimitStrategy):
        return value
    normalized = str(value or TrafficLimitStrategy.NO_RESET.value).strip().upper()
    try:
        return TrafficLimitStrategy(normalized)
    except ValueError as exc:
        raise ValueError("Неизвестная стратегия сброса трафика.") from exc


def effective_traffic_limit(user, subscription) -> tuple[int, TrafficLimitStrategy]:
    override = getattr(user, "traffic_limit_bytes_override", None)
    if override is not None:
        strategy = parse_traffic_limit_strategy(
            getattr(user, "traffic_limit_strategy_override", None) or TrafficLimitStrategy.NO_RESET
        )
        return max(int(override), 0), strategy
    subscription_limit = getattr(subscription, "traffic_limit_bytes", 0) if subscription is not None else 0
    return max(int(subscription_limit or 0), 0), TrafficLimitStrategy.NO_RESET


def traffic_limit_strategy_label(value: object) -> str:
    try:
        strategy = parse_traffic_limit_strategy(value)
    except ValueError:
        return "Неизвестная стратегия"
    return TRAFFIC_LIMIT_STRATEGY_LABELS[strategy]
