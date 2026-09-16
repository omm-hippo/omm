# Support report and install verification implementation plan

1. Add an allow-listed support-report builder that reads only scrubbed command
   names, exception classes, package metadata, and aggregate doctor status.
2. Add `omm report` preview, explicit optional groups, and local save controls
   with no outbound action.
3. Add source-plan/result data structures for model installation and render
   them only in normal interactive installs.
4. Resolve provider size/digest once, enforce known size after download, and
   preserve the existing HTTPS, redirect, SHA-256, disk, and ownership paths.
5. Add focused privacy and install tests, then run the full suite, packaging
   checks, wheel installation smoke, and remote CI before handing off the PR.
