"""Read-only diagnostics for the OMM installation and Ollama integration."""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import site
import subprocess
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from omm import config, linker, package_metadata
from omm.engines import RuntimeAdapterError
from omm.engines.base import LoopbackJsonClient
from omm.engines.ollama import DEFAULT_OLLAMA_URL, OllamaAdapter

DoctorStatus = Literal["PASS", "WARN", "FAIL"]
_STATUS_RANK: dict[DoctorStatus, int] = {"PASS": 0, "WARN": 1, "FAIL": 2}


@dataclass(frozen=True)
class DoctorRemediation:
    message: str
    command: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise ValueError("doctor remediation message must not be empty")
        if any(not isinstance(argument, str) or not argument for argument in self.command):
            raise ValueError("doctor remediation command arguments must be non-empty strings")

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"message": self.message}
        if self.command:
            result["command"] = list(self.command)
        return result

    def display_command(self) -> str | None:
        if not self.command:
            return None
        if platform.system() == "Windows":
            return subprocess.list2cmdline(self.command)
        return shlex.join(self.command)


@dataclass(frozen=True)
class DoctorCheck:
    status: DoctorStatus
    name: str
    detail: str
    remediation: DoctorRemediation | None = None

    def __post_init__(self) -> None:
        if self.status not in _STATUS_RANK:
            raise ValueError(f"unknown doctor status: {self.status}")
        if self.status == "PASS" and self.remediation is not None:
            raise ValueError("PASS doctor checks cannot carry remediation")

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "status": self.status,
            "name": self.name,
            "detail": self.detail,
        }
        if self.remediation is not None:
            result["remediation"] = self.remediation.as_dict()
        return result


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def status(self) -> DoctorStatus:
        if not self.checks:
            return "PASS"
        return max(self.checks, key=lambda check: _STATUS_RANK[check.status]).status

    @property
    def remediations(self) -> tuple[tuple[str, DoctorRemediation], ...]:
        unique: list[tuple[str, DoctorRemediation]] = []
        seen: set[DoctorRemediation] = set()
        for check in self.checks:
            remediation = check.remediation
            if check.status == "PASS" or remediation is None or remediation in seen:
                continue
            seen.add(remediation)
            unique.append((check.name, remediation))
        return tuple(unique)

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "checks": [check.as_dict() for check in self.checks],
        }


def read_theme_read_only() -> str:
    """Read the saved theme without creating, migrating, or backing up config."""
    try:
        raw = config.CONFIG_PATH.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, ValueError):
        return "dark"
    theme = parsed.get("theme") if isinstance(parsed, dict) else None
    return theme if isinstance(theme, str) else "dark"


def _update_remediation(
    source: package_metadata.InstallSource,
) -> DoctorRemediation:
    choices: dict[package_metadata.InstallSource, DoctorRemediation] = {
        package_metadata.InstallSource.GIT: DoctorRemediation(
            "Update the editable OMM checkout, then run omm doctor again.",
            ("omm", "update"),
        ),
        package_metadata.InstallSource.PIPX: DoctorRemediation(
            "Update the pipx-managed OMM installation, then run omm doctor again.",
            ("pipx", "upgrade", "omm-model"),
        ),
        package_metadata.InstallSource.PYPI: DoctorRemediation(
            "Update the Python package, then run omm doctor again.",
            ("python", "-m", "pip", "install", "--upgrade", "omm-model"),
        ),
        package_metadata.InstallSource.HOMEBREW: DoctorRemediation(
            "Update the Homebrew package, then run omm doctor again.",
            ("brew", "upgrade", "omm-hippo/omm/omm"),
        ),
        package_metadata.InstallSource.WINGET: DoctorRemediation(
            "Update the WinGet package, then run omm doctor again.",
            ("winget", "upgrade", "--id", "OmmHippo.OMM", "-e"),
        ),
        package_metadata.InstallSource.NPM: DoctorRemediation(
            "Update the global npm package, then run omm doctor again.",
            ("npm", "update", "--global", "@omm-hippo/omm"),
        ),
    }
    return choices.get(
        source,
        DoctorRemediation(
            "Confirm that this install source is intentional. Otherwise reinstall OMM "
            "with the package manager that originally installed it, then run omm doctor again."
        ),
    )


