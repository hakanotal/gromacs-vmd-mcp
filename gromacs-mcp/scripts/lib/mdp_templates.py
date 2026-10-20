#!/usr/bin/env python3
"""MDP parameter templates, rendering, parsing and linting.

Stages: em, nvt, npt, nve, ions, free_energy, umbrella.

Used by the create_mdp and validate_mdp tools and by the full_md workflow.
Option names accept underscores or hyphens (tau_t and tau-t are the same).
"""

import re
from typing import Any, Dict, List, Optional, Tuple

STAGES = ("em", "nvt", "npt", "nve", "ions", "free_energy", "umbrella")

_COMMON = {
    "cutoff-scheme": "Verlet",
    "pbc": "xyz",
    "coulombtype": "PME",
    "rcoulomb": 1.0,
    "rvdw": 1.0,
    "DispCorr": "EnerPres",
}

_DYNAMICS = {
    **_COMMON,
    "integrator": "md",
    "dt": 0.002,
    "nsteps": 50000,
    "nstcomm": 100,
    "nstlog": 1000,
    "nstenergy": 500,
    "nstcalcenergy": 100,
    "nstxout-compressed": 5000,
    "compressed-x-grps": "System",
    "constraints": "h-bonds",
    "constraint-algorithm": "lincs",
    "tcoupl": "V-rescale",
    "tc-grps": "System",
    "tau-t": 0.1,
    "ref-t": 300,
}

_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "em": {
        **_COMMON,
        "integrator": "steep",
        "emtol": 1000.0,
        "emstep": 0.01,
        "nsteps": 5000,
        "nstenergy": 10,
        "nstlog": 100,
    },
    "ions": {
        **_COMMON,
        "integrator": "steep",
        "emtol": 1000.0,
        "emstep": 0.01,
        "nsteps": 50,
    },
    "nvt": {
        **_DYNAMICS,
        "continuation": "no",
        "gen-vel": "yes",
        "gen-temp": 300,
        "gen-seed": -1,
        "pcoupl": "no",
    },
    "npt": {
        **_DYNAMICS,
        "continuation": "yes",
        "gen-vel": "no",
        "pcoupl": "C-rescale",
        "pcoupltype": "isotropic",
        "tau-p": 2.0,
        "ref-p": 1.0,
        "compressibility": 4.5e-5,
        "refcoord-scaling": "com",
    },
    "nve": {
        **_DYNAMICS,
        "continuation": "yes",
        "gen-vel": "no",
        "tcoupl": "no",
        "tc-grps": None,
        "tau-t": None,
        "ref-t": None,
        "pcoupl": "no",
        "nsteps": 25000,
    },
    "free_energy": {
        **_DYNAMICS,
        "integrator": "sd",
        "tcoupl": "no",
        "tc-grps": "System",
        "tau-t": 1.0,
        "ref-t": 300,
        "pcoupl": "C-rescale",
        "pcoupltype": "isotropic",
        "tau-p": 2.0,
        "ref-p": 1.0,
        "compressibility": 4.5e-5,
        "refcoord-scaling": "com",
        "continuation": "yes",
        "free-energy": "yes",
        "couple-moltype": "MOL",
        "couple-lambda0": "vdw-q",
        "couple-lambda1": "none",
        "couple-intramol": "no",
        "init-lambda-state": 0,
        # charges are switched off first, then van der Waals with soft-core
        "coul-lambdas": "0.00 0.25 0.50 0.75 1.00 1.00 1.00 1.00 1.00 1.00 1.00",
        "vdw-lambdas": "0.00 0.00 0.00 0.00 0.00 0.20 0.40 0.60 0.80 0.90 1.00",
        "sc-alpha": 0.5,
        "sc-power": 1,
        "sc-r-power": 6,
        "nstdhdl": 100,
        "calc-lambda-neighbors": -1,
        "dhdl-print-energy": "total",
    },
    "umbrella": {
        **_DYNAMICS,
        "continuation": "yes",
        "pcoupl": "C-rescale",
        "pcoupltype": "isotropic",
        "tau-p": 2.0,
        "ref-p": 1.0,
        "compressibility": 4.5e-5,
        "refcoord-scaling": "com",
        "pull": "yes",
        "pull-ncoords": 1,
        "pull-ngroups": 2,
        "pull-group1-name": "GROUP_A",
        "pull-group2-name": "GROUP_B",
        "pull-coord1-type": "umbrella",
        "pull-coord1-geometry": "distance",
        "pull-coord1-groups": "1 2",
        "pull-coord1-dim": "Y Y Y",
        "pull-coord1-rate": 0.0,
        "pull-coord1-k": 1000.0,
        "pull-coord1-start": "yes",
        "pull-nstxout": 500,
        "pull-nstfout": 500,
    },
}

