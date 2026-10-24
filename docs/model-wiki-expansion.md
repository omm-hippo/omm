# Model wiki expansion: 7 to 114 exact checkpoints

Related issue: #425. The bundle includes 114 distinct repository/checkpoint
entries across eleven publishers: Qwen, Meta, Google, Microsoft, Mistral AI,
DeepSeek, LG AI Research, IBM, Hugging Face, OpenAI and Z.ai. It includes different
model scales/versions explicitly, not aliases counted as separate models.

The original seven curated descriptions and their sources/review dates are
preserved unchanged. The additional 107 entries use reviewed official hub
metadata, model configuration and publisher documentation. Descriptions are
bilingual factual summaries, with sources, review dates, license labels,
confirmed inputs, context limits when available, architecture, declared tool
format and explicit unknown values. No OMM quality measurements are invented.

`scripts/data/model-wiki-curated-v1.json` preserves the original curated source.
`scripts/data/model-wiki-reviewed-20261023.json` contains reviewed facts and
provenance. Rebuild without network access using:

```sh
python scripts/build_model_wiki.py
```

Collection stopped immediately after the public hub returned HTTP 429. At that
point 93 official repository metadata snapshots and 71 config files had been
collected. Eighteen additional checkpoints were documented by the publishers'
official GitHub repositories. Overlap with four original descriptions is
deduplicated by exact repository identity. No weight download or remote code
execution was used. Do not automatically resume against a rate-limited source.

Hub configuration sources use exact revision URLs. Publisher documentation
also uses exact Git revisions, checked against collected content hashes before
pinning. These are metadata/configuration reviews, not a claim that every
full model card or every translated description was independently benchmarked.

Unknown context/license/tool capabilities stay unknown. A tool-call template
is not proof of model capability or successful agent tasks. Configured context
limits are not verified runner settings. Repository-name scale labels are not
measurements or estimates of required RAM; a MoE's active parameters are not
the total resident weights. Stated languages do not establish language quality.
Base checkpoints need appropriate tuning/templates and are not guaranteed chat
assistants. Source license terms must be checked at the exact checkpoint.

The local GUI supports publisher/task filtering, Korean aliases, repository/
license/language search and 20-row pagination. Details distinguish publisher
information, editorial cautions and absent OMM evaluations. Existing exact
checkpoint/GGUF base-model mapping remains intact; the larger wiki does not
make every entry directly installable, alter recommendation ranking or copy
facts onto similarly named third-party packages.

Runtime wiki use remains bundled/offline. The separate omm.run site catalog
and deployed site are not changed by this local bundle update.