def _read_registry_read_only(path: Path) -> tuple[dict | None, str | None]:
    """Read models.json literally; diagnostics must never repair user state."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except OSError as error:
        return None, f"could not read {path}: {error}"
    try:
        parsed = json.loads(raw)
    except ValueError as error:
        return None, f"invalid JSON in {path}: {error}"
    if not isinstance(parsed, dict):
        return None, f"invalid registry shape in {path}: expected a JSON object"
    return parsed, None


def _pipx_candidate_paths() -> tuple[Path, ...]:
    executable = "pipx.exe" if platform.system() == "Windows" else "pipx"
    candidates: list[Path] = []
    # CPython's nt_user scheme puts user scripts in
    # {USER_BASE}\Python<XY>\Scripts, not {USER_BASE}\Scripts, so the
    # hand-built Windows path below never exists. Ask sysconfig first and
    # keep the old candidates as a fallback for odd environments.
    try:
        user_scripts = sysconfig.get_path("scripts", f"{os.name}_user")
    except (AttributeError, KeyError, TypeError, ValueError):
        user_scripts = None
    if user_scripts:
        candidates.append(Path(user_scripts) / executable)
    try:
        candidates.append(
            Path(site.USER_BASE)
            / ("Scripts" if os.name == "nt" else "bin")
            / executable
        )
    except (AttributeError, TypeError, ValueError):
        pass
    candidates.append(Path.home() / ".local" / "bin" / executable)
    if platform.system() == "Darwin":
        user_python = Path.home() / "Library" / "Python"
        try:
            candidates.extend(
                sorted(user_python.glob(f"*/bin/{executable}"), reverse=True)
            )
        except OSError:
            pass
        candidates.extend(
            [Path("/opt/homebrew/bin") / executable, Path("/usr/local/bin") / executable]
        )

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate.expanduser().absolute()))
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return tuple(unique)


def _find_pipx() -> Path | None:
    on_path = shutil.which("pipx")
    if on_path:
        return Path(on_path)
    for candidate in _pipx_candidate_paths():
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        except OSError:
            continue
    return None


def _pipx_version(executable: Path) -> str | None:
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    version = result.stdout.strip()
    return version if version and len(version) <= 64 else None


def _editable_source_path(record: dict | None) -> tuple[Path | None, str | None]:
    if not isinstance(record, dict):
        return None, None
    directory_info = record.get("dir_info")
    if not isinstance(directory_info, dict) or directory_info.get("editable") is not True:
        return None, None
    url = record.get("url")
    if not isinstance(url, str):
        return None, "editable install metadata has no source URL"
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None, "editable install metadata has an invalid source URL"
    if parsed.scheme != "file" or parsed.query or parsed.fragment:
        return None, "editable install source is not a plain local file URL"
    if parsed.netloc not in ("", "localhost"):
        path_text = f"//{parsed.netloc}{parsed.path}"
    else:
        path_text = parsed.path
    try:
        decoded = url2pathname(unquote(path_text))
    except (TypeError, ValueError):
        return None, "editable install source path could not be decoded"
    return Path(decoded), None


def _git_head(source: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    commit = result.stdout.strip()
    return commit if re.fullmatch(r"[0-9a-fA-F]{40,64}", commit) else None


def _source_version(source: Path) -> str | None:
    try:
        pyproject = (source / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    return match.group(1) if match else None


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(parent.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def _installation_checks(module_path: Path, command_path: Path) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    source = package_metadata.install_source()
    try:
        installed_version = package_metadata.version()
    except Exception:
        installed_version = "unknown"
    installation_status: DoctorStatus = (
        "FAIL"
        if installed_version == "unknown"
        else "WARN"
        if source is package_metadata.InstallSource.UNKNOWN
        else "PASS"
    )
    checks.append(
        DoctorCheck(
            installation_status,
            "installation",
            f"omm {installed_version}; source={source.value}; "
            f"command={command_path}; module={module_path}",
            (
                DoctorRemediation(
                    "OMM's installed version could not be read. Reinstall it with the "
                    "same package manager, then run omm doctor again."
                )
                if installed_version == "unknown"
                else _update_remediation(source)
                if source is package_metadata.InstallSource.UNKNOWN
                else None
            ),
        )
    )
    # sys.argv[0] for a pipx launcher on Windows is the extension-less
    # `...\.local\bin\omm`, which does not exist as a file - the real one is
    # `omm.exe`. Try the bare path, then `.exe`, then PATH lookup, before
    # calling the command unresolvable (false WARN seen 2026-08-23).
    resolved_command = None
    candidates = [command_path]
    if platform.system() == "Windows" and command_path.suffix.lower() != ".exe":
        candidates.append(command_path.with_suffix(".exe"))
    found = shutil.which(str(command_path)) or shutil.which(command_path.name)
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        try:
            resolved_command = candidate.resolve(strict=True)
            break
        except OSError:
            continue
    command_usable = bool(
        resolved_command
        and resolved_command.is_file()
        and (platform.system() == "Windows" or os.access(resolved_command, os.X_OK))
    )
    checks.append(
        DoctorCheck(
            "PASS" if command_usable else "WARN",
            "command",
            f"{command_path} -> {resolved_command}"
            if resolved_command
            else f"could not resolve the invoked command path: {command_path}",
            None
            if command_usable
            else DoctorRemediation(
                "Open a new terminal so PATH is refreshed. If the omm command is "
                "still missing, reinstall OMM with the same package manager and retry."
            ),
        )
    )

    install_record = package_metadata.direct_url()
    editable_source, editable_error = _editable_source_path(install_record)
    if editable_error:
        checks.append(
            DoctorCheck(
                "FAIL",
                "editable source",
                editable_error,
                DoctorRemediation(
                    "Repair or reinstall the editable OMM checkout, then run omm doctor again."
                ),
            )
        )
    elif editable_source is None:
        checks.append(
            DoctorCheck(
                "PASS",
                "editable source",
                f"not editable; installation is managed as {source.value}",
            )
        )
    elif not editable_source.is_dir():
        checks.append(
            DoctorCheck(
                "FAIL",
                "editable source",
                f"configured source does not exist: {editable_source}",
                DoctorRemediation(
                    "Restore the configured source directory or reinstall OMM with the "
                    "same package manager, then run omm doctor again."
                ),
            )
        )
    elif not _path_is_within(module_path, editable_source):
        checks.append(
            DoctorCheck(
                "FAIL",
                "editable source",
                f"{editable_source} does not contain the running OMM module {module_path}",
                DoctorRemediation(
                    "The launcher and editable checkout point to different copies. "
                    "Reinstall OMM from the intended checkout, then run omm doctor again."
                ),
            )
        )
    else:
        checks.append(DoctorCheck("PASS", "editable source", str(editable_source)))
        commit = _git_head(editable_source)
        checks.append(
            DoctorCheck(
                "PASS" if commit else "WARN",
                "source commit",
                commit[:12] if commit else "source is not a readable Git checkout",
                None
                if commit
                else DoctorRemediation(
                    "Restore the editable source as a readable Git checkout or reinstall "
                    "OMM, then run omm doctor again."
                ),
            )
        )
        source_version = _source_version(editable_source)
        if source_version is None:
            checks.append(
                DoctorCheck(
                    "WARN",
                    "version agreement",
                    "source version could not be read",
                    _update_remediation(source),
                )
            )
        elif source_version != installed_version:
            checks.append(
                DoctorCheck(
                    "WARN",
                    "version agreement",
                    f"package metadata={installed_version}; editable source={source_version}",
                    _update_remediation(source),
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    "PASS", "version agreement", f"both report {installed_version}"
                )
            )

    pipx = _find_pipx()
    if pipx is None:
        if source is package_metadata.InstallSource.PIPX:
            checks.append(
                DoctorCheck(
                    "WARN",
                    "pipx",
                    "installation is pipx-managed but the pipx command was not found",
                    DoctorRemediation(
                        "Repair or reinstall pipx and open a new terminal, then run "
                        "omm doctor again."
                    ),
                )
            )
        else:
            checks.append(
                DoctorCheck("PASS", "pipx", f"not required for source={source.value}")
            )
    else:
        version = _pipx_version(pipx)
        checks.append(
            DoctorCheck(
                "PASS" if version else "WARN",
                "pipx",
                f"pipx {version} at {pipx}"
                if version
                else f"found at {pipx}, but --version failed",
                None
                if version
                else DoctorRemediation(
                    "Repair or reinstall pipx, then run omm doctor again."
                ),
            )
        )
    return checks


def _registered_ollama_tags(registry_data: dict) -> list[tuple[str, str, str]]:
    survivors: list[tuple[str, dict, str, object, object]] = []
    for filename, raw_entry in registry_data.items():
        if not isinstance(filename, str) or not isinstance(raw_entry, dict):
            continue
        linked = raw_entry.get("linked")
        stored = raw_entry.get("ollama_name")
        runtime = raw_entry.get("ollama_runtime_name")
        is_linked = isinstance(linked, dict) and linked.get("ollama") is True
        # An explicit linked.ollama=false means the user does not expect the
        # model to appear in Ollama. Entries from older registries may have
        # no linked map, so keep diagnosing those when they carry a tag.
        if isinstance(linked, dict) and not is_linked:
            continue
        if (
            not is_linked
            and not isinstance(runtime, str)
            and not isinstance(stored, str)
        ):
            continue
        stored_tag = stored.strip() if isinstance(stored, str) else ""
        survivors.append((filename, raw_entry, stored_tag, runtime, stored))

    # Resolve every legacy entry in one pass: the single-entry API walks
    # the whole Ollama manifest tree per entry that has no cached
    # `ollama_runtime_name` (see issue #181), and `omm doctor` iterates
    # the entire registry. cli.py already uses the batch form.
    to_resolve = [
        (filename, raw_entry)
        for filename, raw_entry, _stored_tag, runtime, stored in survivors
        if isinstance(runtime, str) or isinstance(stored, str)
    ]
    try:
        resolved = linker.resolve_ollama_runtime_names_batch(to_resolve)
    except (OSError, TypeError, ValueError):
        resolved = {}

    mappings: list[tuple[str, str, str]] = []
    for filename, _raw_entry, stored_tag, runtime, stored in survivors:
        if isinstance(runtime, str) or isinstance(stored, str):
            runtime_tag = resolved.get(filename)
            if runtime_tag is None:
                # Same fallback the per-entry call had on failure.
                runtime_tag = runtime.strip() if isinstance(runtime, str) else stored_tag
        else:
            runtime_tag = ""
        mappings.append((filename, stored_tag or runtime_tag, runtime_tag))
    return mappings


def _canonical_ollama_tag(tag: str) -> str:
    normalized = tag.strip().casefold()
    return normalized if ":" in normalized else f"{normalized}:latest"


def _ollama_api_tags() -> set[str]:
    """Read the exact available names from Ollama's GET /api/tags response."""
    response = LoopbackJsonClient(DEFAULT_OLLAMA_URL).request(
        "GET", "/api/tags", timeout=10, default_failure="server_unavailable"
    )
    rows = response.data.get("models")
    if not isinstance(rows, list):
        raise RuntimeAdapterError("unknown", "Ollama returned an invalid /api/tags list")
    return {
        value
        for row in rows
        if isinstance(row, dict)
        for value in (row.get("name"), row.get("model"))
        if isinstance(value, str) and value.strip()
    }


