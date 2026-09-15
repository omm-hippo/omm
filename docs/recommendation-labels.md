# Recommendation labels

`omm recommend` separates model kind (`TYPE`) from intended task (`BEST FOR`).
It uses these same labels in the picker, selected-model detail, and `--json`.

| TYPE | Meaning |
| --- | --- |
| LLM | Text language model |
| VLM | Vision-language model |
| Embedding | Model producing vectors for retrieval or similarity |
| Unknown | Missing, unsupported, or conflicting type metadata |

BEST FOR has six labels: **General**, **Coding**, **Reasoning** (including math),
**Writing** (including creative writing and roleplay), **Translation**, and
**Documents** (summarization, long-document work, document Q&A). `—` means there
is insufficient task information or the text-generation taxonomy does not
apply, as with embeddings. Tool use appears only as a declared feature in the
selected-model detail, not as a seventh purpose.

## Evidence and compatibility

Labels come from bounded `pipeline_tag`, `tags`, `capabilities`, `model_type`,
and `use_case` metadata. A declared `use_case` wins over task tags; ties between
tags use the table order above. Specialized vision/embedding metadata takes
priority over generic text-generation tags. Conflicting vision and embedding
metadata yields Unknown. Model names, uploader names, download counts, parameter
counts, and GGUF filenames are not evidence of type or task quality.

The sources are shown in the detail and JSON (`model_type_source`,
`use_case_source`). Catalog metadata is a provider declaration, not an OMM
benchmark or proof of runtime feature support. VLM does not assert that image
input or a required projector works in an installed engine.

Old signed catalogs remain readable and are not modified or re-signed. Their
missing fields yield Unknown / — except for exact bundled artifacts (including
their static-rule aliases), classified as LLM / General from their model cards:

- [TinyLlama Chat](https://huggingface.co/TinyLlama/TinyLlama-1.1B-Chat-v1.0)
- [Llama 3.1 Instruct](https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct)
- [Mistral v0.2 Instruct](https://huggingface.co/mistralai/Mistral-7B-Instruct-v0.2)

The existing HF and ModelScope searches now preserve task metadata they already
receive. This adds no provider requests. Future generated/signed catalogs carry
those fields through the existing training path. Label classification does not
change speed or memory predictions, download targets, or installation behavior.

## Shortlist and package details

The hardware panel shows the selected profile's memory budget, capped by the
installation limit: on a 24 GB unified-memory Mac, Dedicated is 19.2 GB,
Balanced is 10.8 GB, and Minimal is 4.8 GB. Profiles limit memory; the ranking
still prefers larger usable models inside that budget. The heading reports
shown entries and eligible packages before grouping and truncation separately.
If a fallback exceeds the requested budget, the picker says so explicitly.

JSON rows include `profile_budget_gb`, nullable `within_profile`,
`eligible_package_count`, and `memory_estimate_basis` (`file_size`,
`parameter_metadata`, `model_name`, or `unknown`). Memory remains an estimate
including runtime overhead. The detail explains when actual file size is
unavailable and the estimate comes from parameter metadata or the model name;
different models may therefore share estimates. No displayed estimate proves
actual speed, live free memory, or runtime compatibility.

After hardware filtering and ranking, recommendations group matching repository
model names and filenames across uploaders and quantizations before selecting up
to ten entries. The first ranked package keeps its exact install reference and
its own task metadata; labels from a discarded mirror are not substituted.

Normal models appear before specialized or uncensored variants. Within each
group, the shortlist first admits up to two entries per model family, then fills
remaining places with the other eligible models. Model versions, sizes, and
fine-tune differences remain distinct. These are selection heuristics, not
quality scores or verification of provider trust or runtime compatibility.

The selected-model detail shows the provider, named quantization, and exact
filename alongside TYPE, BEST FOR, and their classification sources.

## Terminal layout

At 88 columns and above all columns appear. At 68–87 columns MEMORY is hidden;
at 48–67 columns BEST FOR is also hidden; below 48 TYPE is also hidden. Both
labels and their sources remain available in the selected-model detail at every
width. Full words fit within their columns, including Embedding and Translation.

## Implementation and verification plan

1. Classify metadata without network access or model-name heuristics.
2. Preserve provider task metadata and expose the labels in all recommendation outputs.
3. Verify taxonomy, old catalogs, malformed inputs, provider-to-display propagation,
   narrow layouts, JSON, and real picker selection/cancellation in an isolated terminal.
