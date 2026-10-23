# Mandatory checks for OMM-managed runner installation and updates

Related issue: #423. There is no OMM setting or flag to skip these checks.
The scope is software OMM installs/updates through its reviewed runner package
routes, not a scan of every application already on the computer.

## Before package-manager execution

- Accept only the exact reviewed engine package and its platform route.
- Refuse known checksum, signature, quarantine and repository override flags/
  environment variables. Do not disable or repair OS protections automatically.
- macOS: require Gatekeeper enabled and exactly one official `homebrew/cask`
  record with a fixed version, HTTPS and SHA-256. Require checksum/quarantine
  flags, select the official qualified cask and disable Homebrew auto-update
  between inspection and execution. Unverifiable formula routes are blocked.
- Windows: inspect WinGet's `winget` source and require the official Microsoft
  endpoint/type. Require active Defender antivirus/real-time protection with
  signatures no older than seven days. Keep WinGet installer hash verification
  enabled; never pass its ignore-security switches.
- Linux: require one official Flathub source with GPG verification and the
  exact reviewed application ID. Require ClamAV and successfully load its
  official database with a seven-day maximum age before deployment.

The package manager remains responsible for transport, signed repository/
manifest trust and actual download integrity. A checksum authenticates bytes
against its source; it does not prove those bytes are harmless. macOS cask
metadata is not an independently maintained vulnerability feed.

## After installation/update, before reporting success

Linux scans use bounded limits of 1024 MiB per file, 2048 MiB scanned data per
input, and 300 seconds for the command, to cover large bundled GPU libraries.
Exceeding a limit remains a failure; no large file is silently accepted.

| Platform | Actual installed artifact check |
| --- | --- |
| macOS | Strict deep code-signature verification and Gatekeeper assessment of the app bundle |
| Windows | Authenticode validation and a bounded Defender custom scan of the executable's installation directory, with remediation disabled |
| Linux | Confirm a recognized Flatpak app location and run ClamAV with current official signatures; empty scans, detected threats, scan limits/encrypted material or scanner failures do not pass |

Detection is read-only. OMM does not quarantine/delete files, change exclusions,
disable OS security or launch the installed app as part of verification.
Some native tools may require elevated privileges; inability to run is a
failed check. Other antivirus products do not silently replace the currently
implemented Defender/ClamAV contracts.

**Important boundary:** the post-install artifact checks happen after the
package manager has installed files. They are not a scan of all installer code
before it runs. If a postcheck fails, the package may still be installed; OMM
reports failure and advises against launching. It does not silently remove
files or user data. Native signing/known-malware checks do not prove absence
of vulnerabilities, malicious behavior, unsafe runtime settings, or problems
in every shared dependency. Existing external/manual installations remain
outside this managed-install contract.

Successful and failed postchecks are recorded locally in
`OMM_HOME/security-checks/<engine>.json`. The GUI connections view and engine
status JSON show the last installer check with its timestamp. This is historic
evidence, not continuous monitoring or proof that the app is unchanged now.
Reports exclude paths/native error bodies and do not enter any upload channel.
An explicit OMM data purge removes these owned records. Uninstalling a runner
does not require a working scanner, so a blocked application can be removed.

Existing OMM bootstrap/release SSH signatures, artifact checksums and package
manager verification remain in effect. This change does not add a universal
vulnerability audit of Python/git/Homebrew/OS bootstrapping, or certify arbitrary
software installed outside OMM. Windows/Linux native tests and fresh-device
installation flows must be reported separately from command-fixture tests.

References reviewed 2026-10-23:

- [Homebrew command flags](https://docs.brew.sh/Manpage)
- [WinGet download integrity](https://learn.microsoft.com/windows/package-manager/winget/download)
- [Apple Developer ID and Gatekeeper](https://developer.apple.com/developer-id/)
- [Defender custom scans and DisableRemediation](https://learn.microsoft.com/defender-endpoint/command-line-arguments-microsoft-defender-antivirus)
- [Flatpak command and GPG verification](https://docs.flatpak.org/en/latest/flatpak-command-reference.html)
- [ClamAV scanning](https://docs.clamav.net/manual/Usage/Scanning.html)
