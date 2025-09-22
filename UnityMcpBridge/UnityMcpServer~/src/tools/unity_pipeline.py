from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree

from mcp.server.fastmcp import FastMCP, Context
from pydantic import BaseModel, Field, ValidationError

from config import config
from telemetry_decorator import telemetry_tool
from unity_connection import send_command_with_retry

logger = logging.getLogger("mcp-for-unity-server")


class UnityProjectProbeArgs(BaseModel):
    projectPath: str = Field(..., description="Absolute path to the Unity project root")


class RoslynCheckArgs(BaseModel):
    projectPath: str = Field(..., description="Absolute path to the Unity project root")
    glob: Optional[str] = Field(default=None, description="Glob pattern for source discovery")
    langVersion: Optional[str] = Field(default="9.0", description="C# language version")


class UnityCompileArgs(BaseModel):
    projectPath: str = Field(..., description="Absolute path to the Unity project root")
    logPath: Optional[str] = Field(default=None, description="Optional override for log path")


class UnityRunTestsArgs(BaseModel):
    projectPath: str = Field(..., description="Absolute path to the Unity project root")
    platform: str = Field(..., description="EditMode or PlayMode")
    resultsPath: Optional[str] = Field(default=None, description="Optional override for NUnit XML path")
    nographics: Optional[bool] = Field(default=None, description="Reserved for batchmode compatibility")


class UnityBuildIL2CPPArgs(BaseModel):
    projectPath: str = Field(..., description="Absolute path to the Unity project root")
    target: str = Field(..., description="Unity build target")
    outputDir: Optional[str] = Field(default=None, description="Optional build output directory")


@dataclass
class CommandResult:
    ok: bool
    exit_code: int
    data: Optional[Dict[str, Any]] = None
    errors: Optional[List[Dict[str, Any]]] = None


_DEFAULT_SOURCE_GLOB = "Assets/AI.Generated/**/*.cs"
_CSC_ERROR_RE = re.compile(r"^(?P<file>[^:(]+)\((?P<line>\d+)(?:,(?P<column>\d+))?\): (?P<kind>error|warning) (?P<code>[A-Z]+\d+): (?P<message>.*)$")


def _is_offline_mode() -> bool:
    value = os.environ.get("UNITY_MCP_SKIP_STARTUP_CONNECT") or os.environ.get("UNITY_MCP_OFFLINE")
    if not value:
        return False
    return value.lower() in {"1", "true", "yes", "on"}


def register_unity_pipeline_tools(mcp: FastMCP) -> None:
    """Register build/test automation tools with the MCP server."""

    @mcp.tool()
    @telemetry_tool("unity_project_probe")
    def unity_project_probe(ctx: Context, projectPath: str) -> Dict[str, Any]:  # type: ignore[override]
        args = _validated(UnityProjectProbeArgs, projectPath=projectPath)
        result = _unity_project_probe_impl(Path(args.projectPath))
        return _as_dict(result)

    @mcp.tool()
    @telemetry_tool("roslyn_check")
    def roslyn_check(
        ctx: Context,
        projectPath: str,
        glob: Optional[str] = None,
        langVersion: Optional[str] = None,
    ) -> Dict[str, Any]:  # type: ignore[override]
        args = _validated(RoslynCheckArgs, projectPath=projectPath, glob=glob, langVersion=langVersion)
        result = _roslyn_check_impl(Path(args.projectPath), args.glob or _DEFAULT_SOURCE_GLOB, args.langVersion or "9.0")
        return _as_dict(result)

    @mcp.tool()
    @telemetry_tool("unity_compile")
    def unity_compile(ctx: Context, projectPath: str, logPath: Optional[str] = None) -> Dict[str, Any]:  # type: ignore[override]
        args = _validated(UnityCompileArgs, projectPath=projectPath, logPath=logPath)
        result = _unity_compile_impl(Path(args.projectPath), args.logPath)
        return _as_dict(result)

    @mcp.tool()
    @telemetry_tool("unity_run_tests")
    def unity_run_tests(
        ctx: Context,
        projectPath: str,
        platform: str,
        resultsPath: Optional[str] = None,
        nographics: Optional[bool] = None,
    ) -> Dict[str, Any]:  # type: ignore[override]
        args = _validated(
            UnityRunTestsArgs,
            projectPath=projectPath,
            platform=platform,
            resultsPath=resultsPath,
            nographics=nographics,
        )
        result = _unity_run_tests_impl(Path(args.projectPath), args.platform, args.resultsPath)
        return _as_dict(result)

    @mcp.tool()
    @telemetry_tool("unity_build_il2cpp")
    def unity_build_il2cpp(
        ctx: Context,
        projectPath: str,
        target: str,
        outputDir: Optional[str] = None,
    ) -> Dict[str, Any]:  # type: ignore[override]
        args = _validated(UnityBuildIL2CPPArgs, projectPath=projectPath, target=target, outputDir=outputDir)
        result = _unity_build_il2cpp_impl(Path(args.projectPath), args.target, args.outputDir)
        return _as_dict(result)