EXPLANATIONS: Dict[str, str] = {
    "integrator": "steep minimizes energy; md is leap-frog dynamics; sd adds stochastic temperature control",
    "dt": "time step in ps; 0.002 is safe with constraints = h-bonds, use 0.001 without constraints",
    "nsteps": "number of steps; simulated time = nsteps * dt",
    "emtol": "minimization stops when the largest force is below this (kJ/mol/nm)",
    "emstep": "initial step size for minimization (nm)",
    "cutoff-scheme": "Verlet is the only supported scheme (group was removed)",
    "coulombtype": "PME handles long range electrostatics",
    "rcoulomb": "real space Coulomb cutoff (nm); the force field normally dictates it",
    "rvdw": "van der Waals cutoff (nm); the force field normally dictates it",
    "DispCorr": "long range dispersion correction for energy and pressure",
    "constraints": "h-bonds constrains bonds with hydrogens, which allows dt = 0.002",
    "constraint-algorithm": "LINCS for chains, water uses SETTLE automatically",
    "tcoupl": "V-rescale gives correct canonical sampling; Berendsen does not",
    "tc-grps": "groups with their own thermostat; System is fine for homogeneous systems",
    "tau-t": "thermostat coupling time (ps)",
    "ref-t": "target temperature (K)",
    "gen-vel": "generate initial velocities from a Maxwell distribution (only for the first dynamics stage)",
    "gen-temp": "temperature for generated velocities (K)",
    "gen-seed": "random seed, -1 picks a new one each run",
    "continuation": "yes when continuing from a previous run so constraints are not reapplied",
    "pcoupl": "C-rescale gives correct isothermal-isobaric sampling and is stable for equilibration; no disables",
    "pcoupltype": "isotropic scales the box uniformly",
    "tau-p": "barostat coupling time (ps)",
    "ref-p": "target pressure (bar)",
    "compressibility": "isothermal compressibility (1/bar); 4.5e-5 is water",
    "nstenergy": "write energies every N steps",
    "nstlog": "write the log every N steps",
    "nstxout-compressed": "write compressed (xtc) coordinates every N steps; 0 writes none",
    "nstcalcenergy": "compute energies every N steps; nstenergy should be a multiple of it",
    "free-energy": "enables alchemical free energy calculation",
    "couple-moltype": "molecule type to decouple, must match a [ moleculetype ] name",
    "pull": "enables the pull code (umbrella sampling, steered MD)",
    "pull-group1-name": "index group names; they must exist in the index file",
    "pull-coord1-k": "umbrella force constant (kJ/mol/nm^2)",
    "pull-coord1-rate": "pulling rate (nm/ps); 0 holds the reference fixed (umbrella window)",
    "refcoord-scaling": "how position restraint reference points follow box scaling; com is the standard choice",
    "coul-lambdas": "charge scaling per lambda state; switched off before van der Waals",
    "vdw-lambdas": "van der Waals scaling per lambda state, with soft-core to avoid singularities",
}

_DESCRIPTIONS = {
    "em": "Steepest descent energy minimization",
    "ions": "Short minimization used as the input for genion",
    "nvt": "Constant volume equilibration with V-rescale thermostat and generated velocities",
    "npt": "Constant pressure equilibration or production with C-rescale barostat",
    "nve": "Microcanonical run for energy conservation tests",
    "free_energy": "Alchemical decoupling window (set couple-moltype and init-lambda-state per window)",
    "umbrella": "Umbrella sampling window with the pull code (set the two pull groups)",
}


