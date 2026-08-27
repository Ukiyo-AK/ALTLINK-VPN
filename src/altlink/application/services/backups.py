from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
import json
from typing import Any

from sqlalchemy import Date, DateTime, Enum as SqlEnum, Numeric, delete, select, update

from altlink.application.services.base import BaseService, ServiceError
from altlink.infrastructure.db.models import Base
from altlink.utils.time import utc_now

BACKUP_FORMAT = "altlink-db-backup-v1"
BACKUP_EXCLUDED_TABLES = frozenset({"server_metric_snapshots"})


class BackupFormatError(ServiceError):
    pass


@dataclass(slots=True)
class BackupArtifact:
    filename: str
    content: bytes
    summary: dict[str, Any]


class BackupService(BaseService):
    source = "backups"

    async def export_database(self) -> BackupArtifact:
        snapshot = await self._build_snapshot()
        content = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")
        exported_at = datetime.fromisoformat(str(snapshot["exported_at"]))
        filename = f"altlink-backup-{exported_at:%Y%m%d-%H%M%S}.json"
        return BackupArtifact(filename=filename, content=content, summary=self.describe_snapshot(snapshot))

    async def inspect_database_backup(self, payload: bytes | str) -> dict[str, Any]:
        snapshot = self._parse_snapshot(payload)
        return self.describe_snapshot(snapshot)

    async def import_database(self, payload: bytes | str) -> dict[str, Any]:
        snapshot = self._parse_snapshot(payload)
        models = self._models_in_dependency_order()
        tables_payload = snapshot.get("tables", {})

        # Derived history is intentionally omitted from backups and must not survive
        # a restore from an unrelated database state.
        for model in self._excluded_models():
            await self.session.execute(delete(model))
        for model in reversed(models):
            await self.session.execute(delete(model))
        await self.session.flush()

        deferred_updates: list[tuple[type[Base], dict[str, Any], dict[str, Any]]] = []
        imported_counts: dict[str, int] = {}

        for model in models:
            table_name = model.__table__.name
            rows = tables_payload.get(table_name) or []
            imported_counts[table_name] = len(rows)
            self_referencing_columns = self._self_referencing_columns(model)
            for payload_row in rows:
                decoded = self._deserialize_row(model, payload_row)
                deferred_values = {
                    column_name: decoded.pop(column_name)
                    for column_name in self_referencing_columns
                    if decoded.get(column_name) is not None
                }
                self.session.add(model(**decoded))
                if deferred_values:
                    deferred_updates.append((model, self._primary_key_payload(model, decoded), deferred_values))
            await self.session.flush()

        for model, primary_key, values in deferred_updates:
            conditions = [getattr(model, key) == value for key, value in primary_key.items()]
            await self.session.execute(update(model).where(*conditions).values(**values))

        await self.session.flush()
        return {
            "format": BACKUP_FORMAT,
            "exported_at": snapshot.get("exported_at"),
            "database_dialect": self.session.bind.dialect.name if self.session.bind is not None else "unknown",
            "table_counts": imported_counts,
            "total_rows": sum(imported_counts.values()),
        }

    async def _build_snapshot(self) -> dict[str, Any]:
        table_payloads: dict[str, list[dict[str, Any]]] = {}
        table_counts: dict[str, int] = {}
        for model in self._models_in_dependency_order():
            order_by = [column.asc() for column in model.__table__.primary_key.columns]
            rows = list((await self.session.scalars(select(model).order_by(*order_by))).all())
            serialized_rows = [self._serialize_row(model, row) for row in rows]
            table_payloads[model.__table__.name] = serialized_rows
            table_counts[model.__table__.name] = len(serialized_rows)
        return {
            "format": BACKUP_FORMAT,
            "exported_at": utc_now().isoformat(),
            "database_dialect": self.session.bind.dialect.name if self.session.bind is not None else "unknown",
            "table_counts": table_counts,
            "tables": table_payloads,
        }

    def describe_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        table_counts = {
            str(name): int(count)
            for name, count in (snapshot.get("table_counts") or self._count_rows(snapshot)).items()
        }
        return {
            "format": str(snapshot.get("format") or ""),
            "exported_at": snapshot.get("exported_at"),
            "database_dialect": snapshot.get("database_dialect") or "unknown",
            "table_counts": table_counts,
            "total_rows": sum(table_counts.values()),
        }

    def _count_rows(self, snapshot: dict[str, Any]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table_name, rows in (snapshot.get("tables") or {}).items():
            counts[str(table_name)] = len(rows or [])
        return counts

    def _parse_snapshot(self, payload: bytes | str) -> dict[str, Any]:
        try:
            if isinstance(payload, bytes):
                snapshot = json.loads(payload.decode("utf-8"))
            else:
                snapshot = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupFormatError("Файл резервной копии не удалось прочитать как JSON.") from exc
        if not isinstance(snapshot, dict):
            raise BackupFormatError("Файл резервной копии имеет неверный формат.")
        if snapshot.get("format") != BACKUP_FORMAT:
            raise BackupFormatError("Файл резервной копии создан в неподдерживаемом формате.")
        if not isinstance(snapshot.get("tables"), dict):
            raise BackupFormatError("В файле резервной копии отсутствуют данные таблиц.")
        return snapshot

    def _models_in_dependency_order(self) -> list[type[Base]]:
        table_to_model = {
            mapper.local_table.name: mapper.class_
            for mapper in Base.registry.mappers
            if getattr(mapper, "local_table", None) is not None
        }
        models: list[type[Base]] = []
        for table in Base.metadata.sorted_tables:
            if table.name in BACKUP_EXCLUDED_TABLES:
                continue
            model = table_to_model.get(table.name)
            if model is not None and model not in models:
                models.append(model)
        return models

    def _excluded_models(self) -> list[type[Base]]:
        return [
            mapper.class_
            for mapper in Base.registry.mappers
            if getattr(mapper, "local_table", None) is not None
            and mapper.local_table.name in BACKUP_EXCLUDED_TABLES
        ]

    def _serialize_row(self, model: type[Base], row: Base) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for column in model.__table__.columns:
            payload[column.name] = self._serialize_value(getattr(row, column.name))
        return payload

    def _deserialize_row(self, model: type[Base], payload_row: dict[str, Any]) -> dict[str, Any]:
        decoded: dict[str, Any] = {}
        for column in model.__table__.columns:
            if column.name not in payload_row:
                continue
            decoded[column.name] = self._deserialize_value(column.type, payload_row[column.name])
        return decoded

    def _primary_key_payload(self, model: type[Base], decoded_row: dict[str, Any]) -> dict[str, Any]:
        return {
            column.name: decoded_row[column.name]
            for column in model.__table__.primary_key.columns
            if column.name in decoded_row
        }

    def _self_referencing_columns(self, model: type[Base]) -> set[str]:
        names: set[str] = set()
        table = model.__table__
        for column in table.columns:
            for foreign_key in column.foreign_keys:
                if foreign_key.column.table.name == table.name:
                    names.add(column.name)
        return names

    def _serialize_value(self, value: Any) -> Any:
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        return value

    def _deserialize_value(self, column_type: Any, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(column_type, SqlEnum):
            enum_class = getattr(column_type, "enum_class", None)
            if enum_class is not None:
                return enum_class(value)
            return value
        if isinstance(column_type, Numeric):
            return Decimal(str(value))
        if isinstance(column_type, DateTime):
            return datetime.fromisoformat(str(value))
        if isinstance(column_type, Date):
            return date.fromisoformat(str(value))
        return value