def _validated(model: type[BaseModel], **kwargs) -> BaseModel:
    try:
        return model(**kwargs)
    except ValidationError as exc:
        raise ValueError(exc.errors()) from exc


def _as_dict(result: CommandResult) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"ok": bool(result.ok), "exitCode": int(result.exit_code)}
    if result.data is not None:
        payload["data"] = result.data
    if result.errors:
        payload["errors"] = result.errors
    return payload


def _unity_project_probe_impl(project_path: Path) -> CommandResult:
    project_path = _ensure_project_path(project_path)
    if _is_offline_mode():
        data = _offline_project_probe(project_path)
        return CommandResult(ok=True, exit_code=0, data=data)
    try:
        response = send_command_with_retry("unity_project_probe", {"projectPath": str(project_path)})
    except Exception as exc:
        logger.error("unity_project_probe failed: %s", exc)
        return CommandResult(
            ok=False,
            exit_code=1,
            errors=[{"message": str(exc), "code": "unity_project_probe_failed"}],
        )
    return _normalize_bridge_response(response, default_error_code="unity_project_probe_failed")


def _roslyn_check_impl(project_path: Path, glob_pattern: str, lang_version: str) -> CommandResult:
    project_path = _ensure_project_path(project_path)
    files = sorted(str(p) for p in project_path.glob(glob_pattern) if p.is_file())
    if not files:
        return CommandResult(ok=True, exit_code=0, data={"checked": 0, "errors": []})

    probe = _unity_project_probe_impl(project_path)
    if not probe.ok:
        return CommandResult(ok=False, exit_code=probe.exit_code or 1, errors=probe.errors)

    data = probe.data or {}
    csc_path = data.get("roslynCompilerPath") or os.environ.get("UNITY_CSC_PATH")
    if not csc_path:
        return CommandResult(
            ok=False,
            exit_code=2,
            errors=[{"message": "Unable to locate Roslyn compiler (csc).", "code": "CSC_NOT_FOUND"}],
        )

    csc_executable = Path(csc_path)
    if not csc_executable.exists():
        return CommandResult(
            ok=False,
            exit_code=2,
            errors=[{"message": f"Roslyn compiler not found at {csc_executable}", "code": "CSC_NOT_FOUND"}],
        )

    references = _gather_reference_assemblies(project_path, data)
    with tempfile.TemporaryDirectory(prefix="unity-roslyn-") as tmp:
        output_path = Path(tmp) / "DryCompile.dll"
        cmd = [
            str(csc_executable),
            "/nologo",
            "/t:library",
            f"/langversion:{lang_version}",
            "/nowarn:CS1591",
            "/unsafe-",
            "/deterministic",
            f"/out:{output_path}",
        ]
        cmd.extend(f"/reference:{ref}" for ref in references)
        cmd.extend(files)

        logger.debug("Running Roslyn check: %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                cwd=str(project_path),
                capture_output=True,
                text=True,
                timeout=getattr(config, "roslyn_timeout_sec", 120),
                check=False,
            )
        except FileNotFoundError:
            return CommandResult(
                ok=False,
                exit_code=2,
                errors=[{"message": f"Roslyn compiler missing: {csc_executable}", "code": "CSC_NOT_FOUND"}],
            )
        except subprocess.TimeoutExpired:
            return CommandResult(
                ok=False,
                exit_code=3,
                errors=[{"message": "Roslyn compilation timed out", "code": "CSC_TIMEOUT"}],
            )

        diagnostics = _parse_csc_output(result.stdout, result.stderr, base_path=project_path)
        ok = result.returncode == 0
        exit_code = 0 if ok else result.returncode
        payload = {"checked": len(files), "errors": diagnostics}
        return CommandResult(ok=ok, exit_code=exit_code, data=payload, errors=diagnostics if not ok else None)


