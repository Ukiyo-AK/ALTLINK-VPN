from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class RemnawaveUserTraffic(BaseModel):
    usedTrafficBytes: int = 0
    lifetimeUsedTrafficBytes: int = 0
    onlineAt: datetime | None = None
    lastConnectedNodeUuid: str | None = None
    firstConnectedAt: datetime | None = None


class RemnawaveUser(BaseModel):
    uuid: str
    id: int | None = None
    shortUuid: str
    username: str
    status: str
    trafficLimitBytes: int = 0
    trafficLimitStrategy: str = "NO_RESET"
    expireAt: datetime
    telegramId: int | None = None
    email: str | None = None
    description: str | None = None
    tag: str | None = None
    trojanPassword: str | None = None
    vlessUuid: str | None = None
    ssPassword: str | None = None
    subscriptionUrl: str | None = None
    userTraffic: RemnawaveUserTraffic = Field(default_factory=RemnawaveUserTraffic)
    createdAt: datetime | None = None
    updatedAt: datetime | None = None


class RemnawaveNodeInbound(BaseModel):
    uuid: str
    profileUuid: str
    tag: str
    type: str
    network: str | None = None
    security: str | None = None
    port: int | None = None
    rawInbound: dict[str, Any] | None = None


class RemnawaveNodeProfile(BaseModel):
    activeConfigProfileUuid: str | None = None
    activeInbounds: list[RemnawaveNodeInbound] = Field(default_factory=list)


class RemnawaveNode(BaseModel):
    uuid: str
    name: str
    address: str
    port: int | None = None
    isConnected: bool
    isConnecting: bool = False
    isDisabled: bool = False
    lastStatusChange: datetime | None = None
    lastStatusMessage: str | None = None
    xrayVersion: str | None = None
    nodeVersion: str | None = None
    xrayUptime: str | None = None
    isTrafficTrackingActive: bool = False
    trafficResetDay: int | None = None
    usersOnline: int | None = None
    cpuCount: int | None = None
    cpuModel: str | None = None
    totalRam: str | None = None
    consumptionMultiplier: float = 1.0
    trafficLimitBytes: int | None = None
    trafficUsedBytes: int | None = None
    notifyPercent: int | None = None
    viewPosition: int = 0
    countryCode: str | None = None
    tags: list[str] = Field(default_factory=list)
    configProfile: RemnawaveNodeProfile = Field(default_factory=RemnawaveNodeProfile)


class RemnawaveInboundWithSquads(RemnawaveNodeInbound):
    activeSquads: list[str] = Field(default_factory=list)


class RemnawaveConnectionKeys(BaseModel):
    enabledKeys: list[str] = Field(default_factory=list)
    hiddenKeys: list[str] = Field(default_factory=list)
    disabledKeys: list[str] = Field(default_factory=list)


class RemnawaveSubscriptionUserInfo(BaseModel):
    shortUuid: str
    daysLeft: int
    trafficUsed: str
    trafficLimit: str
    lifetimeTrafficUsed: str
    trafficUsedBytes: str
    trafficLimitBytes: str
    lifetimeTrafficUsedBytes: str
    username: str
    expiresAt: datetime
    isActive: bool
    userStatus: str
    trafficLimitStrategy: str


class RemnawaveSubscriptionInfo(BaseModel):
    isFound: bool
    user: RemnawaveSubscriptionUserInfo
    links: list[str] = Field(default_factory=list)
    ssConfLinks: dict[str, str] = Field(default_factory=dict)
    subscriptionUrl: str


class RemnawaveAccessibleSquad(BaseModel):
    squadName: str
    activeInbounds: list[str] = Field(default_factory=list)


class RemnawaveAccessibleNode(BaseModel):
    uuid: str
    nodeName: str
    countryCode: str
    configProfileUuid: str
    configProfileName: str
    activeSquads: list[RemnawaveAccessibleSquad] = Field(default_factory=list)


class RemnawaveAccessibleNodes(BaseModel):
    userUuid: str
    activeNodes: list[RemnawaveAccessibleNode] = Field(default_factory=list)


class RemnawaveTopNodeStat(BaseModel):
    uuid: str
    color: str
    name: str
    countryCode: str
    total: int


class RemnawaveUserUsage(BaseModel):
    categories: list[str] = Field(default_factory=list)
    sparklineData: list[int] = Field(default_factory=list)
    topNodes: list[RemnawaveTopNodeStat] = Field(default_factory=list)
    series: list[dict[str, Any]] = Field(default_factory=list)


class RemnawaveSubscriptionRequestRecord(BaseModel):
    id: int
    userUuid: str
    requestAt: datetime
    requestIp: str | None = None
    userAgent: str | None = None


class RemnawaveSubscriptionRequestHistory(BaseModel):
    total: int
    records: list[RemnawaveSubscriptionRequestRecord] = Field(default_factory=list)


class RemnawaveSystemStats(BaseModel):
    cpu: dict[str, Any] = Field(default_factory=dict)
    memory: dict[str, Any] = Field(default_factory=dict)
    uptime: int = 0
    timestamp: int = 0
    users: dict[str, Any] = Field(default_factory=dict)
    onlineStats: dict[str, Any] = Field(default_factory=dict)
    nodes: dict[str, Any] = Field(default_factory=dict)


class RemnawaveRealtimeNodeUsage(BaseModel):
    nodeUuid: str
    nodeName: str
    countryCode: str
    downloadBytes: int
    uploadBytes: int
    totalBytes: int
    downloadSpeedBps: int
    uploadSpeedBps: int
    totalSpeedBps: int


class RemnawaveNodeMetricInbound(BaseModel):
    tag: str
    upload: str
    download: str


class RemnawaveNodeMetric(BaseModel):
    nodeUuid: str
    nodeName: str
    countryEmoji: str
    providerName: str
    usersOnline: int
    inboundsStats: list[RemnawaveNodeMetricInbound] = Field(default_factory=list)
    outboundsStats: list[RemnawaveNodeMetricInbound] = Field(default_factory=list)

