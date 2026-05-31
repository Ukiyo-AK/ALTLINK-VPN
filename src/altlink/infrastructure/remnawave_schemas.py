from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class BaseSchema(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RemoteUserTraffic(BaseSchema):
    usedTrafficBytes: int
    lifetimeUsedTrafficBytes: int
    onlineAt: datetime | None = None
    firstConnectedAt: datetime | None = None
    lastConnectedNodeUuid: str | None = None


class RemoteInbound(BaseSchema):
    uuid: str
    profileUuid: str
    tag: str
    type: str
    network: str | None = None
    security: str | None = None
    port: int | None = None
    rawInbound: dict | None = None


class RemoteNodeConfigProfile(BaseSchema):
    activeConfigProfileUuid: str | None = None
    activeInbounds: list[RemoteInbound] = []


class RemoteNode(BaseSchema):
    uuid: str
    name: str
    address: str
    port: int | None = None
    isConnected: bool
    isDisabled: bool
    isConnecting: bool
    lastStatusChange: datetime | None = None
    lastStatusMessage: str | None = None
    xrayVersion: str | None = None
    nodeVersion: str | None = None
    xrayUptime: int | str | None = None
    isTrafficTrackingActive: bool
    trafficResetDay: int | None = None
    trafficLimitBytes: int | None = None
    trafficUsedBytes: int | None = None
    notifyPercent: int | None = None
    usersOnline: int | None = None
    viewPosition: int
    countryCode: str
    consumptionMultiplier: float
    tags: list[str]
    cpuCount: int | None = None
    cpuModel: str | None = None
    totalRam: str | None = None
    createdAt: datetime
    updatedAt: datetime
    configProfile: RemoteNodeConfigProfile


class RemoteInternalSquad(BaseSchema):
    uuid: str
    name: str


class RemoteInternalSquadInfo(BaseSchema):
    membersCount: int
    inboundsCount: int


class RemoteManagedInternalSquad(BaseSchema):
    uuid: str
    viewPosition: int
    name: str
    info: RemoteInternalSquadInfo
    inbounds: list[RemoteInbound]
    createdAt: datetime
    updatedAt: datetime


class RemoteUser(BaseSchema):
    uuid: str
    id: int
    shortUuid: str
    username: str
    status: str
    trafficLimitBytes: int
    trafficLimitStrategy: str
    expireAt: datetime
    telegramId: int | None = None
    email: str | None = None
    description: str | None = None
    tag: str | None = None
    hwidDeviceLimit: int | None = None
    externalSquadUuid: str | None = None
    trojanPassword: str
    vlessUuid: str
    ssPassword: str
    lastTriggeredThreshold: int
    subRevokedAt: datetime | None = None
    subLastUserAgent: str | None = None
    subLastOpenedAt: datetime | None = None
    lastTrafficResetAt: datetime | None = None
    createdAt: datetime
    updatedAt: datetime
    subscriptionUrl: str
    activeInternalSquads: list[RemoteInternalSquad] = []
    userTraffic: RemoteUserTraffic


class RemoteAccessibleSquad(BaseSchema):
    squadName: str
    activeInbounds: list[str]


class RemoteAccessibleNode(BaseSchema):
    uuid: str
    nodeName: str
    countryCode: str
    configProfileUuid: str
    configProfileName: str
    activeSquads: list[RemoteAccessibleSquad]


class RemoteConnectionKeys(BaseSchema):
    enabledKeys: list[str]
    hiddenKeys: list[str]
    disabledKeys: list[str]


class RemoteHwidDevice(BaseSchema):
    hwid: str
    userUuid: str
    platform: str | None = None
    osVersion: str | None = None
    deviceModel: str | None = None
    userAgent: str | None = None
    createdAt: datetime
    updatedAt: datetime


class RemoteConnectedIp(BaseSchema):
    ip: str
    lastSeen: datetime


class RemoteUserConnectedIps(BaseSchema):
    userId: str
    ips: list[RemoteConnectedIp] = []


class RemoteNodeUsersIpsResult(BaseSchema):
    success: bool
    nodeUuid: str
    users: list[RemoteUserConnectedIps] = []


class RemoteNodeUsersIpsJob(BaseSchema):
    isCompleted: bool
    isFailed: bool
    result: RemoteNodeUsersIpsResult | None = None


class RemoteSubscriptionInfoUser(BaseSchema):
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


class RemoteSubscriptionInfo(BaseSchema):
    isFound: bool
    user: RemoteSubscriptionInfoUser
    links: list[str]
    ssConfLinks: dict[str, str]
    subscriptionUrl: str


class RemoteSubscriptionRequestRecord(BaseSchema):
    id: int
    userUuid: str
    requestIp: str | None = None
    userAgent: str | None = None
    requestAt: datetime


class RemoteSeriesPoint(BaseSchema):
    uuid: str
    name: str
    color: str
    countryCode: str
    total: int
    data: list[int]


class RemoteUsageTopNode(BaseSchema):
    uuid: str
    color: str
    name: str
    countryCode: str
    total: int


class RemoteUsageResponse(BaseSchema):
    categories: list[str]
    sparklineData: list[int]
    topNodes: list[RemoteUsageTopNode]
    series: list[RemoteSeriesPoint]


class RemoteNodeUserUsageRow(BaseSchema):
    userUuid: str
    username: str
    nodeUuid: str
    total: float
    date: str


class RemoteTrafficSnapshot(BaseSchema):
    snapshotDate: date
    total: int
