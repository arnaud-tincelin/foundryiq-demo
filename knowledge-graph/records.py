"""Validate source files and prepare short fact records for graph extraction."""

import re
from dataclasses import dataclass


class DemoError(Exception):
    pass


QUALITY_FILES = {
    "quality/quality-hold-qh-014.json": ("QH-014", "quality-hold"),
    "quality/delivery-pod-901.json": ("POD-901", "proof-of-delivery"),
    "quality/delivery-pod-902.json": ("POD-902", "proof-of-delivery"),
    "quality/delivery-pod-904.json": ("POD-904", "proof-of-delivery"),
}
WAREHOUSE_FILES = {
    "dispatch-sh-901.json": ("SH-901", "dispatch"),
    "dispatch-sh-902.json": ("SH-902", "dispatch"),
    "dispatch-sh-904.json": ("SH-904", "dispatch"),
    "dispatch-sh-905.json": ("SH-905", "dispatch"),
    "inventory-fg-083.json": ("FG-083", "inventory"),
}
PRODUCTION_FILES = {
    "material-issue-a-01.json": ("MIA-041-01", "material-issue"),
    "material-issue-a-02.json": ("MIA-041-02", "material-issue"),
    "material-issue-b-01.json": ("MIB-018-01", "material-issue"),
    "build-dm40-01.json": ("BT-040-01", "build"),
    "build-dm40-02.json": ("BT-040-02", "build"),
    "build-dm40-03.json": ("BT-040-03", "build"),
    "build-dm70-01.json": ("BT-070-01", "build"),
}
EXPECTED_RECORDS = {
    (source, record_id)
    for source, files in (("quality-blob", QUALITY_FILES), ("production-blob", PRODUCTION_FILES),
                          ("warehouse-blob", WAREHOUSE_FILES))
    for record_id, _ in files.values()
}


@dataclass(frozen=True)
class Record:
    source: str
    record_id: str
    locator: str
    statement: str

    @property
    def filename(self) -> str:
        return f"{self.source}-{self.record_id}.txt"

    def document(self) -> bytes:
        return (f"{self.statement}\n\nOriginal record ID: {self.record_id}\n"
                f"Origin: {self.source}\nOriginal record URL: {self.locator}\n").encode("utf-8")


def entity_id(fields: dict, key: str, prefix: str) -> str:
    value = fields.get(key)
    if not isinstance(value, str) or not re.fullmatch(prefix + r"-[0-9]+", value):
        raise DemoError(f"{key}: expected an exact {prefix}-number.")
    return value


def quality_record(filename: str, fields: dict, locator: str) -> Record:
    record_id, kind = QUALITY_FILES[filename]
    if (fields.get("recordId"), fields.get("kind")) != (record_id, kind):
        raise DemoError(f"{filename}: unexpected record ID or type.")
    if kind == "quality-hold":
        if fields.get("status") != "held":
            raise DemoError(f"{filename}: expected a confirmed quality hold.")
        lot = entity_id(fields, "sensorLotId", "SL")
        fact = f"Quality hold {record_id} applies to sensor lot {lot}."
    else:
        site_name = fields.get("customerSiteName")
        if fields.get("status") != "confirmed" or not isinstance(site_name, str) or not site_name.strip():
            raise DemoError(f"{filename}: expected a signed receipt and customer site name.")
        shipment = entity_id(fields, "shipmentId", "SH")
        site = entity_id(fields, "customerSiteId", "CS")
        fact = (f"Shipment {shipment} was delivered to customer site {site} with a signed receipt. "
                f"The display name for {site} is {site_name.strip()}.")
    return Record("quality-blob", record_id, locator, fact)


def production_record(filename: str, fields: dict, locator: str) -> Record:
    record_id, kind = PRODUCTION_FILES[filename]
    if (fields.get("recordId"), fields.get("kind")) != (record_id, kind):
        raise DemoError(f"{filename}: unexpected record ID or type.")
    assembly = entity_id(fields, "assemblyLotId", "AL")
    if kind == "material-issue":
        if fields.get("status") != "posted":
            raise DemoError(f"{filename}: expected posted material consumption.")
        lot = entity_id(fields, "sensorLotId", "SL")
        fact = f"Sensor lot {lot} was consumed in assembly lot {assembly}."
    else:
        if fields.get("status") != "completed":
            raise DemoError(f"{filename}: expected a completed build.")
        lot = entity_id(fields, "finishedLotId", "FL")
        fact = f"Assembly lot {assembly} was used to build finished lot {lot}."
    return Record("production-blob", record_id, locator, fact)


def warehouse_record(filename: str, fields: dict, locator: str) -> Record:
    record_id, kind = WAREHOUSE_FILES[filename]
    if (fields.get("recordId"), fields.get("kind")) != (record_id, kind):
        raise DemoError(f"{filename}: unexpected record ID or type.")
    lot = entity_id(fields, "finishedLotId", "FL")
    if kind == "inventory":
        if fields.get("status") != "blocked-in-warehouse" or fields.get("dispatched") is not False:
            raise DemoError(f"{filename}: expected a warehouse hold that was not dispatched.")
        fact = f"Finished lot {lot} remains blocked in the warehouse and was not dispatched."
    else:
        shipment = entity_id(fields, "shipmentId", "SH")
        if shipment != record_id or fields.get("status") != "dispatched":
            raise DemoError(f"{filename}: expected a dispatched manifest with a matching shipment ID.")
        fact = f"Finished lot {lot} was loaded on shipment {shipment} and dispatched."
    return Record("warehouse-blob", record_id, locator, fact)


def complete_snapshot(records: list[Record]) -> list[Record]:
    actual = {(r.source, r.record_id) for r in records}
    if actual != EXPECTED_RECORDS or len(actual) != len(records):
        raise DemoError("Source snapshot is missing or repeats a required record; nothing was published.")
    return sorted(records, key=lambda record: record.filename)