def _unity_compile_impl(project_path: Path, log_path: Optional[str]) -> CommandResult:
    project_path = _ensure_project_path(project_path)
    if _is_offline_mode():
        return CommandResult(
            ok=False,
            exit_code=1,
            errors=[{"message": "Unity connection is not available in offline mode", "code": "unity_compile_unavailable"}],
        )
    _clear_console()
    try:
        response = send_command_with_retry(
            "unity_compile",
            {"projectPath": str(project_path), "logPath": log_path} if log_path else {"projectPath": str(project_path)},
        )
    except Exception as exc:
        logger.error("unity_compile failed: %s", exc)
        return CommandResult(
            ok=False,
            exit_code=1,
            errors=[{"message": str(exc), "code": "unity_compile_failed"}],
        )
    bridge = _normalize_bridge_response(response, default_error_code="unity_compile_failed")
    if not bridge.ok:
        return bridge

    logs = _collect_console_entries(types=["error", "warning"])
    errors = [entry for entry in logs if entry.get("type") == "error"]
    warnings = [entry for entry in logs if entry.get("type") == "warning"]
    parsed_errors = [_parse_console_diagnostic(entry, project_path) for entry in errors]
    parsed_warnings = [_parse_console_diagnostic(entry, project_path) for entry in warnings]
    parsed_errors = [e for e in parsed_errors if e]
    parsed_warnings = [w for w in parsed_warnings if w]

    ok = not parsed_errors
    payload = {
        "errors": parsed_errors,
        "warnings": parsed_warnings,
        "rawLogPath": bridge.data.get("logPath") if bridge.data else None,
    }
    return CommandResult(ok=ok, exit_code=0 if ok else 1, data=payload, errors=parsed_errors if not ok else None)


def _unity_run_tests_impl(project_path: Path, platform: str, results_path: Optional[str]) -> CommandResult:
    project_path = _ensure_project_path(project_path)
    if _is_offline_mode():
        return CommandResult(
            ok=False,
            exit_code=1,
            errors=[{"message": "Unity connection is not available in offline mode", "code": "unity_tests_unavailable"}],
        )
    _clear_console()
    try:
        response = send_command_with_retry(
            "unity_run_tests",
            {
                "projectPath": str(project_path),
                "platform": platform,
                "resultsPath": results_path,
            },
        )
    except Exception as exc:
        logger.error("unity_run_tests failed: %s", exc)
        return CommandResult(
            ok=False,
            exit_code=1,
            errors=[{"message": str(exc), "code": "unity_run_tests_failed"}],
        )
    bridge = _normalize_bridge_response(response, default_error_code="unity_run_tests_failed")
    if not bridge.ok:
        return bridge

    data = bridge.data or {}
    results_file = data.get("resultsPath")
    if not results_file:
        return CommandResult(
            ok=False,
            exit_code=1,
            errors=[{"message": "Unity did not return a resultsPath", "code": "TEST_RESULTS_MISSING"}],
        )

    summary = _parse_nunit_results(Path(results_file))
    if summary is None:
        return CommandResult(
            ok=False,
            exit_code=1,
            errors=[{"message": f"Unable to parse NUnit results at {results_file}", "code": "TEST_RESULTS_PARSE"}],
        )

    ok = summary["failed"] == 0
    payload = {
        "passed": summary["passed"],
        "failed": summary["failed"],
        "durationSec": summary["duration"],
        "failures": summary["failures"],
        "resultsPath": results_file,
        "logPath": data.get("logPath"),
    }
    errors = [
        {
            "message": failure["message"],
            "file": failure.get("file"),
            "line": failure.get("line"),
        }
        for failure in summary["failures"]
    ] if not ok else None
    return CommandResult(ok=ok, exit_code=0 if ok else 1, data=payload, errors=errors)


