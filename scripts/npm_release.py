#!/usr/bin/env python3
"""Verify, publish, and exercise OMM npm release tarballs."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import npm_package

CHECKSUMS_NAME = "SHA256SUMS"
REGISTRY = "https://registry.npmjs.org/"
LIFECYCLE_SCRIPTS = {"preinstall", "install", "postinstall", "prepare"}
MAX_UNPACKED_PACKAGE_BYTES = 128 * 1024 * 1024
MAX_TAR_MEMBERS = 1_000


class NpmReleaseError(RuntimeError):
    """Raised when an npm release cannot be proven safe and complete."""


@dataclass(frozen=True)
class PackageInfo:
    path: Path
    name: str
    version: str
    target: str | None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _integrity(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return "sha512-" + base64.b64encode(digest.digest()).decode("ascii")


def _tar_files(bundle: tarfile.TarFile) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    unpacked_size = 0
    for member_index, member in enumerate(bundle.getmembers(), start=1):
        if member_index > MAX_TAR_MEMBERS:
            raise NpmReleaseError("npm tarball contains too many members")
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise NpmReleaseError(f"unsafe npm tar member: {member.name!r}")
        if path.parts[0] != "package":
            raise NpmReleaseError(f"npm tar member is outside package/: {member.name!r}")
        if member.isdir():
            continue
        if not member.isfile() or member.issym() or member.islnk():
            raise NpmReleaseError(f"npm tar member is not a regular file: {member.name!r}")
        extracted = bundle.extractfile(member)
        if extracted is None:
            raise NpmReleaseError(f"cannot read npm tar member: {member.name!r}")
        if member.name in files:
            raise NpmReleaseError(f"duplicate npm tar member: {member.name!r}")
        unpacked_size += member.size
        if unpacked_size > MAX_UNPACKED_PACKAGE_BYTES:
            raise NpmReleaseError("npm tarball exceeds the unpacked size limit")
        files[member.name] = extracted.read()
    return files


def _manifest(files: dict[str, bytes]) -> dict[str, Any]:
    try:
        value = json.loads(files["package/package.json"].decode("utf-8"))
    except (KeyError, UnicodeDecodeError, ValueError) as error:
        raise NpmReleaseError("npm tarball has no valid package.json") from error
    if not isinstance(value, dict):
        raise NpmReleaseError("npm package.json must contain an object")
    return value


def inspect_tarball(path: Path) -> PackageInfo:
    if not path.is_file() or path.is_symlink() or path.suffix != ".tgz":
        raise NpmReleaseError(f"invalid npm tarball: {path}")
    try:
        with tarfile.open(path, mode="r:gz") as bundle:
            files = _tar_files(bundle)
    except (OSError, tarfile.TarError) as error:
        raise NpmReleaseError(f"cannot read npm tarball {path.name}: {error}") from error

    manifest = _manifest(files)
    name = manifest.get("name")
    version = manifest.get("version")
    expected_version = npm_package.project_version()
    if not isinstance(name, str) or not isinstance(version, str):
        raise NpmReleaseError("npm package name and version must be strings")
    if version != expected_version:
        raise NpmReleaseError(
            f"npm package {name!r} has version {version!r}, expected {expected_version!r}"
        )
    if manifest.get("private") is not False:
        raise NpmReleaseError(f"npm package {name!r} is not explicitly publishable")
    if manifest.get("publishConfig") != {"access": "public", "provenance": True}:
        raise NpmReleaseError(f"npm package {name!r} does not require public provenance")
    scripts = manifest.get("scripts", {})
    if not isinstance(scripts, dict) or LIFECYCLE_SCRIPTS.intersection(scripts):
        raise NpmReleaseError(f"npm package {name!r} has an install lifecycle script")
    repository = manifest.get("repository")
    if not isinstance(repository, dict) or repository.get("url") != (
        "git+https://github.com/omm-hippo/omm.git"
    ):
        raise NpmReleaseError(f"npm package {name!r} has the wrong repository")

    if name == npm_package.LAUNCHER_NAME:
        expected_files = {f"package/{item}" for item in npm_package.EXPECTED_LAUNCHER_FILES}
        if set(files) != expected_files:
            raise NpmReleaseError("npm launcher tarball has files outside its allowlist")
        if files["package/LICENSE"] != npm_package.canonical_license_bytes():
            raise NpmReleaseError("npm launcher license does not match the repository")
        expected_launcher = npm_package.canonical_text_bytes(
            npm_package.LAUNCHER_SOURCE / "bin" / "omm.js"
        )
        if files["package/bin/omm.js"] != expected_launcher:
            raise NpmReleaseError("npm launcher entry point does not match the source")
        if files["package/lib/launcher.js"] != npm_package.canonical_text_bytes(
            npm_package.LAUNCHER_SOURCE / "lib" / "launcher.js"
        ):
            raise NpmReleaseError("npm launcher implementation does not match the source")
        if manifest.get("bin") != {"omm": "bin/omm.js"}:
            raise NpmReleaseError("npm launcher exposes an unexpected command")
        if manifest.get("engines") != {"node": ">=22.14.0"}:
            raise NpmReleaseError("npm launcher has the wrong Node version floor")
        try:
            packed_targets = json.loads(files["package/targets.json"].decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise NpmReleaseError("npm launcher targets.json is invalid") from error
        if packed_targets != npm_package.targets():
            raise NpmReleaseError("npm launcher target map does not match the source contract")
        expected_optional = {
            value["package"]: expected_version
            for value in npm_package.targets().values()
        }
        if manifest.get("optionalDependencies") != expected_optional:
            raise NpmReleaseError("npm launcher has the wrong optional dependencies")
        return PackageInfo(path=path, name=name, version=version, target=None)

    target_by_package = {
        value["package"]: (target_name, value)
        for target_name, value in npm_package.targets().items()
    }
    if name not in target_by_package:
        raise NpmReleaseError(f"unexpected npm package: {name!r}")
    target_name, target = target_by_package[name]
    binary_name = f"package/{target['binary']}"
    expected_files = {"package/LICENSE", "package/package.json", binary_name}
    if set(files) != expected_files:
        raise NpmReleaseError(f"npm platform tarball {name!r} has unexpected files")
    if files["package/LICENSE"] != npm_package.canonical_license_bytes():
        raise NpmReleaseError(
            f"npm platform tarball {name!r} license does not match the repository"
        )
    binary = files[binary_name]
    try:
        npm_package.validate_binary_format(binary, target)
    except npm_package.NpmPackageError as error:
        raise NpmReleaseError(
            f"npm platform binary does not match {target_name}: {error}"
        ) from error
    metadata = manifest.get("omm")
    if not isinstance(metadata, dict) or metadata != {
        "binary": target["binary"],
        "distribution": "omm-model",
        "sha256": hashlib.sha256(binary).hexdigest(),
        "target": target_name,
    }:
        raise NpmReleaseError(f"npm platform metadata does not match {target_name}")
    if manifest.get("os") != [target["os"]] or manifest.get("cpu") != [target["cpu"]]:
        raise NpmReleaseError(f"npm platform selectors do not match {target_name}")
    expected_libc = [target["libc"]] if "libc" in target else None
    if manifest.get("libc") != expected_libc and (
        expected_libc is not None or "libc" in manifest
    ):
        raise NpmReleaseError(f"npm libc selector does not match {target_name}")
    return PackageInfo(path=path, name=name, version=version, target=target_name)


def verify_bundle(pack_dir: Path, *, write_checksums: bool = False) -> list[PackageInfo]:
    if not pack_dir.is_dir():
        raise NpmReleaseError(f"npm package directory does not exist: {pack_dir}")
    checksum_path = pack_dir / CHECKSUMS_NAME
    if checksum_path.is_symlink():
        raise NpmReleaseError("npm bundle checksum file cannot be a symlink")
    allowed_files = {checksum_path} if checksum_path.exists() else set()
    tarballs = sorted(pack_dir.glob("*.tgz"))
    actual_entries = set(pack_dir.iterdir())
    if actual_entries != set(tarballs) | allowed_files:
        unexpected = sorted(
            path.name for path in actual_entries - set(tarballs) - allowed_files
        )
        raise NpmReleaseError(f"npm bundle contains unexpected files: {unexpected}")
    packages = [inspect_tarball(path) for path in tarballs]
    expected_names = {npm_package.LAUNCHER_NAME} | {
        value["package"] for value in npm_package.targets().values()
    }
    names = [package.name for package in packages]
    if len(names) != len(set(names)) or set(names) != expected_names:
        raise NpmReleaseError(
            f"npm bundle contains packages {sorted(names)}, expected {sorted(expected_names)}"
        )
    expected_lines = [f"{_sha256(path)}  {path.name}" for path in tarballs]
    expected_text = "\n".join(expected_lines) + "\n"
    if write_checksums:
        checksum_path.write_text(expected_text, encoding="ascii", newline="\n")
    elif not checksum_path.is_file() or checksum_path.read_text(encoding="ascii") != expected_text:
        raise NpmReleaseError("npm bundle checksums are missing or do not match")
    return packages


CMD_METACHARACTERS = frozenset('&|<>^%"\r\n')


def _native_command(executable: str | Path, *arguments: str) -> list[str] | str:
    """Build the argument for ``subprocess.run`` that executes ``executable``.

    ``.cmd``/``.bat`` scripts (npm on Windows) must go through ``cmd.exe``. That
    invocation is returned as a single raw command-line string, not a list: for
    a list, ``subprocess`` would re-quote the already-quoted payload with
    backslash-escaped quotes that ``cmd.exe`` cannot parse, which breaks any
    interpreter installed under a path with spaces (``C:\\Program Files\\nodejs``,
    the default Node install). With ``/s``, ``cmd.exe`` strips the outer quotes
    around the payload and runs the quoted line inside verbatim.

    Two invariants keep that payload safe, because ``cmd.exe`` parses it before
    the script ever sees it and ``subprocess.list2cmdline`` does not help: it
    quotes only tokens containing whitespace, and its quoting targets the MSVC
    ``argv`` parser, not a shell.

    1. The executable is always quoted. Unquoted, a path holding ``(`` or ``&``
       is split by ``cmd.exe`` and the command fails to start.
    2. No argument may contain a ``cmd.exe`` metacharacter. Unquoted, ``%VAR%``
       is expanded, ``^`` is eaten, ``>`` redirects to a file, and ``&`` runs a
       second command. Such arguments cannot be passed through ``cmd.exe`` to a
       ``.cmd`` script safely, so they are rejected rather than mangled.
    """
    executable = str(executable)
    if os.name == "nt" and Path(executable).suffix.casefold() in {".cmd", ".bat"}:
        for argument in arguments:
            unsafe = sorted(CMD_METACHARACTERS.intersection(argument))
            if unsafe:
                raise NpmReleaseError(
                    f"cannot run {Path(executable).name} with the argument "
                    f"{argument!r}: cmd.exe interprets {unsafe} while parsing the "
                    f"command line, so the argument cannot reach the script intact"
                )
        tail = subprocess.list2cmdline(arguments)
        command = f'"{executable}"' + (f" {tail}" if tail else "")
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return f'"{comspec}" /d /s /c "{command}"'
    return [executable, *arguments]


def _validate_registry_url(registry: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(registry)
        port = parsed.port
    except ValueError as error:
        raise NpmReleaseError(f"invalid npm registry URL: {registry!r}") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
    ):
        raise NpmReleaseError("npm registry URL must be credential-free HTTPS")
    return registry


def _run(
    executable: str | Path,
    *arguments: str,
    check: bool = True,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _native_command(executable, *arguments),
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=cwd,
        timeout=300,
    )


def _npm() -> str:
    command = shutil.which("npm")
    if command is None:
        raise NpmReleaseError("npm is not installed")
    return command


def _command_path(prefix: Path) -> Path:
    return prefix / ("omm.cmd" if os.name == "nt" else "bin/omm")


def _command_shims(prefix: Path) -> list[Path]:
    """Every wrapper npm creates in a global prefix for the ``omm`` bin entry.

    On Windows npm writes three of them side by side -- ``omm`` (the sh shim),
    ``omm.cmd`` and ``omm.ps1`` -- so checking only the one this module runs
    would call a partial uninstall clean. Verified against npm 11.12.1
    installing ``@omm-hippo/omm`` into a scratch ``--prefix``.
    """
    if os.name == "nt":
        return [prefix / "omm", prefix / "omm.cmd", prefix / "omm.ps1"]
    return [prefix / "bin" / "omm"]


def _install_tree(prefix: Path) -> str:
    """Best-effort ``npm ls`` dump for a failed probe. Never raises.

    A probe failure (wrong version, missing binary) usually means npm resolved
    the platform optional dependency differently than expected. The CI log only
    shows the exception, so capture the installed tree to point at the cause on
    the next release without a manual repro (issue #237).
    """
    try:
        listed = _run(
            _npm(),
            "ls",
            "--global",
            "--prefix",
            str(prefix),
            "--all",
            "--depth",
            "2",
            check=False,
        )
    except (OSError, subprocess.SubprocessError, NpmReleaseError) as error:
        return f"npm ls unavailable: {error}"
    return f"{listed.stdout}\n{listed.stderr}".strip() or "npm ls produced no output"


def _probe_install(
    prefix: Path,
    omm_home: Path,
    version: str,
    target_package: str,
) -> None:
    command = _command_path(prefix)
    if not command.is_file():
        raise NpmReleaseError(
            f"npm did not expose the OMM command at {command}\n"
            f"npm ls:\n{_install_tree(prefix)}"
        )
    # These probes run the installed command, so a non-zero exit is a probe
    # result, not an internal error: report it as NpmReleaseError with the same
    # diagnostics as a wrong answer, instead of letting subprocess raise a
    # CalledProcessError that carries neither the output nor the install tree.
    version_result = _run(command, "--version", check=False)
    version_lines = f"{version_result.stdout}\n{version_result.stderr}".splitlines()
    if version_result.returncode != 0:
        raise NpmReleaseError(
            f"npm-installed OMM --version exited {version_result.returncode}\n"
            f"--version stdout: {version_result.stdout!r}\n"
            f"--version stderr: {version_result.stderr!r}\n"
            f"npm ls:\n{_install_tree(prefix)}"
        )
    if f"omm {version}" not in {line.strip() for line in version_lines}:
        raise NpmReleaseError(
            "npm-installed OMM reported the wrong version\n"
            f"--version stdout: {version_result.stdout!r}\n"
            f"--version stderr: {version_result.stderr!r}\n"
            f"npm ls:\n{_install_tree(prefix)}"
        )
    help_result = _run(command, "--help", check=False)
    if help_result.returncode != 0 or "Example usage:" not in (
        f"{help_result.stdout}\n{help_result.stderr}"
    ):
        raise NpmReleaseError(
            f"npm-installed OMM help probe failed (exit {help_result.returncode})\n"
            f"--help stdout: {help_result.stdout!r}\n"
            f"--help stderr: {help_result.stderr!r}\n"
            f"npm ls:\n{_install_tree(prefix)}"
        )
    environment = dict(os.environ)
    environment["OMM_HOME"] = str(omm_home)
    update = _run(command, "update", check=False, env=environment)
    update_output = f"{update.stdout}\n{update.stderr}"
    if update.returncode != 1 or "npm update --global @omm-hippo/omm" not in update_output:
        raise NpmReleaseError("npm-installed OMM did not report npm update guidance")
    if (omm_home / "src").exists():
        raise NpmReleaseError("npm-installed OMM unexpectedly created a Git checkout")
    _run(
        _npm(),
        "uninstall",
        "--global",
        "--prefix",
        str(prefix),
        npm_package.LAUNCHER_NAME,
        target_package,
    )
    # Path.exists() follows symlinks, so it misses a dangling launcher left
    # behind when npm removes the target but not the link itself.
    leftover = [shim for shim in _command_shims(prefix) if os.path.lexists(shim)]
    if leftover:
        raise NpmReleaseError(
            "npm uninstall left the OMM command exposed: "
            f"{[shim.name for shim in leftover]}"
        )


def smoke_tarballs(pack_dir: Path, target_name: str) -> None:
    packages = verify_bundle(pack_dir)
    launcher = next(package for package in packages if package.target is None)
    platform_package = next(package for package in packages if package.target == target_name)
    with tempfile.TemporaryDirectory(prefix="omm-npm-smoke-") as temporary:
        root = Path(temporary)
        prefix = root / "prefix"
        _run(
            _npm(),
            "install",
            "--global",
            "--prefix",
            str(prefix),
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            str(platform_package.path),
            str(launcher.path),
        )
        _probe_install(
            prefix,
            root / "omm-home",
            launcher.version,
            platform_package.name,
        )


# A brand-new version's platform `optionalDependencies` can take a while to
# propagate across the npm registry CDN after `publish`. During that window a
# plain `npm install --global @omm-hippo/omm@<new>` intermittently resolves the
# wrong platform package (or none), so the post-publish probe fails even though
# every artifact is correct -- exactly the transient seen on the v0.3.33 release
# (issue #237). Retry the install + probe from a clean slate before giving up;
# a real defect fails every attempt and still raises with the #242 tree dump.
REGISTRY_PROBE_ATTEMPTS = 5
REGISTRY_PROBE_BACKOFF_SECONDS = 20


def _install_launcher_from_registry(
    prefix: Path,
    omm_home: Path,
    version: str,
    target_package: str,
    registry: str,
) -> None:
    # A lagging CDN does not only produce a wrong install tree: `npm install`
    # itself returns 5xx/ETARGET/EBADPLATFORM and the probe can time out. `_run`
    # uses `check=True`, so those arrive as CalledProcessError/TimeoutExpired,
    # not NpmReleaseError -- catching only NpmReleaseError here meant the retry
    # loop never ran for the most common transient failures.
    last_error: Exception | None = None
    for attempt in range(REGISTRY_PROBE_ATTEMPTS):
        try:
            if attempt > 0:
                _run(_npm(), "cache", "clean", "--force", check=False)
                time.sleep(REGISTRY_PROBE_BACKOFF_SECONDS)
                shutil.rmtree(prefix, ignore_errors=True)
            _run(
                _npm(),
                "install",
                "--global",
                "--prefix",
                str(prefix),
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--registry",
                registry,
                f"{npm_package.LAUNCHER_NAME}@{version}",
            )
            _probe_install(prefix, omm_home, version, target_package)
            return
        except (NpmReleaseError, subprocess.SubprocessError, OSError) as error:
            last_error = error
    detail = f"{type(last_error).__name__}: {last_error}"
    if isinstance(last_error, subprocess.CalledProcessError):
        detail += (
            f"\nstdout: {last_error.stdout or ''}"
            f"\nstderr: {last_error.stderr or ''}"
        )
    if not isinstance(last_error, NpmReleaseError):
        # Only NpmReleaseError already carries the #242 tree dump.
        detail = f"{detail}\nnpm ls:\n{_install_tree(prefix)}"
    raise NpmReleaseError(
        f"npm registry path still broken after {REGISTRY_PROBE_ATTEMPTS} attempts "
        f"(post-publish optional-dependency propagation lag or a real defect; "
        f"issue #237)\n{detail}"
    )


def _audit_entry_names(entries: Any) -> list[str]:
    if not isinstance(entries, list):
        return []
    names = []
    for entry in entries:
        if isinstance(entry, dict):
            names.append(f"{entry.get('name')}@{entry.get('version')}")
        else:
            names.append(str(entry))
    return sorted(names)


def _audit_signatures(audit: Path, version: str, target_package: str) -> None:
    """Require verified registry signatures *and* provenance attestations.

    ``npm audit signatures`` exits non-zero only when a registry *signature* is
    invalid or missing. Attestations are merely counted in its human-readable
    summary, so a version published without provenance passes the bare command
    -- verified against npm 11.12.1: a tree holding only ``lodash@4.17.21``
    (registry signature, no provenance) exits 0.

    Parse the report instead. ``--include-attestations`` is what adds the
    ``verified`` array; plain ``--json`` reports only ``invalid`` and
    ``missing``. Every OMM package installed in the audited tree -- the launcher
    plus the one platform package npm resolved for this host -- must appear
    there, which is what the design doc's "verify registry signatures and
    provenance" actually promises.
    """

    result = _run(
        _npm(),
        "audit",
        "signatures",
        "--json",
        "--include-attestations",
        cwd=audit,
        check=False,
    )
    try:
        report = json.loads(result.stdout)
    except ValueError as error:
        raise NpmReleaseError(
            f"npm audit signatures returned invalid JSON (exit {result.returncode})\n"
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        ) from error
    if not isinstance(report, dict):
        raise NpmReleaseError("npm audit signatures did not return a report object")

    invalid = _audit_entry_names(report.get("invalid"))
    missing = _audit_entry_names(report.get("missing"))
    if invalid or missing:
        raise NpmReleaseError(
            "npm registry signatures are not trustworthy: "
            f"invalid={invalid}, missing={missing}"
        )
    verified = report.get("verified")
    if not isinstance(verified, list):
        raise NpmReleaseError(
            "npm audit signatures reported no attestation details; "
            f"report keys: {sorted(report)}"
        )
    attested = {
        (entry.get("name"), entry.get("version"))
        for entry in verified
        if isinstance(entry, dict)
    }
    expected = {(npm_package.LAUNCHER_NAME, version), (target_package, version)}
    if not expected <= attested:
        raise NpmReleaseError(
            "npm published these without a verified provenance attestation: "
            f"{sorted(f'{name}@{value}' for name, value in expected - attested)}; "
            f"attested: {_audit_entry_names(verified)}"
        )
    if result.returncode != 0:
        raise NpmReleaseError(
            f"npm audit signatures failed (exit {result.returncode}): {result.stderr}"
        )


def smoke_registry(version: str, target_name: str, registry: str = REGISTRY) -> None:
    registry = _validate_registry_url(registry)
    target_package = npm_package.targets()[target_name]["package"]
    with tempfile.TemporaryDirectory(prefix="omm-npm-registry-") as temporary:
        root = Path(temporary)
        prefix = root / "prefix"
        _install_launcher_from_registry(
            prefix, root / "omm-home", version, target_package, registry
        )

        audit = root / "audit"
        audit.mkdir()
        _run(_npm(), "init", "--yes", cwd=audit)
        _run(
            _npm(),
            "install",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            "--registry",
            registry,
            f"{npm_package.LAUNCHER_NAME}@{version}",
            cwd=audit,
        )
        _audit_signatures(audit, version, target_package)


def _registry_integrity(package: PackageInfo, registry: str) -> str | None:
    result = _run(
        _npm(),
        "view",
        f"{package.name}@{package.version}",
        "dist.integrity",
        "--json",
        "--registry",
        registry,
        check=False,
    )
    if result.returncode == 0:
        try:
            value = json.loads(result.stdout)
        except ValueError as error:
            raise NpmReleaseError("npm registry returned invalid integrity JSON") from error
        if not isinstance(value, str):
            raise NpmReleaseError("npm registry returned no package integrity")
        return value
    if "E404" in f"{result.stdout}\n{result.stderr}":
        return None
    raise NpmReleaseError(
        f"npm registry lookup failed for {package.name}@{package.version}: {result.stderr}"
    )


def _packed_contents(path: Path) -> dict[str, str]:
    """Return the sha256 of every packed file, keyed by tar member name.

    ``npm pack`` is not byte-reproducible across hosts: gzip framing and member
    timestamps differ even when the packed files are identical, so two correct
    builds of one version almost never share a tarball sha256. The extracted
    members are what actually ship, so member content is the only comparison
    that can prove two tarballs carry the same release.
    """

    try:
        with tarfile.open(path, mode="r:gz") as bundle:
            files = _tar_files(bundle)
    except (OSError, tarfile.TarError) as error:
        raise NpmReleaseError(f"cannot read npm tarball {path.name}: {error}") from error
    return {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}


def _differing_members(built: dict[str, str], published: dict[str, str]) -> list[str]:
    shared = built.keys() & published.keys()
    return sorted(
        (built.keys() ^ published.keys())
        | {name for name in shared if built[name] != published[name]}
    )


def reuse_published_packages(pack_dir: Path, registry: str = REGISTRY) -> None:
    """Adopt registry bytes for versions this run rebuilt identically.

    npm versions are immutable, so a rerun cannot overwrite an already-published
    version and must keep the registry copy for the checksum manifest and for
    ``publish_bundle``'s integrity guard. Adopting those bytes is only safe once
    they are proven to carry this run's build: the registry copy is downloaded,
    checked against the registry's own integrity, re-validated against the
    source contract, and then compared file by file with the freshly built
    tarball. A mismatch means the published version came from different sources
    and the release must not silently inherit it (a new version is the only fix).
    """

    registry = _validate_registry_url(registry)
    packages = verify_bundle(pack_dir, write_checksums=True)
    for package in packages:
        published_integrity = _registry_integrity(package, registry)
        if published_integrity is None:
            print(f"Not published yet; keeping built bytes: {package.name}@{package.version}")
            continue
        built_contents = _packed_contents(package.path)
        built_sha256 = _sha256(package.path)
        with tempfile.TemporaryDirectory(prefix="omm-npm-published-") as temporary:
            destination = Path(temporary)
            _run(
                _npm(),
                "pack",
                f"{package.name}@{package.version}",
                "--ignore-scripts",
                "--pack-destination",
                str(destination),
                "--json",
                "--registry",
                registry,
            )
            downloaded_tarballs = list(destination.glob("*.tgz"))
            if len(downloaded_tarballs) != 1:
                raise NpmReleaseError(
                    f"npm pack returned {len(downloaded_tarballs)} tarballs for "
                    f"{package.name}@{package.version}"
                )
            downloaded = inspect_tarball(downloaded_tarballs[0])
            if (
                downloaded.name,
                downloaded.version,
                downloaded.target,
            ) != (package.name, package.version, package.target):
                raise NpmReleaseError(
                    f"downloaded registry package identity does not match {package.name}"
                )
            if _integrity(downloaded.path) != published_integrity:
                raise NpmReleaseError(
                    f"downloaded registry package integrity does not match {package.name}"
                )
            published_contents = _packed_contents(downloaded.path)
            if published_contents != built_contents:
                raise NpmReleaseError(
                    f"published {package.name}@{package.version} does not contain the "
                    f"files this run built, so its bytes cannot be reused; "
                    f"differing packed files: {_differing_members(built_contents, published_contents)}; "
                    f"built tarball sha256 {built_sha256}, "
                    f"registry tarball sha256 {_sha256(downloaded.path)}"
                )
            shutil.copyfile(downloaded.path, package.path)
            print(f"Reused published bytes: {package.name}@{package.version}")
    verify_bundle(pack_dir, write_checksums=True)


def publish_bundle(pack_dir: Path, registry: str = REGISTRY) -> None:
    registry = _validate_registry_url(registry)
    packages = verify_bundle(pack_dir)
    ordered = sorted(packages, key=lambda package: package.target is None)
    for package in ordered:
        expected_integrity = _integrity(package.path)
        published_integrity = _registry_integrity(package, registry)
        if published_integrity is not None:
            if published_integrity != expected_integrity:
                raise NpmReleaseError(
                    f"existing {package.name}@{package.version} has different bytes"
                )
            print(f"Already published with matching bytes: {package.name}@{package.version}")
            continue
        result = _run(
            _npm(),
            "publish",
            str(package.path),
            "--access",
            "public",
            "--registry",
            registry,
            check=False,
        )
        if result.returncode != 0:
            raise NpmReleaseError(
                f"npm publish failed for {package.name}@{package.version}: {result.stderr}"
            )
        for attempt in range(8):
            published_integrity = _registry_integrity(package, registry)
            if published_integrity == expected_integrity:
                break
            if published_integrity is not None:
                raise NpmReleaseError(
                    f"published {package.name}@{package.version} has different bytes"
                )
            if attempt < 7:
                time.sleep(5)
        else:
            raise NpmReleaseError(
                f"published {package.name}@{package.version} did not appear in the registry"
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    bundle = commands.add_parser("verify-bundle")
    bundle.add_argument("--pack-dir", type=Path, required=True)
    bundle.add_argument("--write-checksums", action="store_true")

    smoke = commands.add_parser("smoke-tarballs")
    smoke.add_argument("--pack-dir", type=Path, required=True)
    smoke.add_argument("--target", choices=sorted(npm_package.targets()), required=True)

    registry_smoke = commands.add_parser("smoke-registry")
    registry_smoke.add_argument("--version", required=True)
    registry_smoke.add_argument("--target", choices=sorted(npm_package.targets()), required=True)
    registry_smoke.add_argument("--registry", default=REGISTRY)

    publish = commands.add_parser("publish-bundle")
    publish.add_argument("--pack-dir", type=Path, required=True)
    publish.add_argument("--registry", default=REGISTRY)

    reuse = commands.add_parser("reuse-published")
    reuse.add_argument("--pack-dir", type=Path, required=True)
    reuse.add_argument("--registry", default=REGISTRY)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify-bundle":
            verify_bundle(args.pack_dir, write_checksums=args.write_checksums)
        elif args.command == "smoke-tarballs":
            smoke_tarballs(args.pack_dir, args.target)
        elif args.command == "smoke-registry":
            smoke_registry(args.version, args.target, args.registry)
        elif args.command == "publish-bundle":
            publish_bundle(args.pack_dir, args.registry)
        elif args.command == "reuse-published":
            reuse_published_packages(args.pack_dir, args.registry)
    except (NpmReleaseError, OSError, subprocess.SubprocessError) as error:
        print(f"npm release validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
