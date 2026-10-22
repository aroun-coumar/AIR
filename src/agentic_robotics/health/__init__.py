"""Health: expected-component aggregation, monitors, probes, readiness, diagnosis (ADR 0015).

    from agentic_robotics.health import (HealthAggregator, HealthGate, derive_manifest,
                                         gated_checks)

    hm = derive_manifest(manifest, builtin_catalog(), timeout_s=2.0)
    agg = HealthAggregator(hm)
    gate = HealthGate(trace=runtime.trace)
    gate.attach_health(hm, agg)
    runtime.checks = gated_checks(gate, recovery=[hm])
"""

from .aggregate import HealthAggregator, evaluate_item
from .diagnosis import (
    Diagnoser,
    DiagnosisCheck,
    DiagnosisEntry,
    DiagnosisTable,
    DiagSeverity,
    FaultPhase,
    builtin_table,
    exercise,
)
from .gating import (
    HealthGate,
    RobotOwnedRecoveryCheck,
    downgrade,
    gate_capability,
    gated_checks,
)
from .model import (
    CapabilityGate,
    ExpectedItem,
    HealthLevel,
    HealthManifest,
    HealthReport,
    HealthStatus,
    component_path,
    derive_manifest,
)
from .monitors import FrequencyMonitor, LatencyMonitor
from .probes import PsiProbe
from .readiness import (
    ALL_MODES,
    CapabilityCheck,
    CheckResult,
    FreshnessCheck,
    HealthCheck,
    ManifestCheck,
    NotReady,
    ReadinessCheck,
    ReadinessContext,
    ReadinessGate,
    ReadinessReport,
)

__requirements__ = ("REQ13", "REQ14")

__all__ = [
    "ALL_MODES", "CapabilityCheck", "CapabilityGate", "CheckResult", "DiagSeverity",
    "Diagnoser", "DiagnosisCheck", "DiagnosisEntry", "DiagnosisTable", "ExpectedItem",
    "FaultPhase", "FrequencyMonitor", "FreshnessCheck", "HealthAggregator", "HealthCheck",
    "HealthGate", "HealthLevel", "HealthManifest", "HealthReport", "HealthStatus",
    "LatencyMonitor", "ManifestCheck", "NotReady", "PsiProbe", "ReadinessCheck",
    "ReadinessContext", "ReadinessGate", "ReadinessReport", "RobotOwnedRecoveryCheck",
    "builtin_table", "component_path", "derive_manifest", "downgrade", "evaluate_item",
    "exercise", "gate_capability", "gated_checks",
]