def _unity_build_il2cpp_impl(project_path: Path, target: str, output_dir: Optional[str]) -> CommandResult:
    project_path = _ensure_project_path(project_path)
    if _is_offline_mode():
        return CommandResult(
            ok=False,
            exit_code=1,
            errors=[{"message": "Unity connection is not available in offline mode", "code": "unity_build_il2cpp_unavailable"}],
        )
    try:
        response = send_command_with_retry(
            "unity_build_il2cpp",
            {"projectPath": str(project_path), "target": target, "outputDir": output_dir},
        )
    except Exception as exc:
        logger.error("unity_build_il2cpp failed: %s", exc)
        return CommandResult(
            ok=False,
            exit_code=1,
            errors=[{"message": str(exc), "code": "unity_build_il2cpp_failed"}],
        )
    bridge = _normalize_bridge_response(response, default_error_code="unity_build_il2cpp_failed")
    if bridge.ok:
        return bridge
    return CommandResult(
        ok=False,
        exit_code=bridge.exit_code or 1,
        errors=bridge.errors or [{"message": "IL2CPP build failed"}],
    )


def _ensure_project_path(project_path: Path) -> Path:
    if not project_path.exists():
        raise FileNotFoundError(f"Project path does not exist: {project_path}")
    return project_path.resolve()


def _normalize_bridge_response(response: Dict[str, Any], default_error_code: str) -> CommandResult:
    if isinstance(response, dict) and response.get("success"):
        data = response.get("data") or {}
        return CommandResult(ok=True, exit_code=0, data=data)

    error_message = "Unknown Unity error"
    code = default_error_code
    if isinstance(response, dict):
        error_message = response.get("message") or response.get("error") or error_message
        code = response.get("code") or code
    return CommandResult(ok=False, exit_code=1, errors=[{"message": error_message, "code": code}])


def _gather_reference_assemblies(project_path: Path, probe_data: Dict[str, Any]) -> List[str]:
    paths = []
    search_paths = probe_data.get("managedAssemblySearchPaths") or []
    for entry in search_paths:
        if entry and Path(entry).exists():
            paths.append(Path(entry))

    library_script_assemblies = project_path / "Library" / "ScriptAssemblies"
    if library_script_assemblies.exists():
        paths.append(library_script_assemblies)

    references: List[str] = []
    preferred = [
        "netstandard.dll",
        "mscorlib.dll",
        "System.dll",
        "System.Core.dll",
        "UnityEngine.dll",
        "UnityEditor.dll",
        "UnityEngine.CoreModule.dll",
        "UnityEditor.CoreModule.dll",
        "UnityEngine.UI.dll",
    ]

    for directory in paths:
        for name in preferred:
            candidate = directory / name
            if candidate.exists():
                references.append(str(candidate))

        # Also include all assemblies in ScriptAssemblies for dependencies (e.g., generated asmdefs)
        if directory == library_script_assemblies:
            for assembly in directory.glob("*.dll"):
                references.append(str(assembly))

    return sorted(set(references))


def _parse_csc_output(stdout: str, stderr: str, base_path: Path) -> List[Dict[str, Any]]:
    diagnostics: List[Dict[str, Any]] = []
    for line in (stdout.splitlines() + stderr.splitlines()):
        match = _CSC_ERROR_RE.match(line.strip())
        if not match:
            continue
        if match.group("kind") != "error":
            continue
        file_path = match.group("file")
        abs_path = Path(file_path)
        if not abs_path.is_absolute():
            abs_path = (base_path / file_path).resolve()
        try:
            rel_path = abs_path.relative_to(base_path)
        except ValueError:
            rel_path = abs_path
        diagnostics.append(
            {
                "code": match.group("code"),
                "file": str(rel_path).replace(os.sep, "/"),
                "line": int(match.group("line")),
                "message": match.group("message").strip(),
            }
        )
    return diagnostics


def _clear_console() -> None:
    try:
        send_command_with_retry("read_console", {"action": "clear"})
    except Exception:
        logger.debug("Failed to clear Unity console before operation", exc_info=True)


def _collect_console_entries(types: List[str]) -> List[Dict[str, Any]]:
    try:
        response = send_command_with_retry(
            "read_console",
            {
                "action": "get",
                "types": types,
                "format": "json",
                "includeStacktrace": True,
            },
        )
    except Exception as exc:
        logger.error("Failed to collect console entries: %s", exc)
        return []

    if not isinstance(response, dict) or not response.get("success"):
        return []
    return response.get("data", {}).get("lines", []) or []


