#!/usr/bin/env python3
"""Shared utilities for the GROMACS MCP server.

Result envelope (every tool returns a flat dict):
    status        "success" or "error"
    error         message, only when status is "error"
    warnings      list of strings (always present)
    output_files  list of strings (always present)
    command       the gmx command line, when one was run
    truncated     names of fields that were shortened, when any were
plus tool specific fields next to these.

Environment variables:
    GMX_MCP_HOME       base directory for jobs/, runs/, results/, logs/ (default: repository root,
                       or <GMX_MCP_WORKDIR>/gromacs_mcp when a sandbox is set)
    GMX_MCP_LOG_DIR    log directory (default: <home>/logs)
    GMX_MCP_RESULTS_DIR  default output directory for tools that write files
    GMX_MCP_MAX_CHARS  per field character limit in results (default 20000)
"""

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

MCP_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_MAX_CHARS = 20000
MAX_LIST_ITEMS = 2000


# ==============================================================================
# Locations
# ==============================================================================
if str(MCP_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(MCP_ROOT / "scripts"))

from lib.gromacs_utils import get_home, get_results_dir, get_runs_dir  # noqa: E402,F401  (defined once there)


def get_log_dir() -> Path:
    return Path(os.environ.get("GMX_MCP_LOG_DIR", get_home() / "logs")).expanduser()


def get_example_data_path() -> Path:
    """Path to the bundled example data directory."""
    return MCP_ROOT / "examples" / "data"


# ==============================================================================
# Validation
# ==============================================================================
def validate_input_file(file_path: str, expected_extension: Optional[str] = None) -> Dict[str, Any]:
    """Check that a file exists, is a file, has the expected extension and is allowed by GMX_MCP_WORKDIR."""
    try:
        from lib.gromacs_utils import resolve_in_workdir, PathNotAllowedError
        try:
            path = resolve_in_workdir(file_path)
        except PathNotAllowedError as e:
            return {"valid": False, "error": str(e)}

        if not path.exists():
            return {"valid": False, "error": f"File not found: {file_path}"}
        if not path.is_file():
            return {"valid": False, "error": f"Path is not a file: {file_path}"}
        if expected_extension and path.suffix.lower() != expected_extension.lower():
            return {"valid": False, "error": f"Expected {expected_extension} file, got {path.suffix}"}
        return {"valid": True, "file_size": path.stat().st_size, "absolute_path": str(path)}
    except Exception as e:
        return {"valid": False, "error": f"Validation error: {str(e)}"}


def setup_output_directory(output_path: Optional[str], default_name: str = "output") -> Dict[str, Any]:
    """Create the output directory. A path with a suffix is treated as a file path."""
    try:
        from lib.gromacs_utils import resolve_in_workdir, PathNotAllowedError
        if output_path:
            path = Path(output_path)
            if path.suffix:
                output_dir, output_file = path.parent, path
            else:
                output_dir, output_file = path, None
        else:
            output_dir, output_file = get_results_dir() / default_name, None

        try:
            output_dir = resolve_in_workdir(output_dir)
            if output_file:
                output_file = resolve_in_workdir(output_file)
        except PathNotAllowedError as e:
            return {"success": False, "error": str(e)}

        output_dir.mkdir(parents=True, exist_ok=True)
        return {
            "success": True,
            "output_dir": str(output_dir),
            "output_file": str(output_file) if output_file else None,
        }
    except Exception as e:
        return {"success": False, "error": f"Failed to setup output directory: {str(e)}"}


# ==============================================================================
# Result envelope
# ==============================================================================
def _shorten(value: Any, limit: int, path: str, truncated: List[str], depth: int = 0) -> Any:
    if isinstance(value, str):
        if len(value) > limit:
            truncated.append(path)
            keep = limit // 2
            omitted = len(value) - 2 * keep
            return f"{value[:keep]}\n... [{omitted} characters omitted] ...\n{value[-keep:]}"
        return value
    if depth >= 4:
        return value
    if isinstance(value, list):
        items = value
        if len(items) > MAX_LIST_ITEMS:
            truncated.append(path)
            items = items[:MAX_LIST_ITEMS // 2] + items[-MAX_LIST_ITEMS // 2:]
        return [_shorten(v, limit, f"{path}[]", truncated, depth + 1) for v in items]
    if isinstance(value, dict):
        return {k: _shorten(v, limit, f"{path}.{k}" if path else str(k), truncated, depth + 1)
                for k, v in value.items()}
    return value


def format_tool_result(
    success: bool,
    data: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
    output_files: Optional[list] = None,
    warnings: Optional[List[str]] = None,
    command: Optional[str] = None,
    max_chars: Optional[int] = None,
) -> Dict[str, Any]:
    """Wrap a tool result in the common envelope.

    The data dict is merged in for both outcomes, so a failed command still returns its
    stdout, stderr and command line. When error_message is not given, data["error"] is used.
    """
    data = dict(data or {})
    data.pop("result", None)  # status is the single source of truth

    result: Dict[str, Any] = {"status": "success" if success else "error"}
    if not success:
        result["error"] = error_message or data.pop("error", None) or "Unknown error"
        data.pop("error", None)
    else:
        data.pop("error", None)

    combined_warnings = list(data.pop("warnings", None) or []) + list(warnings or [])
    files = list(output_files if output_files is not None else (data.pop("output_files", None) or []))
    data.pop("output_files", None)

    result.update(data)
    result["warnings"] = combined_warnings
    result["output_files"] = files
    cmd = command or data.get("command")
    if cmd:
        result["command"] = cmd

    limit = max_chars or int(os.environ.get("GMX_MCP_MAX_CHARS", DEFAULT_MAX_CHARS))
    truncated: List[str] = []
    result = _shorten(result, limit, "", truncated)
    if truncated:
        result["truncated"] = sorted(set(truncated))
        result["warnings"] = result["warnings"] + [
            "Some fields were shortened; read the saved output files for the full content"
        ]
    return result


# ==============================================================================
# Logging
# ==============================================================================
class _InterceptHandler(logging.Handler):
    """Route stdlib logging (used by scripts and libraries) into loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        from loguru import logger
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


_logging_configured = False


def configure_logging(level: str = "INFO") -> None:
    """Send all logs to stderr and a rotating file under the log directory.

    Nothing is written to stdout, which carries the MCP protocol on stdio.
    """
    global _logging_configured
    if _logging_configured:
        return
    from loguru import logger

    logger.remove()
    logger.add(sys.stderr, level=level.upper(),
               format="{time:HH:mm:ss} | {level: <8} | {message}")
    try:
        log_dir = get_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(log_dir / "gromacs_mcp.log", level=level.upper(), rotation="5 MB", retention=5)
    except OSError as e:
        logger.warning(f"File logging disabled: {e}")

    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    _logging_configured = True
