"""Policies, releases and engineering-domain contracts (ADR 0016)."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pytest

from agentic_robotics.cli import main
from agentic_robotics.contracts.authority import Delegation
from agentic_robotics.contracts.dataset import (
    Episode,
    EpisodeMetadata,
    GitState,
    NanCheck,
    Step,
    TrainingRun,
    first_non_finite,
    from_lerobot,
    from_rlds,
    to_lerobot,
    to_rlds,
)
from agentic_robotics.contracts.parameters import (
    ParameterDef,
    ParameterSchema,
    ParameterSet,
    ParamType,
    UserLevel,
)
from agentic_robotics.contracts.policy import (
    ActionChunk,
    ActionSlot,
    ActionSpace,
    ExitMode,
    FeatureSpec,
    ObservationProcessing,
    PolicyManifest,
    PolicyOrigin,
    PolicyRequirements,
    UnifiedAction,
    default_action_space,
)
from agentic_robotics.contracts.release import (
    ActivationStateKind,
    AssetCard,
    AssetKind,
    ConformanceCheck,
    ConformanceReportRecord,
    EvaluationEvidence,
    Release,
    ReleaseActivation,
    ReleaseTarget,
)
from agentic_robotics.core.clock import ManualClock
from agentic_robotics.core.errors import SchemaError
from agentic_robotics.core.schema import load_any
from agentic_robotics.provenance import Project
from agentic_robotics.registry import LocalRegistry
from agentic_robotics.releases import (
    ActivationError,
    ActivationManager,
    HmacSigner,
    KeyStore,
    PolicyError,
    PolicySlot,
    SlotState,
    TrustedSet,
    TufError,
    ed25519_available,
    tuf,
)
from agentic_robotics.releases.records import (
    git_state,
    record_episode,
    record_parameter_snapshot,
    record_policy,
    record_training_run,
)
from agentic_robotics.runtime.authority import AuthorityService
from agentic_robotics.runtime.trace import Trace

pytestmark = pytest.mark.req("REQ16", "F04")
policies = pytest.mark.req("REQ05", "REQ11", "REQ12", "G05", "G15")
releases = pytest.mark.req("REQ10", "G14", "G15", "D15")
params = pytest.mark.req("REQ03", "REQ04", "G10")
datasets = pytest.mark.req("REQ02", "F05", "G03")
cards = pytest.mark.req("REQ18", "F06", "F10")

MODEL = b"\x00opaque-model-bytes\x01"
DIGEST = "sha256:" + hashlib.sha256(MODEL).hexdigest()
JOINTS = ("hip", "knee")


def manifest(**kw: object) -> PolicyManifest:
    fields: dict[str, object] = dict(
        policy_id="walk", version="1.0.0", model_digest=DIGEST, model_api="duck/1",
        model_format="onnx", inference_backend="onnxruntime-cpu", robot_model="duck-v2",
        observation_features=(FeatureSpec(name="imu", shape=(3,), unit="rad/s"),
                              FeatureSpec(name="joints", shape=(2,), unit="rad",
                                          names=JOINTS)),
        action_feature=FeatureSpec(name="joint_targets", shape=(2,), unit="rad",
                                   names=JOINTS),
        prediction_steps=4, execution_steps=2, action_interval_s=0.02,
        max_action_age_s=0.1, exit_mode=ExitMode.DAMP, origin=PolicyOrigin.OFFICIAL,
        joint_order=JOINTS, kp=(20.0, 20.0), kd=(0.5, 0.5),
        observation=ObservationProcessing(clip=5.0, history=1),
        action_low=(-1.0, -1.0), action_high=(1.0, 1.0))
    fields.update(kw)
    return PolicyManifest(**fields)  # type: ignore[arg-type]


REQS = PolicyRequirements(model_api="duck/1", robot_model="duck-v2", obs_dim=5, action_dim=2,
                          joint_order=JOINTS, kp=(20.0, 20.0), kd=(0.5, 0.5),
                          observation=ObservationProcessing(clip=5.0, history=1),
                          inference_backends=("onnxruntime-cpu",), max_action_age_s=0.25)


def runner_factory(m: PolicyManifest, model: bytes):  # type: ignore[no-untyped-def]
    def run(obs):  # type: ignore[no-untyped-def]
        return [[0.1, -0.1]] * m.prediction_steps
    return run


OBS = {"imu": [0.0, 0.1, 0.2], "joints": [0.3, 0.4]}


def slot(clock: ManualClock, **kw: object) -> PolicySlot:
    return PolicySlot("duck-1", "locomotion", REQS, clock=clock, trace=Trace(clock),
                      **kw)  # type: ignore[arg-type]


# -- policy manifest --------------------------------------------------------------------

@policies
def test_manifest_round_trips_and_identity_covers_backend_and_gains() -> None:
    m = manifest()
    assert load_any(json.loads(m.to_json())) == m
    assert m.obs_dim == 5 and m.action_dim == 2
    assert m.identity() == manifest(description="other text").identity()
    assert m.identity() != manifest(inference_backend="onnxruntime-gpu").identity()
    assert m.identity() != manifest(kp=(21.0, 20.0)).identity()


@policies
def test_manifest_rejects_inconsistent_metadata() -> None:
    with pytest.raises(SchemaError):
        manifest(execution_steps=5)          # more than predicted
    with pytest.raises(SchemaError):
        manifest(max_action_age_s=0.0)       # staleness bound is mandatory and positive
    with pytest.raises(SchemaError):
        manifest(kp=(1.0,))                  # gains must follow joint order
    with pytest.raises(SchemaError):
        manifest(action_low=(0.0,))
    doc = json.loads(manifest().to_json())
    del doc["max_action_age_s"]
    with pytest.raises(SchemaError):
        PolicyManifest.from_dict(doc)
    doc = json.loads(manifest().to_json())
    doc["surprise"] = 1
    with pytest.raises(SchemaError):
        PolicyManifest.from_dict(doc)


@policies
def test_load_gate_refuses_mismatch_and_keeps_old_policy() -> None:
    clock = ManualClock()
    s = slot(clock)
    good = s.load(manifest(), MODEL, runner_factory=runner_factory)
    assert good.loaded and s.generation == 1
    bad = s.load(manifest(version="2.0.0", joint_order=("knee", "hip")), MODEL,
                 runner_factory=runner_factory)
    assert not bad.loaded and "joint_order differs" in bad.reasons
    assert s.identity == good.identity and s.generation == 1
    tampered = s.load(manifest(version="3"), MODEL + b"x")
    assert not tampered.loaded and "digest" in tampered.reasons[0]

    def broken(m: PolicyManifest, b: bytes):  # type: ignore[no-untyped-def]
        raise RuntimeError("bad onnx")
    failed = s.load(manifest(version="4"), MODEL, runner_factory=broken)
    assert not failed.loaded and s.identity == good.identity
    assert [e.kind for e in s.trace].count("policy_load_refused") == 3   # type: ignore[union-attr]
    widened = s.load(manifest(version="5", max_action_age_s=1.0), MODEL)
    assert not widened.loaded and "ceiling" in widened.reasons[0]


@policies
def test_non_official_policy_needs_explicit_authority() -> None:
    clock = ManualClock(start=100.0)
    auth = AuthorityService(clock, ["operator:root"])
    s = slot(clock, authority=auth)
    community = manifest(origin=PolicyOrigin.COMMUNITY, source="hf://org/walk@abc")
    assert not s.load(community, MODEL).loaded
    auth.grant(Delegation(delegation_id="dlg_pol", grantor="operator:root",
                          grantee="operator:ana", robots=("duck-1",),
                          skills=("policy:locomotion",), operations=("load_policy",)))
    assert not s.load(community, MODEL, principal="agent:x", delegation_id="dlg_pol").loaded
    assert s.load(community, MODEL, principal="operator:ana", delegation_id="dlg_pol").loaded
    # official needs none
    assert slot(clock).load(manifest(), MODEL).loaded


@policies
def test_two_step_start_checked_inference_and_exit_mode() -> None:
    clock = ManualClock()
    s = slot(clock)
    s.load(manifest(), MODEL, runner_factory=runner_factory)
    with pytest.raises(PolicyError):
        s.infer(OBS)                          # not started
    with pytest.raises(PolicyError):
        s.start("arm_x", "operator:ana")      # not armed
    token = s.arm("operator:ana")
    with pytest.raises(PolicyError):
        s.start("wrong", "operator:ana")
    s.start(token, "operator:ana")
    assert s.state is SlotState.RUNNING
    chunk = s.infer(OBS, observation_id="o1", capture_time=clock.now())
    assert chunk.provenance.artifact == s.identity and chunk.provenance.generation == 1
    assert len(s.accept(chunk)) == 2          # execution_steps of 4 predicted
    with pytest.raises(PolicyError, match="observation"):
        s.infer({"imu": [0.0, 0.1], "joints": [0.3, 0.4]})
    with pytest.raises(PolicyError):
        s.infer({**OBS, "imu": [math.nan, 0, 0]})
    with pytest.raises(PolicyError):
        s.infer({**OBS, "extra": [1.0]})
    assert s.stop("done") is ExitMode.DAMP
    assert s.state is SlotState.STOPPED


@policies
def test_arm_expires() -> None:
    clock = ManualClock()
    s = slot(clock, arm_timeout_s=1.0)
    s.load(manifest(), MODEL, runner_factory=runner_factory)
    token = s.arm("operator:ana")
    clock.advance(2.0)
    with pytest.raises(PolicyError, match="arming expired"):
        s.start(token, "operator:ana")
    assert s.state is SlotState.LOADED


@policies
def test_action_checked_after_inference() -> None:
    clock = ManualClock()
    s = slot(clock)

    def wild(m: PolicyManifest, b: bytes):  # type: ignore[no-untyped-def]
        return lambda obs: [[5.0, 0.0]] * 4
    s.load(manifest(), MODEL, runner_factory=wild)
    s.start(s.arm("op"), "op")
    with pytest.raises(PolicyError, match="above declared bounds"):
        s.infer(OBS)
    assert any(e.kind == "policy_check_failed" for e in s.trace)  # type: ignore[union-attr]


@policies
def test_stale_and_foreign_chunks_are_refused() -> None:
    clock = ManualClock()
    s = slot(clock)
    s.load(manifest(), MODEL, runner_factory=runner_factory)
    s.start(s.arm("op"), "op")
    chunk = s.infer(OBS, capture_time=clock.now())
    clock.advance(0.2)                        # past max_action_age_s = 0.1
    with pytest.raises(PolicyError) as exc:
        s.accept(chunk)
    assert exc.value.reason == "stale_chunk"
    fresh = s.infer(OBS)
    s.stop()
    s.load(manifest(version="1.0.1"), MODEL, runner_factory=runner_factory)
    s.start(s.arm("op"), "op")
    with pytest.raises(PolicyError) as exc:
        s.accept(fresh)                       # produced by the previous generation
    assert exc.value.reason == "foreign_chunk"
    assert ActionChunk.from_dict(fresh.to_dict()) == fresh


@policies
def test_rollback_restores_previous_policy() -> None:
    clock = ManualClock()
    s = slot(clock)
    first = s.load(manifest(), MODEL).identity
    s.load(manifest(version="1.1"), MODEL)
    assert s.rollback() == first and s.identity == first


@policies
def test_registry_stores_policy_with_model_blob(tmp_path: Path) -> None:
    reg = LocalRegistry(tmp_path / "reg")
    ref = reg.publish_policy(manifest(), MODEL)
    assert ref.name == "walk"
    m, model = reg.fetch_policy("walk", "1.0.0")
    assert model == MODEL and m == manifest()
    with pytest.raises(SchemaError):
        reg.publish_policy(manifest(version="2"), MODEL + b"!")
    blob = tmp_path / "reg" / "_blobs" / "sha256" / DIGEST.split(":")[1]
    blob.write_bytes(b"tampered")
    with pytest.raises(SchemaError, match="integrity"):
        reg.fetch_policy("walk", "1.0.0")
    assert [r.name for r in reg.list()] == ["walk"]


# -- action space ---------------------------------------------------------------------

@policies
def test_unified_action_space_named_slots_units_and_masks() -> None:
    space = default_action_space()
    a = space.pack({"right_arm_joint_pos": [0.1] * 7, "right_gripper_open": 1.0},
                   units={"right_arm_joint_pos": "rad"})
    assert len(a.values) == space.dim and sum(a.mask) == 8
    assert space.unpack(a) == {"right_arm_joint_pos": (0.1,) * 7, "right_gripper_open": (1.0,)}
    assert UnifiedAction.from_dict(a.to_dict()) == a
    with pytest.raises(SchemaError):
        space.pack({"right_arm_joint_pos": [0.1] * 7}, units={"right_arm_joint_pos": "deg"})
    with pytest.raises(SchemaError):
        space.pack({"no_such_slot": 1.0})
    with pytest.raises(SchemaError):
        space.pack({"base_linear_vel": [1.0]})
    with pytest.raises(SchemaError):
        ActionSpace(space_id="x", slots=(ActionSlot(name="a"), ActionSlot(name="a")))
    with pytest.raises(SchemaError):
        ActionSlot(name="a", unit="furlong")


# -- release activation ---------------------------------------------------------------

def release(version: str, counter: int, **kw: object) -> Release:
    fields: dict[str, object] = dict(
        release_id=f"rel_{version}", name="duck-daemon", version=version,
        security_counter=counter, confirm_checks=("health", "loop_hz"),
        targets=(ReleaseTarget(path=f"daemon/{version}", digest=DIGEST, length=len(MODEL)),))
    fields.update(kw)
    return Release(**fields)  # type: ignore[arg-type]


class Tick:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@releases
def test_trial_then_confirm_and_automatic_revert(tmp_path: Path) -> None:
    clock = Tick()
    trace = Trace(ManualClock())
    m = ActivationManager("duck-1", "daemon", clock=clock, path=tmp_path / "s.json",
                          trace=trace)
    m.stage(release("1.0", 1))
    assert m.boot() is ActivationStateKind.TRIAL
    assert not m.confirm({"health": True})            # loop_hz not observed
    assert not m.confirm({"health": True, "loop_hz": False})
    assert m.confirm({"health": True, "loop_hz": True})
    assert m.state.state is ActivationStateKind.CONFIRMED and m.state.security_counter == 1
    m.stage(release("1.1", 2))
    m.boot()
    assert m.state.active and m.state.active.version == "1.1"
    assert m.state.security_counter == 1              # rises only on confirm
    m.boot()                                          # rebooted without confirming
    assert m.state.active and m.state.active.version == "1.0"
    assert m.state.history[-1].kind == "reverted"
    # persisted and reloadable
    again = ActivationManager("duck-1", "daemon", clock=clock, path=tmp_path / "s.json")
    assert again.state == m.state
    assert ReleaseActivation.from_dict(m.state.to_dict()) == m.state
    assert "release_reverted" in [e.kind for e in trace]


@releases
def test_security_counter_blocks_downgrade_and_deadline_reverts(tmp_path: Path) -> None:
    clock = Tick()
    project = Project.init(tmp_path, name="t")
    m = ActivationManager("duck-1", "daemon", clock=clock, project=project)
    m.stage(release("2.0", 5))
    m.boot()
    m.confirm({"health": True, "loop_hz": True})
    with pytest.raises(ActivationError) as exc:
        m.stage(release("1.9", 4))
    assert exc.value.reason == "downgrade"
    m.stage(release("2.1", 5, confirm_within_s=60.0))
    m.boot()
    clock.t += 61
    with pytest.raises(ActivationError, match="deadline"):
        m.confirm({"health": True, "loop_hz": True})
    assert m.state.active and m.state.active.version == "2.0"
    kinds = [e.kind for e in project.ledger.query()]
    assert {"release.refused", "release.confirmed", "release.reverted"} <= set(kinds)
    with pytest.raises(SchemaError):
        release("3", 1, confirm_checks=())


# -- TUF metadata ---------------------------------------------------------------------

NOW = 1_800_000_000.0


class Repo:
    def __init__(self, *, threshold: int = 2) -> None:
        self.keys = {r: [HmacSigner(f"{r}-{i}".encode()) for i in range(3)]
                     for r in tuf.TOP_LEVEL_ROLES}
        self.thresholds = {r: threshold for r in tuf.TOP_LEVEL_ROLES}
        self.store = KeyStore()
        for signers in self.keys.values():
            for s in signers:
                self.store.add_hmac(s)
        self.root = self.sign("root", tuf.root_metadata(
            {r: [s.key for s in ss] for r, ss in self.keys.items()}, self.thresholds,
            version=1, expires=NOW + 365 * 86400))

    def sign(self, role: str, signed: dict, n: int | None = None) -> dict:  # type: ignore[type-arg]
        return tuf.sign(signed, self.keys[role][: n or self.thresholds[role]])

    def publish(self, targets: dict, version: int = 1, expires: float = NOW + 86400,  # type: ignore[type-arg]
                snap_v: int | None = None, ts_v: int | None = None) -> tuple[bytes, ...]:
        t = self.sign("targets", tuf.targets_metadata(targets, version=version, expires=expires))
        s = self.sign("snapshot", tuf.snapshot_metadata(t, version=snap_v or version,
                                                        expires=expires))
        ts = self.sign("timestamp", tuf.timestamp_metadata(s, version=ts_v or version,
                                                           expires=expires))
        return tuf.encode(ts), tuf.encode(s), tuf.encode(t)


@releases
def test_tuf_metadata_shapes_follow_the_spec() -> None:
    repo = Repo()
    signed = repo.root["signed"]
    assert set(signed) == {"_type", "spec_version", "consistent_snapshot", "version",
                           "expires", "keys", "roles"}
    assert set(signed["roles"]) == {"root", "targets", "snapshot", "timestamp"}
    kid, key = next(iter(signed["keys"].items()))
    assert set(key) == {"keytype", "scheme", "keyval"} and tuf.key_id(key) == kid
    assert signed["expires"].endswith("Z") and len(signed["expires"]) == 20
    ts, _s, t = repo.publish({"a.json": tuf.target_info(b"abc")})
    assert set(tuf.decode(ts)["signed"]["meta"]) == {"snapshot.json"}
    assert tuf.decode(t)["signed"]["targets"]["a.json"]["hashes"]["sha256"] == \
        hashlib.sha256(b"abc").hexdigest()


@releases
def test_tuf_client_threshold_rollback_freeze_and_target_checks() -> None:
    repo = Repo()
    client = TrustedSet(tuf.encode(repo.root), repo.store, now=NOW)
    ts, s, t = repo.publish({"a.json": tuf.target_info(b"abc")})
    client.refresh(timestamp=ts, snapshot=s, targets=t)
    assert client.verify_target("a.json", b"abc")["length"] == 3
    with pytest.raises(TufError) as exc:
        client.verify_target("a.json", b"abd")
    assert exc.value.reason == "mismatch"
    with pytest.raises(TufError):
        client.target("missing.json")
    # same version again: no update, no error
    assert client.update_timestamp(ts) is False
    # below threshold
    weak = tuf.sign(tuf.targets_metadata({}, version=2, expires=NOW + 10),
                    repo.keys["timestamp"][:1])
    with pytest.raises(TufError) as exc:
        TrustedSet(tuf.encode(repo.root), repo.store, now=NOW).update_timestamp(
            tuf.encode(weak))
    assert exc.value.reason in ("threshold", "bad_metadata")
    # rollback of timestamp
    ts2, s2, t2 = repo.publish({"a.json": tuf.target_info(b"abc")}, version=3)
    client.refresh(timestamp=ts2, snapshot=s2, targets=t2)
    with pytest.raises(TufError) as exc:
        client.update_timestamp(ts)
    assert exc.value.reason == "rollback"
    # freeze: expired metadata
    old = Repo()
    ts3, s3, t3 = old.publish({}, expires=NOW - 1)
    with pytest.raises(TufError) as exc:
        TrustedSet(tuf.encode(old.root), old.store, now=NOW).refresh(
            timestamp=ts3, snapshot=s3, targets=t3)
    assert exc.value.reason == "expired"
    # snapshot that does not match the timestamp
    ts4, _, _ = repo.publish({"a.json": tuf.target_info(b"abc")}, version=4)
    _, s5, _ = repo.publish({"b.json": tuf.target_info(b"b")}, version=4)
    client.update_timestamp(ts4)
    with pytest.raises(TufError) as exc:
        client.update_snapshot(s5)
    assert exc.value.reason == "mismatch"


@releases
def test_tuf_root_rotation_needs_old_and_new_thresholds() -> None:
    repo = Repo()
    client = TrustedSet(tuf.encode(repo.root), repo.store, now=NOW)
    new_keys = [HmacSigner(f"newroot-{i}".encode()) for i in range(2)]
    for k in new_keys:
        repo.store.add_hmac(k)
    role_keys = {r: [s.key for s in ss] for r, ss in repo.keys.items()}
    role_keys["root"] = [k.key for k in new_keys]
    v2 = tuf.root_metadata(role_keys, repo.thresholds, version=2, expires=NOW + 86400)
    only_new = tuf.sign(v2, new_keys)
    with pytest.raises(TufError):
        client.update_root(tuf.encode(only_new))
    both = tuf.sign(v2, [*repo.keys["root"][:2], *new_keys])
    client.update_root(tuf.encode(both))
    skip = tuf.sign(tuf.root_metadata(role_keys, repo.thresholds, version=4,
                                      expires=NOW + 86400), new_keys)
    with pytest.raises(TufError) as exc:
        client.update_root(tuf.encode(skip))
    assert exc.value.reason == "rollback"


@releases
def test_tuf_over_registry_assets_and_director_assignment(tmp_path: Path) -> None:
    reg = LocalRegistry(tmp_path / "reg")
    reg.publish_policy(manifest(), MODEL)
    reg.publish("daemon-release", "1.0", release("1.0", 1))
    targets = tuf.registry_targets(reg)
    assert set(targets) == {"walk/1.0.0.json", "daemon-release/1.0.json"}
    image = Repo()
    ts, s, t = image.publish(targets)
    image_client = TrustedSet(tuf.encode(image.root), image.store, now=NOW)
    image_client.refresh(timestamp=ts, snapshot=s, targets=t)
    assert tuf.verify_registry_asset(image_client, reg, "walk", "1.0.0") == manifest()

    director = Repo(threshold=1)
    assigned = tuf.director_targets({
        "duck-1": {"policy.locomotion": ("walk/1.0.0.json", targets["walk/1.0.0.json"])},
        "duck-2": {"daemon": ("daemon-release/1.0.json",
                              targets["daemon-release/1.0.json"])}})
    dts, ds, dt = director.publish(assigned)
    director_client = TrustedSet(tuf.encode(director.root), director.store, now=NOW)
    director_client.refresh(timestamp=dts, snapshot=ds, targets=dt)
    mine = tuf.assigned_targets(director_client, image_client, "duck-1")
    assert list(mine) == ["policy.locomotion"] and mine["policy.locomotion"]["path"] == \
        "walk/1.0.0.json"
    # director pointing at bytes the image repository does not vouch for
    forged = dict(assigned)
    forged["duck-1/policy.locomotion"] = {**assigned["duck-1/policy.locomotion"],
                                          "length": 1}
    dts2, ds2, dt2 = director.publish(forged, version=2)
    director_client.refresh(timestamp=dts2, snapshot=ds2, targets=dt2)
    with pytest.raises(TufError, match="disagree"):
        tuf.assigned_targets(director_client, image_client, "duck-1")


@releases
def test_signing_refuses_floats_and_unknown_keys() -> None:
    from agentic_robotics.releases.signing import SigningError, canonical_bytes
    with pytest.raises(SigningError):
        canonical_bytes({"x": 1.5})
    signer = HmacSigner(b"k")
    assert not KeyStore().verify(signer.key, signer.sign(b"d"), b"d")
    store = KeyStore()
    store.add_hmac(signer)
    assert store.verify(signer.key, signer.sign(b"d"), b"d")
    assert not store.verify(signer.key, signer.sign(b"d"), b"e")


@releases
def test_ed25519_signing_when_extra_installed() -> None:
    if not ed25519_available():
        pytest.skip("cryptography ([signing] extra) is not installed")
    from agentic_robotics.releases.signing import Ed25519Signer
    s = Ed25519Signer.generate()
    assert s.key["keytype"] == "ed25519" and len(s.key["keyval"]["public"]) == 64
    assert KeyStore().verify(s.key, s.sign(b"data"), b"data")
    assert not KeyStore().verify(s.key, s.sign(b"data"), b"other")


# -- parameters -----------------------------------------------------------------------

SCHEMA = ParameterSchema(name="drive.v1", applies_to="fake", parameters=(
    ParameterDef(name="max_speed", type=ParamType.FLOAT, display_name="Max speed",
                 unit="m/s", min=0.0, max=2.0, default=1.0),
    ParameterDef(name="mode", type=ParamType.ENUM, values={"0": "manual", "1": "auto"},
                 default=0, reboot_required=True),
    ParameterDef(name="features", type=ParamType.BITMASK,
                 bitmask={"0": "lidar", "1": "camera"}, default=0,
                 user_level=UserLevel.ADVANCED),
    ParameterDef(name="wheel_offset", type=ParamType.FLOAT, unit="mm", calibration=True),
    ParameterDef(name="enabled", type=ParamType.BOOL, default=True),
))


@params
def test_parameter_validation_units_ranges_values_bitmask() -> None:
    good = ParameterSet(robot_id="r1", schema_name="drive.v1",
                        values={"max_speed": 1.5, "mode": 1, "features": 3, "enabled": False})
    assert SCHEMA.validate(good) == []
    bad = ParameterSet(robot_id="r1", values={"max_speed": 3.0, "mode": 2, "features": 4,
                                              "enabled": 1, "unknown": 1})
    problems = SCHEMA.validate(bad)
    assert len(problems) == 5
    assert len(SCHEMA.validate(bad, allow_unknown=True)) == 4
    with pytest.raises(SchemaError):
        ParameterDef(name="x", type=ParamType.FLOAT, unit="parsec")
    with pytest.raises(SchemaError):
        ParameterDef(name="x", type=ParamType.ENUM)
    with pytest.raises(SchemaError):
        ParameterDef(name="x", type=ParamType.FLOAT, min=0, max=1, default=2.0)
    assert SCHEMA.get("features").label(3) == "lidar|camera"  # type: ignore[union-attr]
    assert ParameterSchema.from_dict(SCHEMA.to_dict()) == SCHEMA
    assert ParameterSet.from_dict(good.to_dict()) == good


@params
def test_parameter_diff_flags_reboot_and_calibration(tmp_path: Path) -> None:
    old = SCHEMA.defaults("r1")
    new = old.evolve(values={**old.values, "mode": 1, "wheel_offset": 0.4})
    changes = {c.name: c for c in SCHEMA.diff(old, new)}
    assert set(changes) == {"mode", "wheel_offset"}
    assert changes["mode"].reboot_required and changes["wheel_offset"].calibration
    project = Project.init(tmp_path, name="p")
    ref = record_parameter_snapshot(project, new, SCHEMA)
    assert ref.kind == "parameter_set" and ref.attributes["problems"] == []
    assert ref.entity_id in project.entities()


# -- datasets -------------------------------------------------------------------------

def episode(terminal: bool = True) -> Episode:
    steps = (
        Step(observation={"state": [0.0, 1.0]}, action=(0.1,), reward=0.0, discount=1.0,
             is_first=True, timestamp=0.0),
        Step(observation={"state": [0.1, 1.1]}, action=(0.2,), reward=1.0, discount=1.0,
             timestamp=0.1, metadata={"note": "x"}),
        Step(observation={"state": [0.2, 1.2]}, is_last=True, is_terminal=terminal,
             timestamp=0.2),
    )
    return Episode(metadata=EpisodeMetadata(episode_id="ep1", agent_id="policy:walk",
                                            environment_config={"floor": "carpet"},
                                            task="walk forward", robot_type="duck", fps=10.0),
                   steps=steps)


@datasets
def test_episode_flags_keep_truncation_distinct_from_termination() -> None:
    assert episode().terminated and not episode().truncated
    assert episode(terminal=False).truncated
    s = episode().steps
    with pytest.raises(SchemaError):
        Episode(metadata=EpisodeMetadata(episode_id="e"), steps=s[:2])   # no is_last
    with pytest.raises(SchemaError):
        Episode(metadata=EpisodeMetadata(episode_id="e"),
                steps=(s[0], Step(observation={}, is_terminal=True), s[2]))
    assert Episode.from_dict(episode().to_dict()) == episode()


@datasets
@pytest.mark.parametrize("terminal", [True, False])
def test_rlds_and_lerobot_conversions_are_lossless(terminal: bool) -> None:
    ep = episode(terminal)
    r = to_rlds(ep)
    assert {"observation", "action", "reward", "discount", "is_first", "is_last",
            "is_terminal"} <= set(r["steps"][0])
    assert from_rlds(json.loads(json.dumps(r))) == ep
    lr = to_lerobot(ep, episode_index=7, start_index=100)
    f0 = lr["frames"][0]
    assert {"timestamp", "frame_index", "episode_index", "index", "task_index"} <= set(f0)
    assert f0["observation.state"] == [0.0, 1.0] and f0["index"] == 100
    last = lr["frames"][-1]
    assert last["next.done"] is True and last["next.truncated"] is (not terminal)
    assert from_lerobot(json.loads(json.dumps(lr))) == ep


@datasets
def test_training_run_records_git_and_nan_check(tmp_path: Path) -> None:
    losses = [0.5, 0.4, math.nan, 0.3]
    bad = first_non_finite(losses)
    run = TrainingRun(run_id="run1", algorithm="ppo",
                      git=GitState(commit="abc", branch="main", dirty=False), seed=3,
                      nan_check=NanCheck(enabled=True, checked=("loss",),
                                         found_non_finite=bad is not None, first_step=bad),
                      config={"dr": {"friction": [0.5, 1.0]}}, metrics={"reward": 1.0})
    assert run.reproducible and not run.trustworthy and run.nan_check.first_step == 2
    assert TrainingRun.from_dict(run.to_dict()) == run
    project = Project.init(tmp_path, name="p")
    ep_ref = record_episode(project, episode())
    act = record_training_run(project, run.evolve(datasets=(ep_ref.entity_id,)))
    assert act.used == (ep_ref.entity_id,) and act.status.value == "failed"
    pol = record_policy(project, manifest(training_run=act.generated[0]), MODEL)
    assert pol.attributes["identity"] == manifest().identity()
    g = git_state(Path(__file__).parent)
    assert g is None or len(g.commit) == 40


# -- asset cards and conformance reports ----------------------------------------------

@cards
def test_asset_card_is_strict_and_lists_gaps() -> None:
    card = AssetCard(asset="walk@1.0.0#sha256:00", kind=AssetKind.POLICY, title="Walk",
                     license="Apache-2.0", intended_use=("flat indoor floors",))
    assert "no hazards stated for an asset that moves hardware" in card.problems()
    full = card.evolve(out_of_scope_use=("stairs",), limitations=("carpet untested",),
                       hazards=("falls",), evaluations=(EvaluationEvidence(
                           name="flat", metric="success", value=0.9, seeds=(1, 2),
                           evidence="ledger:12"),))
    assert full.problems() == []
    assert AssetCard.from_dict(full.to_dict()) == full
    with pytest.raises(SchemaError):
        card.evolve(license="not a license!")
    with pytest.raises(SchemaError):
        card.evolve(intended_use=())


@cards
def test_conformance_report_record_cannot_assert_its_own_pass() -> None:
    with pytest.raises(SchemaError):
        ConformanceReportRecord(robot_id="r", adapter_kind="fake", suite_version="1.0",
                                air_version="0", passed=True,
                                outcomes=(ConformanceCheck("x", False),))


@cards
async def test_conformance_suite_writes_versioned_report(tmp_path: Path) -> None:
    from agentic_robotics.conformance import SUITE_VERSION, check_adapter
    from agentic_robotics.integrations.fake import fake_skill_set
    from agentic_robotics.robot import Robot
    from agentic_robotics.skills import SkillRegistry
    from tests.conftest import manifest as robot_manifest

    robot = Robot.from_manifest(robot_manifest())
    report = await check_adapter(robot, SkillRegistry(fake_skill_set().skills))
    rec = report.to_record(generated_at=1.0)
    assert rec.passed == report.passed and rec.suite_version == SUITE_VERSION
    assert rec.contract_versions["capability_report"]
    path = tmp_path / "c.json"
    rec.save(path)
    assert load_any(json.loads(path.read_text("utf-8"))) == rec


# -- CLI ------------------------------------------------------------------------------

@cards
def test_cli_policy_params_dataset_release(tmp_path: Path,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    side = tmp_path / "walk.policy.json"
    manifest().save(side)
    model = tmp_path / "walk.onnx"
    model.write_bytes(MODEL)
    reqs = tmp_path / "reqs.json"
    reqs.write_text(json.dumps({"model_api": "duck/1", "robot_model": "duck-v2",
                                "joint_order": ["knee", "hip"]}), "utf-8")
    assert main(["policy", "check", str(side), "--model", str(model)]) == 0
    assert main(["policy", "check", str(side), "--requires", str(reqs)]) == 1
    assert "REFUSE joint_order differs" in capsys.readouterr().out
    assert main(["policy", "publish", str(side), str(model), "--root",
                 str(tmp_path / "reg"), "--project", str(tmp_path)]) == 0

    schema, pset = tmp_path / "schema.json", tmp_path / "set.json"
    SCHEMA.save(schema)
    SCHEMA.defaults("r1").save(pset)
    assert main(["params", "validate", str(schema), str(pset)]) == 0
    other = tmp_path / "set2.json"
    SCHEMA.defaults("r1").evolve(values={"mode": 1}).save(other)
    assert main(["params", "diff", str(schema), str(pset), str(other)]) == 0
    assert "reboot required" in capsys.readouterr().out

    ep = tmp_path / "ep.json"
    episode(False).save(ep)
    rl = tmp_path / "ep.rlds.json"
    assert main(["dataset", "convert", str(ep), "--to", "rlds", "--out", str(rl)]) == 0
    assert main(["dataset", "check", str(rl)]) == 0
    assert "truncated" in capsys.readouterr().out

    rel, state = tmp_path / "rel.json", tmp_path / "state.json"
    release("1.0", 1).save(rel)
    base = ["--robot", "duck-1", "--slot", "daemon", "--state", str(state),
            "--project", str(tmp_path)]
    assert main(["release", "stage", str(rel), *base]) == 0
    assert main(["release", "boot", *base]) == 0
    assert main(["release", "confirm", "--check", "health=pass", *base]) == 1
    assert main(["release", "confirm", "--check", "health=pass", "--check",
                 "loop_hz=pass", *base]) == 0
    assert ReleaseActivation.load(state).state is ActivationStateKind.CONFIRMED
    assert main(["release", "targets", "--root", str(tmp_path / "reg")]) == 0
    assert '"walk/1.0.0.json"' in capsys.readouterr().out


@cards
def test_documents_validate_against_exported_json_schemas() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    from agentic_robotics.core.jsonschema import contract_schema

    docs = [manifest(), SCHEMA, SCHEMA.defaults("r1"), episode(), release("1.0", 1),
            default_action_space(), AssetCard(asset="a", kind=AssetKind.MODEL, title="t",
                                              license="MIT", intended_use=("x",))]
    for d in docs:
        jsonschema.validate(json.loads(d.to_json()), contract_schema(type(d)))
