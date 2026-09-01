#!/usr/bin/env python3
"""Build-independent checks for OMM release archives.

The release workflow calls this module after ``python -m build``.  Keeping
the checks in Python instead of shell makes the same validation available on
macOS, Linux, and Windows runners.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
CHECKSUMS_FILENAME = "SHA256SUMS"
SUBPROCESS_TIMEOUT_SECONDS = 300
MAX_ARCHIVE_MEMBERS = 10_000
MAX_UNPACKED_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024


class ReleaseValidationError(RuntimeError):
    """Raised when an archive cannot be tied to the declared release."""


def _project_table(text: str) -> str:
    match = re.search(r"(?ms)^\[project\]\s*$\n(.*?)(?=^\[|\Z)", text)
    if match is None:
        raise ReleaseValidationError("pyproject.toml has no [project] table")
    return match.group(1)


def project_identity(pyproject: Path = PYPROJECT) -> tuple[str, str]:
    table = _project_table(pyproject.read_text(encoding="utf-8"))

    def value(field: str) -> str:
        match = re.search(rf'(?m)^{re.escape(field)}\s*=\s*"([^"]+)"\s*$', table)
        if match is None:
            raise ReleaseValidationError(f"[project].{field} must be a literal string")
        return match.group(1)

    return value("name"), value("version")


def validate_tag(tag: str, version: str) -> None:
    expected = f"v{version}"
    if tag != expected:
        raise ReleaseValidationError(
            f"release tag {tag!r} does not match pyproject version; expected {expected!r}"
        )


def _git(
    repository: Path,
    *args: str,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=check,
        capture_output=capture_output,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )


def _git_output(repository: Path, *args: str) -> str:
    return _git(repository, *args, capture_output=True).stdout.strip()


def verify_release_identity(
    tag: str,
    *,
    repository: Path = ROOT,
    remote: str = "origin",
    main_branch: str = "main",
) -> tuple[str, str]:
    """Tie a signed release tag to the checked-out project and main history."""

    repository = repository.resolve()
    pyproject = repository / "pyproject.toml"
    allowed_signers = repository / "src" / "omm" / "trust" / "allowed_signers"
    if not repository.is_dir():
        raise ReleaseValidationError(f"release repository does not exist: {repository}")
    if not allowed_signers.is_file():
        raise ReleaseValidationError(f"allowed signers file does not exist: {allowed_signers}")

    _, version = project_identity(pyproject)
    validate_tag(tag, version)

    signature = _git(
        repository,
        "-c",
        f"gpg.ssh.allowedSignersFile={allowed_signers}",
        "verify-tag",
        tag,
        check=False,
    )
    if signature.returncode != 0:
        raise ReleaseValidationError(
            f"release tag {tag!r} is not signed by an allowed signer"
        )

    tag_commit = _git_output(repository, "rev-parse", f"{tag}^{{commit}}")
    checkout_commit = _git_output(repository, "rev-parse", "HEAD^{commit}")
    if tag_commit != checkout_commit:
        raise ReleaseValidationError(
            f"release tag {tag!r} points to {tag_commit}, but HEAD is {checkout_commit}"
        )

    remote_ref = f"refs/remotes/{remote}/{main_branch}"
    _git(
        repository,
        "fetch",
        "--no-tags",
        remote,
        f"{main_branch}:{remote_ref}",
    )
    ancestry = _git(
        repository,
        "merge-base",
        "--is-ancestor",
        tag_commit,
        remote_ref,
        check=False,
    )
    if ancestry.returncode != 0:
        raise ReleaseValidationError(
            f"release commit {tag_commit} is not in {remote}/{main_branch} history"
        )
    return version, tag_commit


def _metadata_identity(payload: bytes, source: Path) -> tuple[str, str]:
    metadata = BytesParser().parsebytes(payload)
    name = metadata.get("Name")
    version = metadata.get("Version")
    if not name or not version:
        raise ReleaseValidationError(f"{source.name} metadata has no Name/Version")
    return name, version


def _safe_archive_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or "\\" in name
        or not path.parts
        or name != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ReleaseValidationError(f"archive contains unsafe path {name!r}")
    return path


def _wheel_identity(wheel: Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            if len(names) > MAX_ARCHIVE_MEMBERS:
                raise ReleaseValidationError(
                    f"{wheel.name} contains too many archive members"
                )
            unpacked_size = 0
            normalized_names: set[str] = set()
            for info in archive.infolist():
                normalized = _safe_archive_path(info.filename.rstrip("/")).as_posix()
                if normalized in normalized_names:
                    raise ReleaseValidationError(
                        f"{wheel.name} contains duplicate path {normalized!r}"
                    )
                normalized_names.add(normalized)
                unpacked_size += info.file_size
                if unpacked_size > MAX_UNPACKED_ARCHIVE_BYTES:
                    raise ReleaseValidationError(
                        f"{wheel.name} exceeds the unpacked size limit"
                    )
                mode = info.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if (
                    info.flag_bits & 0x1
                    or (
                        info.create_system == 3
                        and file_type not in {0, stat.S_IFREG, stat.S_IFDIR}
                    )
                ):
                    raise ReleaseValidationError(
                        f"{wheel.name} contains an unsafe member {info.filename!r}"
                    )
            metadata_files = [
                name for name in names if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_files) != 1:
                raise ReleaseValidationError(
                    f"{wheel.name} must contain exactly one .dist-info/METADATA file"
                )
            if archive.getinfo(metadata_files[0]).file_size > MAX_METADATA_BYTES:
                raise ReleaseValidationError(
                    f"{wheel.name} metadata exceeds the size limit"
                )
            return _metadata_identity(archive.read(metadata_files[0]), wheel)
    except zipfile.BadZipFile as error:
        raise ReleaseValidationError(f"cannot read wheel {wheel.name}: {error}") from error


def _sdist_identity(sdist: Path) -> tuple[str, str]:
    try:
        with tarfile.open(sdist, "r:gz") as archive:
            members = archive.getmembers()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ReleaseValidationError(
                    f"{sdist.name} contains too many archive members"
                )
            roots: set[str] = set()
            normalized_names: set[str] = set()
            unpacked_size = 0
            for member in members:
                path = _safe_archive_path(member.name.rstrip("/"))
                normalized = path.as_posix()
                if normalized in normalized_names:
                    raise ReleaseValidationError(
                        f"{sdist.name} contains duplicate path {normalized!r}"
                    )
                normalized_names.add(normalized)
                roots.add(path.parts[0])
                if not (member.isfile() or member.isdir()):
                    raise ReleaseValidationError(
                        f"{sdist.name} contains an unsafe member {member.name!r}"
                    )
                if member.isfile():
                    unpacked_size += member.size
                    if unpacked_size > MAX_UNPACKED_ARCHIVE_BYTES:
                        raise ReleaseValidationError(
                            f"{sdist.name} exceeds the unpacked size limit"
                        )
            if len(roots) != 1:
                raise ReleaseValidationError(
                    f"{sdist.name} must contain exactly one top-level directory"
                )
            metadata_files = [
                member
                for member in members
                if member.isfile()
                and member.name.count("/") == 1
                and member.name.endswith("/PKG-INFO")
            ]
            if len(metadata_files) != 1:
                raise ReleaseValidationError(
                    f"{sdist.name} must contain exactly one top-level PKG-INFO file"
                )
            if metadata_files[0].size > MAX_METADATA_BYTES:
                raise ReleaseValidationError(
                    f"{sdist.name} metadata exceeds the size limit"
                )
            stream = archive.extractfile(metadata_files[0])
            if stream is None:
                raise ReleaseValidationError(f"cannot read metadata from {sdist.name}")
            return _metadata_identity(stream.read(), sdist)
    except tarfile.TarError as error:
        raise ReleaseValidationError(f"cannot read sdist {sdist.name}: {error}") from error


def distribution_archives(dist_dir: Path) -> tuple[Path, Path]:
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseValidationError(
            f"expected one wheel and one sdist in {dist_dir}, found "
            f"{len(wheels)} wheel(s) and {len(sdists)} sdist(s)"
        )
    return wheels[0], sdists[0]


def _validate_dist_contents(
    dist_dir: Path, archives: tuple[Path, Path], *, require_checksums: bool
) -> None:
    expected = {path.name for path in archives}
    checksum_file = dist_dir / CHECKSUMS_FILENAME
    if require_checksums or checksum_file.exists():
        expected.add(CHECKSUMS_FILENAME)
    actual = {entry.name for entry in dist_dir.iterdir()}
    if actual != expected:
        raise ReleaseValidationError(
            f"release bundle contains {sorted(actual)}, expected exactly {sorted(expected)}"
        )
    for entry in dist_dir.iterdir():
        if not entry.is_file() or entry.is_symlink():
            raise ReleaseValidationError(
                f"release bundle entry {entry.name!r} must be a regular file"
            )


def validate_archives(dist_dir: Path, pyproject: Path = PYPROJECT) -> tuple[Path, Path]:
    expected = project_identity(pyproject)
    wheel, sdist = distribution_archives(dist_dir)
    _validate_dist_contents(dist_dir, (wheel, sdist), require_checksums=False)
    if not wheel.name.endswith("-py3-none-any.whl"):
        raise ReleaseValidationError(
            f"{wheel.name} is not the expected platform-independent Python 3 wheel"
        )
    for archive, actual in ((wheel, _wheel_identity(wheel)), (sdist, _sdist_identity(sdist))):
        if actual != expected:
            raise ReleaseValidationError(
                f"{archive.name} identifies {actual[0]} {actual[1]}, "
                f"but pyproject declares {expected[0]} {expected[1]}"
            )
    return wheel, sdist


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_checksums(dist_dir: Path) -> Path:
    archives = distribution_archives(dist_dir)
    _validate_dist_contents(dist_dir, archives, require_checksums=False)
    destination = dist_dir / CHECKSUMS_FILENAME
    lines = [f"{_sha256(path)}  {path.name}" for path in sorted(archives)]
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return destination


def validate_checksums(dist_dir: Path) -> None:
    checksum_file = dist_dir / CHECKSUMS_FILENAME
    if not checksum_file.is_file():
        raise ReleaseValidationError(f"missing {checksum_file}")
    archives = distribution_archives(dist_dir)
    _validate_dist_contents(dist_dir, archives, require_checksums=True)
    expected_files = {path.name for path in archives}
    seen: set[str] = set()
    for line in checksum_file.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/]+)", line)
        if match is None:
            raise ReleaseValidationError(f"invalid checksum line: {line!r}")
        expected_hash, filename = match.groups()
        if filename in seen:
            raise ReleaseValidationError(f"duplicate checksum entry for {filename}")
        if filename not in expected_files:
            raise ReleaseValidationError(f"unexpected checksum entry for {filename}")
        path = dist_dir / filename
        if not path.is_file() or _sha256(path) != expected_hash:
            raise ReleaseValidationError(f"checksum mismatch for {filename}")
        seen.add(filename)
    if seen != expected_files:
        raise ReleaseValidationError(
            f"checksum file covers {sorted(seen)}, expected {sorted(expected_files)}"
        )


def _venv_executable(root: Path, name: str, platform_name: str = os.name) -> Path:
    scripts = root / ("Scripts" if platform_name == "nt" else "bin")
    suffix = ".exe" if platform_name == "nt" else ""
    return scripts / f"{name}{suffix}"


def smoke_install(dist_dir: Path, pyproject: Path = PYPROJECT) -> None:
    name, version = project_identity(pyproject)
    wheel, _ = validate_archives(dist_dir, pyproject)
    validate_checksums(dist_dir)
    with tempfile.TemporaryDirectory(prefix="omm-release-smoke-") as temporary:
        root = Path(temporary)
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        python = _venv_executable(environment, "python")
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                str(wheel),
            ],
            check=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
        probe = (
            "import importlib.metadata, omm, localfit_server; "
            f"assert importlib.metadata.version({name!r}) == {version!r}"
        )
        subprocess.run(
            [str(python), "-c", probe],
            check=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
        command = _venv_executable(environment, "omm")
        command_env = os.environ.copy()
        command_env["OMM_HOME"] = str(root / "omm-home")
        version_result = subprocess.run(
            [str(command), "--version"],
            check=True,
            env=command_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
        expected_version = f"omm {version}"
        if version_result.stdout.strip() != expected_version:
            raise ReleaseValidationError(
                f"installed command reported {version_result.stdout.strip()!r}, "
                f"expected {expected_version!r}"
            )
        subprocess.run(
            [str(command), "help"],
            check=True,
            env=command_env,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    tag_parser = subparsers.add_parser("check-tag")
    tag_parser.add_argument("--tag", required=True)

    identity_parser = subparsers.add_parser("verify-release")
    identity_parser.add_argument("--tag", required=True)
    identity_parser.add_argument("--repository", type=Path, default=ROOT)
    identity_parser.add_argument("--remote", default="origin")
    identity_parser.add_argument("--main-branch", default="main")

    for command in ("verify-dist", "write-checksums", "verify-checksums", "smoke-install"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "check-tag":
            _, version = project_identity()
            validate_tag(args.tag, version)
        elif args.command == "verify-release":
            version, commit = verify_release_identity(
                args.tag,
                repository=args.repository,
                remote=args.remote,
                main_branch=args.main_branch,
            )
            print(f"Verified signed release v{version} at {commit}")
        elif args.command == "verify-dist":
            validate_archives(args.dist_dir)
        elif args.command == "write-checksums":
            print(write_checksums(args.dist_dir))
        elif args.command == "verify-checksums":
            validate_checksums(args.dist_dir)
        elif args.command == "smoke-install":
            smoke_install(args.dist_dir)
        else:  # pragma: no cover - argparse constrains the command
            raise AssertionError(args.command)
    except (OSError, ReleaseValidationError, subprocess.SubprocessError) as error:
        print(f"release validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
