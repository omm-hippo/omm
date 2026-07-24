"""Model-hub providers (HuggingFace, ModelScope, ...). Each provider module
implements fetch_repo_files/download_url/remote_file_size/remote_file_sha256
with the same signatures - see providers/base.py for the shared exceptions
and hub.py for the dispatch layer that picks a module by provider name."""