def _ollama_checks(registry_data: dict) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    installed = linker.is_ollama_installed()
    executable = linker.find_ollama_executable()
    install_ollama = DoctorRemediation(
        "If you want to use Ollama, install it and then run omm doctor again. "
        "If you use another runner, this optional warning can be ignored."
    )
    start_ollama = DoctorRemediation(
        "Start the Ollama app or service, wait for its local API to be ready, "
        "then run omm doctor again."
    )
    checks.append(
        DoctorCheck(
            "PASS" if installed else "WARN",
            "Ollama installation",
            f"detected at {executable}" if executable else (
                "application detected" if installed else "not detected"
            ),
            None if installed else install_ollama,
        )
    )

    adapter = OllamaAdapter()
    health = adapter.health()
    mappings = _registered_ollama_tags(registry_data)
    if not health.reachable:
        checks.append(
            DoctorCheck(
                "WARN",
                "Ollama server",
                f"not reachable ({health.failure_reason or 'unknown'})",
                start_ollama if installed else install_ollama,
            )
        )
        if mappings:
            checks.append(
                DoctorCheck(
                    "WARN",
                    "Ollama tags",
                    f"{len(mappings)} registered mapping(s) not checked because the server is unavailable",
                )
            )
        else:
            checks.append(
                DoctorCheck("PASS", "Ollama tags", "no registered Ollama mappings")
            )
        return checks

    checks.append(
        DoctorCheck(
            "PASS",
            "Ollama server",
            f"reachable; version={health.version or 'unknown'}",
        )
    )
    if not mappings:
        checks.append(DoctorCheck("PASS", "Ollama tags", "no registered Ollama mappings"))
        return checks

    try:
        api_tags = _ollama_api_tags()
    except RuntimeAdapterError as error:
        checks.append(
            DoctorCheck(
                "WARN",
                "Ollama tags",
                f"/api/tags could not be checked ({error.reason})",
                start_ollama,
            )
        )
        return checks

    actual_tags = {_canonical_ollama_tag(value) for value in api_tags}
    for filename, stored_tag, runtime_tag in mappings:
        if not runtime_tag:
            checks.append(
                DoctorCheck(
                    "WARN",
                    f"Ollama tag: {filename}",
                    "linked in the registry but no Ollama runtime tag is stored",
                    DoctorRemediation(
                        "Re-link this model into Ollama, then run omm doctor again.",
                        ("omm", "link", filename, "--engine", "ollama"),
                    ),
                )
            )
            continue
        visible = _canonical_ollama_tag(runtime_tag) in actual_tags
        detail = f"stored={stored_tag}; runtime={runtime_tag}"
        detail += "; present in /api/tags" if visible else "; not present in /api/tags"
        checks.append(
            DoctorCheck(
                "PASS" if visible else "WARN",
                f"Ollama tag: {filename}",
                detail,
                None
                if visible
                else DoctorRemediation(
                    "Repair this model's Ollama link, then run omm doctor again.",
                    ("omm", "link", filename, "--engine", "ollama"),
                ),
            )
        )
    return checks


