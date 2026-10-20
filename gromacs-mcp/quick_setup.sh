#!/bin/bash
#===============================================================================
# GROMACS MCP setup
#===============================================================================
# Creates ./env with the Python dependencies and makes sure GROMACS is available.
#
#   bash quick_setup.sh [options]
#
# GROMACS is found in this order: $GMX_MCP_GMX, gmx on PATH (Homebrew, module, system
# install), then installed from conda-forge into ./env when conda or mamba exists.
# Conda is only needed when GROMACS is not installed already.
#
# Options:
#   --skip-env        Do not create or update ./env
#   --skip-gromacs    Do not try to install GROMACS
#   --dev             Also install test dependencies (pytest, matplotlib)
#   --plots           Also install matplotlib (plot_xvg tool)
#   --gromacs VER     conda-forge GROMACS version to install (default: latest available)
#   -h, --help        Show this help
#===============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_DIR="${SCRIPT_DIR}/env"
PYTHON_BIN="${PYTHON_BIN:-python3}"
GROMACS_VERSION=""
SKIP_ENV=false
SKIP_GROMACS=false
EXTRA_REQS=""

info() { echo "[info] $1"; }
ok() { echo "[ok] $1"; }
warn() { echo "[warning] $1"; }
fail() { echo "[error] $1"; exit 1; }

while [[ $# -gt 0 ]]; do
    case $1 in
        --skip-env) SKIP_ENV=true; shift ;;
        --skip-gromacs) SKIP_GROMACS=true; shift ;;
        --dev) EXTRA_REQS="${SCRIPT_DIR}/requirements-dev.txt"; shift ;;
        --plots) EXTRA_REQS="${EXTRA_REQS:-matplotlib}"; shift ;;
        --gromacs) GROMACS_VERSION="$2"; shift 2 ;;
        -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
        *) warn "Unknown option: $1"; shift ;;
    esac
done

conda_cmd() {
    if command -v mamba >/dev/null 2>&1; then echo mamba
    elif command -v conda >/dev/null 2>&1; then echo conda
    fi
}

find_gmx() {
    if [ -n "$GMX_MCP_GMX" ] && [ -x "$GMX_MCP_GMX" ]; then echo "$GMX_MCP_GMX"; return; fi
    if command -v gmx >/dev/null 2>&1; then command -v gmx; return; fi
    if command -v gmx_mpi >/dev/null 2>&1; then command -v gmx_mpi; return; fi
    if [ -x "${ENV_DIR}/bin/gmx" ]; then echo "${ENV_DIR}/bin/gmx"; return; fi
}

# 1. Python environment
if [ "$SKIP_ENV" = false ]; then
    if [ ! -x "${ENV_DIR}/bin/python" ]; then
        GMX_NOW="$(find_gmx)"
        CONDA="$(conda_cmd)"
        if [ -z "$GMX_NOW" ] && [ -n "$CONDA" ] && [ "$SKIP_GROMACS" = false ]; then
            info "Creating conda environment in ${ENV_DIR} (GROMACS will be installed into it)"
            "$CONDA" create -y -p "$ENV_DIR" -c conda-forge "python>=3.10" pip
        else
            info "Creating virtual environment in ${ENV_DIR} with ${PYTHON_BIN}"
            "$PYTHON_BIN" -m venv "$ENV_DIR"
        fi
    fi
    "${ENV_DIR}/bin/python" -m pip install --upgrade pip >/dev/null
    "${ENV_DIR}/bin/python" -m pip install -r "${SCRIPT_DIR}/requirements.txt"
    if [ -n "$EXTRA_REQS" ]; then
        if [ -f "$EXTRA_REQS" ]; then "${ENV_DIR}/bin/python" -m pip install -r "$EXTRA_REQS"
        else "${ENV_DIR}/bin/python" -m pip install $EXTRA_REQS; fi
    fi
    ok "Python dependencies installed"
fi

# 2. GROMACS
GMX="$(find_gmx)"
if [ -z "$GMX" ] && [ "$SKIP_GROMACS" = false ]; then
    CONDA="$(conda_cmd)"
    if [ -n "$CONDA" ] && [ -d "${ENV_DIR}/conda-meta" ]; then
        SPEC="gromacs"; [ -n "$GROMACS_VERSION" ] && SPEC="gromacs=${GROMACS_VERSION}"
        info "Installing ${SPEC} from conda-forge into ${ENV_DIR}"
        "$CONDA" install -y -p "$ENV_DIR" -c conda-forge "$SPEC"
        GMX="$(find_gmx)"
    elif [ "$(uname)" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
        warn "GROMACS not found. Install it with: brew install gromacs"
    else
        warn "GROMACS not found. Install it (conda-forge, your package manager or a module) or set GMX_MCP_GMX."
    fi
fi

# 3. Verify
"${ENV_DIR}/bin/python" -c "import fastmcp, loguru; print('fastmcp', fastmcp.__version__)"
if [ -n "$GMX" ]; then
    "$GMX" --version 2>/dev/null | grep -E "GROMACS version" || true
    ok "GROMACS: $GMX"
    if [ ! -f "${SCRIPT_DIR}/examples/data/sample.tpr" ]; then
        info "Generating example data"
        GMX_MCP_GMX="$GMX" "${ENV_DIR}/bin/python" "${SCRIPT_DIR}/scripts/make_example_data.py" --tpr-only >/dev/null \
            && ok "examples/data/sample.tpr built"
    fi
else
    warn "Server will start, but GROMACS tools will fail until gmx is available"
fi

echo ""
echo "Register with Claude Code:"
echo "  claude mcp add gromacs -- ${ENV_DIR}/bin/python ${SCRIPT_DIR}/src/server.py"
echo ""
echo "Or add to .mcp.json:"
ENV_LINE=""
if [ -n "$GMX" ]; then
    ENV_LINE=",
      \"env\": {\"GMX_MCP_GMX\": \"${GMX}\"}"
fi
cat <<JSON
{
  "mcpServers": {
    "gromacs": {
      "command": "${ENV_DIR}/bin/python",
      "args": ["${SCRIPT_DIR}/src/server.py"]${ENV_LINE}
    }
  }
}
JSON