def _norm(key: str) -> str:
    return key.replace("_", "-")


def template_names() -> Dict[str, str]:
    return dict(_DESCRIPTIONS)


def get_template(stage: str) -> Dict[str, Any]:
    if stage not in _TEMPLATES:
        raise ValueError(f"Unknown stage '{stage}'. Available: {', '.join(STAGES)}")
    return dict(_TEMPLATES[stage])


def build_params(stage: str, overrides: Optional[Dict[str, Any]] = None,
                 time_ps: Optional[float] = None, temperature: Optional[float] = None,
                 pressure: Optional[float] = None) -> Dict[str, Any]:
    """
    Template plus overrides.

    Convenience arguments: time_ps (sets nsteps from dt), temperature (ref-t and gen-temp),
    pressure (ref-p). A value of None in overrides removes the key.
    """
    params = get_template(stage)
    over = {_norm(k): v for k, v in (overrides or {}).items()}
    params.update({k: v for k, v in over.items()})

    params = {k: v for k, v in params.items() if v is not None}
    groups = str(params["tc-grps"]).split() if params.get("tc-grps") else []
    if temperature is not None:
        if groups:
            params["ref-t"] = " ".join([str(temperature)] * len(groups))
        if "gen-temp" in params:
            params["gen-temp"] = temperature
    if len(groups) > 1:
        # one value per temperature coupling group; a single value is repeated
        for key in ("tau-t", "ref-t"):
            if key in params and len(str(params[key]).split()) == 1:
                params[key] = " ".join([str(params[key])] * len(groups))
    if pressure is not None and "ref-p" in params:
        params["ref-p"] = pressure
    if time_ps is not None:
        dt = float(params.get("dt", 0.002))
        params["nsteps"] = int(round(time_ps / dt))
    return params


def render_mdp(params: Dict[str, Any], title: Optional[str] = None) -> str:
    """Render a params dict as .mdp text."""
    lines = []
    if title:
        lines.append(f"; {title}")
    width = max((len(k) for k in params), default=0)
    for key, value in params.items():
        lines.append(f"{key.ljust(width)} = {value}")
    return "\n".join(lines) + "\n"


def parse_mdp(text: str) -> Dict[str, str]:
    """Parse .mdp text into {key: value}. Comments and blank lines are ignored."""
    params: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        params[_norm(key.strip())] = value.strip()
    return params


def explain(params: Dict[str, Any]) -> Dict[str, str]:
    """Explanations for the keys present in params."""
    return {k: EXPLANATIONS[k] for k in params if k in EXPLANATIONS}


def _num(params: Dict[str, Any], key: str) -> Optional[float]:
    try:
        return float(str(params[key]).split()[0])
    except (KeyError, ValueError, IndexError):
        return None


