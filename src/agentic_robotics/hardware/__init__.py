"""Hardware catalog: robot classes, device types and protocols, and manifest checks."""

from .catalog import (
    DeviceCatalog,
    DeviceType,
    MaintenanceBasis,
    MaintenanceItem,
    PropertyKind,
    PropertySpec,
    Protocol,
    Reference,
    RobotClass,
    SignalSpec,
    Slot,
    builtin_catalog,
    load_files,
    matches,
    project_catalog,
)
from .validate import (
    ComponentLimit,
    ComponentMaintenance,
    Finding,
    Level,
    check_manifest,
    evaluate,
    maintenance_plan,
    telemetry_limits,
)

__requirements__ = ("REQ03",)

__all__ = [
    "ComponentLimit", "ComponentMaintenance", "DeviceCatalog", "DeviceType", "Finding", "Level",
    "MaintenanceBasis", "MaintenanceItem", "PropertyKind", "PropertySpec", "Protocol",
    "Reference", "RobotClass", "SignalSpec", "Slot", "builtin_catalog", "check_manifest",
    "evaluate", "load_files", "maintenance_plan", "matches", "project_catalog",
    "telemetry_limits",
]
