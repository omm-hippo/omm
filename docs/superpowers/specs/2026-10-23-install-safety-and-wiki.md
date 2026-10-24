# Mandatory managed-installer checks and expanded model wiki

User authorization: require security checks for software installed through #423,
and substantially expand #425. Keep personal installations, security settings,
foreground applications and models untouched during development.

Installation/update checks cannot be disabled by an OMM flag or preference.
Before a managed operation, validate the exact official package identity/source,
reject known integrity/quarantine bypass environment settings, require cask
SHA-256 metadata and package-manager integrity enforcement, and inspect OS
protection/scanner availability. After installation/update, inspect the actual
installed artifact: macOS codesign + Gatekeeper; Windows Authenticode + a bounded
Defender custom scan without remediation; Linux Flatpak source verification +
ClamAV scanning of the installed app. Unsupported or unverifiable paths fail
closed. Uninstallation remains available. Failed postchecks may leave package
files installed, and must explicitly say so rather than starting the app or
claiming success. Never auto-disable OS protections or quarantine.

These are specific trust/integrity/known-malware checks, not a guarantee of no
vulnerabilities or malicious behavior. This phase covers OMM-managed runner
installation/update, not every program on the computer, OS bootstrapping or an
universal vulnerability scan. Existing OMM source/release SSH/checksum contracts
remain in force. Document the boundaries plainly; tests/fixtures do not prove
native Windows/Linux or clean-device installer flows.

For the wiki, retain schema and existing curated descriptions, add at least
100 independently identified official model repositories, and pin metadata and
configuration sources to reviewed revisions. Preserve unknown license/context/
tool support instead of guessing. Do not infer quality, runner compatibility,
or GGUF availability from a model name. Preserve exact checkpoint matching.
Add bilingual factual descriptions, source/review information and usable search
across publishers, aliases and model metadata. Runtime use stays bundled/offline.
The metadata collection does not download weights or execute remote code.

Validate fail-closed installation gates, update/reinstall paths, unsupported
source/bypass protection, native command contracts, catalog schema/provenance,
missing metadata, exact matching, browser search/rendering and packaged assets.
