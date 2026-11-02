"""Package library: interface contracts, packages, compatibility checks, a service
registry, and a versioned skill library with evidence-bound promotion (ADR 0020)."""

from .compat import (
    HostFacts,
    check_installation,
    check_package_on_robot,
    check_provided,
    check_skill_on_robot,
    providers,
)
from .contracts import (
    DeployedPackage,
    Deployment,
    DeviceRequirement,
    EvidenceDomain,
    EvidenceRef,
    HardwareRequirements,
    HostRequirements,
    InterfaceContract,
    InterfaceEffect,
    InterfaceKind,
    InterfaceMode,
    PackageDependency,
    PackageKind,
    PackageManifest,
    Provide,
    Require,
    SkillRelease,
    SkillStage,
)
from .interfaces import InterfaceCatalog, builtin_interfaces, project_interfaces
from .services import (
    Binding,
    CallResult,
    ProviderState,
    ServiceError,
    ServiceOutcomeUnknown,
    ServiceRegistry,
)
from .skills import Resolution, SkillLibrary, SkillLibraryError, registry_name

__requirements__ = ("REQ05",)

__all__ = [
    "Binding", "CallResult", "DeployedPackage", "Deployment", "DeviceRequirement",
    "EvidenceDomain", "EvidenceRef", "HardwareRequirements", "HostFacts", "HostRequirements",
    "InterfaceCatalog", "InterfaceContract", "InterfaceEffect", "InterfaceKind",
    "InterfaceMode", "PackageDependency", "PackageKind", "PackageManifest", "Provide",
    "ProviderState", "Require", "Resolution", "ServiceError", "ServiceOutcomeUnknown",
    "ServiceRegistry", "SkillLibrary", "SkillLibraryError", "SkillRelease", "SkillStage",
    "builtin_interfaces", "check_installation", "check_package_on_robot",
    "check_provided", "check_skill_on_robot", "project_interfaces", "providers", "registry_name",
]