def running_command_path() -> Path:
    arg0 = Path(sys.argv[0]).expanduser()
    if arg0.is_absolute():
        return arg0
    found = shutil.which(str(arg0))
    return Path(found) if found else arg0


def collect_report(
    *, module_path: Path | None = None, command_path: Path | None = None
) -> DoctorReport:
    """Collect diagnostics using reads, GET requests, and read-only subprocesses only."""
    running_module = module_path or Path(__file__).with_name("cli.py").resolve()
    running_command = command_path or running_command_path()
    checks = _installation_checks(running_module, running_command)
    registry_data, registry_error = _read_registry_read_only(config.REGISTRY_PATH)
    if registry_error:
        checks.append(
            DoctorCheck(
                "FAIL",
                "registry",
                registry_error,
                DoctorRemediation(
                    f"Back up {config.REGISTRY_PATH}, then inspect or restore valid JSON. "
                    "Do not delete it blindly; run omm doctor again after repair."
                ),
            )
        )
        registry_data = {}
    else:
        assert registry_data is not None
        checks.append(
            DoctorCheck(
                "PASS",
                "registry",
                f"{len(registry_data)} registered model(s) at {config.REGISTRY_PATH}",
            )
        )
    checks.extend(_ollama_checks(registry_data))
    from omm.install_state import pending_records

    for pending in pending_records():
        checks.append(DoctorCheck(
            "WARN", "Install recovery",
            f"{pending['filename']}: last checkpoint {pending.get('phase', 'unknown')}; "
            "re-run the original install command to recheck and resume.",
            DoctorRemediation(
                "Re-run the original omm install command. OMM will recheck the saved "
                "checkpoint before resuming; then run omm doctor again."
            ),
        ))
    from omm.runtime_profiles import interrupted_runs

    for pending in interrupted_runs():
        checks.append(DoctorCheck(
            "WARN", "Runtime profile cleanup",
            f"A previous OMM run ended before confirming cleanup of {pending['alias']}. "
            "Inspect this alias in Ollama; no model was automatically unloaded or deleted.",
            DoctorRemediation(
                f"Inspect {pending['alias']} in Ollama and stop it only if it is no "
                "longer in use, then run omm doctor again.",
                ("ollama", "ps"),
            ),
        ))
    return DoctorReport(tuple(checks))