def lint_params(params: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Check an mdp parameter set for common mistakes.

    Returns a list of {level: error|warning|note, key, message}.
    """
    p = {_norm(k): v for k, v in params.items()}
    issues: List[Dict[str, str]] = []

    def add(level: str, key: str, msg: str) -> None:
        issues.append({"level": level, "key": key, "message": msg})

    integrator = str(p.get("integrator", "md")).lower()
    minimizing = integrator in ("steep", "cg", "l-bfgs")
    tcoupl = str(p.get("tcoupl", "no")).lower()
    pcoupl = str(p.get("pcoupl", "no")).lower()
    dt = _num(p, "dt")
    nsteps = _num(p, "nsteps")

    if str(p.get("cutoff-scheme", "Verlet")).lower() == "group":
        add("error", "cutoff-scheme", "The group scheme was removed; use Verlet")

    if minimizing:
        if pcoupl != "no" or tcoupl != "no":
            add("warning", "pcoupl", "Temperature and pressure coupling are ignored during minimization; remove them")
        if p.get("gen-vel", "no") == "yes":
            add("note", "gen-vel", "gen-vel has no effect during minimization")
    else:
        if dt is not None and nsteps is not None:
            add("note", "nsteps", f"Simulated time is {dt * nsteps:.3f} ps ({nsteps:g} steps of {dt:g} ps)")
        constraints = str(p.get("constraints", "none")).lower()
        if constraints in ("none", "") and dt is not None and dt > 0.0011:
            add("warning", "dt", "dt above 1 fs without constraints is unstable; set constraints = h-bonds or lower dt")
        if tcoupl == "berendsen":
            add("warning", "tcoupl", "Berendsen does not sample a correct ensemble; use V-rescale")
        if pcoupl == "berendsen":
            add("warning", "pcoupl", "Berendsen barostat does not sample a correct ensemble; use C-rescale for equilibration")
        if pcoupl != "no" and tcoupl == "no" and integrator != "sd":
            add("warning", "tcoupl", "Pressure coupling without temperature coupling lets the system heat up")
        tau_p = _num(p, "tau-p")
        if pcoupl in ("c-rescale", "parrinello-rahman") and tau_p is not None and tau_p < 1.0:
            add("warning", "tau-p", "tau-p below 1 ps can make the box oscillate; 2 ps is a safe choice")
        needs_groups = tcoupl not in ("no", "") or integrator in ("sd", "bd")
        if needs_groups and "tc-grps" not in p:
            add("error", "tc-grps", f"{'integrator ' + integrator if integrator in ('sd', 'bd') else 'tcoupl'} "
                                    "needs tc-grps, tau-t and ref-t")
        if needs_groups and ("tau-t" not in p or "ref-t" not in p):
            add("error", "tau-t", "temperature coupling needs tau-t and ref-t (one value per tc-grps group)")
        if "tc-grps" in p:
            n = len(str(p["tc-grps"]).split())
            for key in ("tau-t", "ref-t"):
                if key in p and len(str(p[key]).split()) != n:
                    add("error", key, f"{key} has {len(str(p[key]).split())} value(s) but tc-grps has {n} group(s)")
        if "POSRES" in str(p.get("define", "")) and pcoupl != "no" and "refcoord-scaling" not in p:
            add("warning", "refcoord-scaling", "Position restraints with pressure coupling need refcoord-scaling "
                                               "(com is the usual choice); grompp warns otherwise")
        if p.get("gen-vel") == "yes" and p.get("continuation") == "yes":
            add("warning", "gen-vel", "gen-vel = yes together with continuation = yes discards velocities from the previous run")
        if "nstxout-compressed" in p or "nstxout" in p:
            xc = _num(p, "nstxout-compressed") or 0
            xo = _num(p, "nstxout") or 0
            if xc == 0 and xo == 0:
                add("warning", "nstxout-compressed", "No trajectory is written (nstxout and nstxout-compressed are 0)")
        else:
            add("note", "nstxout-compressed", "No trajectory output is configured; set nstxout-compressed to analyze the run")

    nstenergy = _num(p, "nstenergy")
    nstcalc = _num(p, "nstcalcenergy")
    if nstenergy and nstcalc and nstenergy % nstcalc != 0:
        add("warning", "nstenergy", "nstenergy should be a multiple of nstcalcenergy")

    if str(p.get("free-energy", "no")).lower() == "yes":
        if "couple-moltype" not in p:
            add("error", "couple-moltype", "free-energy = yes requires couple-moltype")
        if "vdw-lambdas" not in p and "coul-lambdas" not in p:
            add("error", "vdw-lambdas", "free-energy = yes requires lambda arrays")
    if str(p.get("pull", "no")).lower() == "yes":
        for key in ("pull-group1-name", "pull-group2-name"):
            if str(p.get(key, "")).startswith("GROUP_"):
                add("warning", key, "Placeholder pull group name; set it to a group in your index file")
    return issues


def render_stage(stage: str, overrides: Optional[Dict[str, Any]] = None, **kwargs) -> Tuple[str, Dict[str, Any]]:
    """Render a stage to (mdp_text, params)."""
    params = build_params(stage, overrides, **kwargs)
    return render_mdp(params, title=f"{stage}: {_DESCRIPTIONS[stage]}"), params