def _parse_console_diagnostic(entry: Dict[str, Any], project_path: Path) -> Optional[Dict[str, Any]]:
    message = entry.get("message") or entry.get("content")
    if not message:
        return None

    match = _CSC_ERROR_RE.match(message.splitlines()[0].strip())
    if match:
        file_path = match.group("file")
        line = int(match.group("line"))
        return {
            "code": match.group("code"),
            "file": _relative_path(project_path, file_path),
            "line": line,
            "message": match.group("message").strip(),
        }

    return {
        "message": message.strip(),
        "type": entry.get("type"),
    }


def _relative_path(base: Path, path_str: str) -> str:
    path = Path(path_str)
    if not path.is_absolute():
        path = (base / path).resolve()
    try:
        rel = path.relative_to(base)
        return str(rel).replace(os.sep, "/")
    except ValueError:
        return str(path)


def _parse_nunit_results(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        tree = ElementTree.parse(path)
    except ElementTree.ParseError:
        return None

    root = tree.getroot()
    if root.tag != "test-run":
        return None

    def _int_attr(node: ElementTree.Element, name: str) -> int:
        value = node.get(name)
        try:
            return int(float(value)) if value is not None else 0
        except ValueError:
            return 0

    passed = _int_attr(root, "passed")
    failed = _int_attr(root, "failed")
    duration_str = root.get("duration") or "0"
    try:
        duration = float(duration_str)
    except ValueError:
        duration = 0.0

    failures: List[Dict[str, Any]] = []
    for case in root.iter("test-case"):
        result = case.get("result") or ""
        if result.lower() != "failed":
            continue
        failure_node = case.find("failure")
        message = ""
        stack = ""
        if failure_node is not None:
            message_node = failure_node.find("message")
            stack_node = failure_node.find("stack-trace")
            message = message_node.text if message_node is not None and message_node.text else ""
            stack = stack_node.text if stack_node is not None and stack_node.text else ""
        failures.append(
            {
                "testName": case.get("fullname") or case.get("name"),
                "message": message.strip(),
                "stacktrace": stack.strip(),
            }
        )

    return {
        "passed": passed,
        "failed": failed,
        "duration": duration,
        "failures": failures,
    }


def _offline_project_probe(project_path: Path) -> Dict[str, Any]:
    version = "unknown"
    version_file = project_path / "ProjectSettings" / "ProjectVersion.txt"
    if version_file.exists():
        for line in version_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "m_EditorVersion" in line:
                version = line.split(":", 1)[-1].strip()
                break

    api_level = "Unknown"
    scripting_backends: Dict[str, str] = {}
    settings_file = project_path / "ProjectSettings" / "ProjectSettings.asset"
    api_map = {
        "1": "NET_2_0_Subset",
        "2": "NET_2_0",
        "3": "NET_4_6",
        "6": "NET_Standard_2_0",
        "8": "NET_Standard_2_1",
    }
    backend_map = {
        "0": "Mono",
        "1": "IL2CPP",
        "2": "WinRTDotNET",
    }

    if settings_file.exists():
        text = settings_file.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("apiCompatibilityLevel:") and "Standalone" in line:
                value = line.split("Standalone", 1)[-1].strip().strip('{}[]:, ')
                api_level = api_map.get(value, value or api_level)
            if line.startswith("scriptingBackend:") and "Standalone" in line:
                value = line.split("Standalone", 1)[-1].strip().strip('{}[]:, ')
                scripting_backends["Standalone"] = backend_map.get(value, value)

    packages = []
    packages_lock = project_path / "Packages" / "packages-lock.json"
    if packages_lock.exists():
        try:
            data = json.loads(packages_lock.read_text(encoding="utf-8"))
            for name, meta in (data.get("dependencies") or {}).items():
                packages.append({"name": name, "version": meta.get("version", "")})
        except Exception:
            pass

    roslyn_path = os.environ.get("UNITY_CSC_PATH", "")

    return {
        "unityVersion": version,
        "apiCompatibilityLevel": api_level,
        "scriptingBackendPerPlatform": scripting_backends,
        "packages": packages,
        "editorApplicationPath": "",
        "roslynCompilerPath": roslyn_path,
        "managedAssemblySearchPaths": [],
    }
