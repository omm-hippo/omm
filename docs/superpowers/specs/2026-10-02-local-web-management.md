# Local model management and first response

Related work: #421, #423, #424, #425, #433.

OMM's browser manager reuses its existing model hub, package verification,
runner linking, hardware detection, and recommendation functions. The browser
and management API share one loopback origin. One owned worker performs each
confirmed file operation, with durable job outcomes and idempotent request IDs.

This implementation extends the manager with a separately confirmed, bounded
Ollama/LM Studio response check. It records compatibility independently of
installation, shows the returned sample, releases only the load it created,
and preserves preloaded models. Live memory planning runs before loading;
insufficient headroom stops the check without unloading another user's model.
Runtime failures leave the downloaded file available for repair or retry.

Source launchers and first-use guidance connect a successful OMM installation
to the local manager. Login startup is outside this change. Generated launchers are reviewed and
unit-tested; real Finder clicks and Windows installation must be reported
separately from wrapper or packaging checks.

Recommendation evidence is shared by web and CLI/JSON. Catalog totals and
machine-local correction are distinct from matched environment support. New
training metadata describes real training/holdout support without counting
synthetic bootstrap rows as measurements. Missing metadata in older signed
catalogs remains an explicit unknown, never a confidence percentage.

The existing model wiki is reused with a pinned content version and source
links. Explanations match explicit checkpoint/repository identifiers. Missing
or stale descriptions remain visible as such. Developer claims, editorial
notes, and OMM measurements are not combined into a quality score.

Validation covers real browser requests, persisted files and registry entries,
runner-side links, cancellation, a real first response on an isolated local
Ollama daemon, owned-load cleanup, restarts, desktop/mobile rendering, wheel
assets, and the documented command contract. Human usability evaluation and
platforms unavailable locally remain explicitly unverified.
