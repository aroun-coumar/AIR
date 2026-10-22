"""Optuna as a *sampler* for :class:`~agentic_robotics.engineering.search.Search`
(extra ``[optuna]``). AIR stays the system of record: Optuna proposes configurations
through ``study.ask()``; every trial, including failed and pruned ones, is an AIR
ExperimentRecord; the SearchRecord stores the sampler class, its seed and the Optuna
version, which an Optuna study does not persist.

Verified against optuna ``study/_tell.py``: ``tell(trial, values=None, state=None)``
requires values for COMPLETE and refuses them for PRUNED and FAIL.

    sampler = OptunaSampler(directions=["minimize"], seed=0)   # imports optuna lazily
    Search("s1", ..., sampler=sampler).run(20)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..core.errors import PluginError
from .search import CandidateSummary, Direction, Objective, Param, TrialStatus, candidate_id

__requirements__ = ("REQ04", "G04")


def _import_optuna() -> Any:
    try:
        import optuna  # type: ignore[import-not-found, unused-ignore]
    except ImportError as exc:
        raise PluginError("Optuna is not installed; install agentic-robotics[optuna]"
                          ) from exc
    return optuna


class OptunaSampler:
    """Wraps an Optuna study. Pass ``study`` and ``trial_state`` to inject them (tests,
    or a study you configured yourself); otherwise one is created with ``sampler_name``
    from ``optuna.samplers`` and ``seed``."""

    name = "optuna"

    def __init__(self, *, directions: Sequence[str | Direction] = ("minimize",),
                 seed: int | None = 0, sampler_name: str = "TPESampler",
                 study: Any = None, trial_state: Any = None, version: str | None = None
                 ) -> None:
        self.seed = seed
        if study is None or trial_state is None:
            optuna = _import_optuna()
            if study is None:
                sampler = getattr(optuna.samplers, sampler_name)(seed=seed)
                study = optuna.create_study(directions=[str(Direction(d).value)
                                                        for d in directions],
                                            sampler=sampler)
            trial_state = trial_state or optuna.trial.TrialState
            version = version or str(optuna.__version__)
        self.study = study
        self.trial_state = trial_state
        self.sampler_class = type(getattr(study, "sampler", None)).__name__ \
            if hasattr(study, "sampler") else sampler_name
        self.version = f"optuna {version or 'unknown'} {self.sampler_class}"
        self._pending: dict[str, Any] = {}

    def propose(self, space: Sequence[Param],
                history: Sequence[CandidateSummary]) -> dict[str, Any] | None:
        trial = self.study.ask()
        config: dict[str, Any] = {}
        for p in space:
            if p.kind == "choice":
                config[p.name] = trial.suggest_categorical(p.name, list(p.choices))
            elif p.kind == "int":
                config[p.name] = trial.suggest_int(p.name, int(p.low or 0), int(p.high or 0),
                                                   log=p.log)
            else:
                config[p.name] = trial.suggest_float(p.name, float(p.low or 0.0),
                                                     float(p.high or 0.0), log=p.log)
        key = candidate_id(config)
        if key in self._pending:
            # Optuna proposed a configuration AIR has pending: tell it as failed so the
            # study does not keep a dangling RUNNING trial.
            self.study.tell(trial, state=self.trial_state.FAIL)
        else:
            self._pending[key] = trial
        return config

    def observe(self, config: Mapping[str, Any], result: CandidateSummary,
                objectives: Sequence[Objective]) -> None:
        trial = self._pending.pop(candidate_id(config), None)
        if trial is None:
            return
        trial.set_user_attr("air_candidate", result.candidate)
        trial.set_user_attr("air_constraints", list(result.constraints.values()))
        if result.status == TrialStatus.COMPLETE:
            self.study.tell(trial, values=[result.values[o.metric] for o in objectives])
        elif result.status == TrialStatus.PRUNED:
            self.study.tell(trial, state=self.trial_state.PRUNED)
        else:
            self.study.tell(trial, state=self.trial_state.FAIL)
