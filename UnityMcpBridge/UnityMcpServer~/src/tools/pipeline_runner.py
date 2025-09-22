from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, Dict, List

from .unity_pipeline import (
    _unity_project_probe_impl,
    _roslyn_check_impl,
    _unity_compile_impl,
    _unity_run_tests_impl,
    _unity_build_il2cpp_impl,
    _DEFAULT_SOURCE_GLOB,
    CommandResult,
)


def _write_result(path: Path, result) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        payload = {
            "ok": bool(result.ok),
            "exitCode": int(result.exit_code),
        }
        if result.data is not None:
            payload["data"] = result.data
        if result.errors:
            payload["errors"] = result.errors
        json.dump(payload, handle, indent=2)
        handle.write("\n")


def run_pipeline(project_path: Path, log_dir: Path, include_playmode: bool = True, run_il2cpp: bool = False) -> None:
    project_path = project_path.resolve()
    log_dir = log_dir.resolve()

    steps: Dict[str, Callable[[], CommandResult]] = {
        "unity_project_probe": lambda: _unity_project_probe_impl(project_path),
        "roslyn_check": lambda: _roslyn_check_impl(project_path, _DEFAULT_SOURCE_GLOB, "9.0"),
        "unity_compile": lambda: _unity_compile_impl(project_path, None),
        "unity_run_tests_editmode": lambda: _unity_run_tests_impl(project_path, "EditMode", None),
    }

    if include_playmode:
        steps["unity_run_tests_playmode"] = lambda: _unity_run_tests_impl(project_path, "PlayMode", None)

    if run_il2cpp:
        steps["unity_build_il2cpp"] = lambda: _unity_build_il2cpp_impl(project_path, "Standalone", None)

    failed_steps: List[str] = []
    worst_exit = 0

    for name, action in steps.items():
        result = action()
        _write_result(log_dir / f"{name}.json", result)
        if not result.ok:
            exit_code = int(result.exit_code) if result.exit_code is not None else 1
            worst_exit = max(worst_exit, exit_code)
            failed_steps.append(f"{name} (exit {exit_code})")

    if failed_steps:
        message = ", ".join(failed_steps)
        print(f"[pipeline_runner] Failure in steps: {message}", file=sys.stderr)
        raise SystemExit(worst_exit or 1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Execute the Unity MCP pipeline locally.")
    parser.add_argument("--project", dest="project", required=True, help="Path to the Unity project root")
    parser.add_argument("--log-dir", dest="log_dir", default="logs", help="Directory for JSON outputs")
    parser.add_argument("--no-playmode", action="store_true", help="Skip play mode tests")
    parser.add_argument("--il2cpp", action="store_true", help="Attempt IL2CPP build step")
    args = parser.parse_args()

    run_pipeline(Path(args.project), Path(args.log_dir), include_playmode=not args.no_playmode, run_il2cpp=args.il2cpp)


if __name__ == "__main__":
    main()
