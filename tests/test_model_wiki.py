from omm import model_wiki


def test_bundled_version_preserves_claim_basis_and_sources():
    data=model_wiki.catalog()
    assert data['contentVersion']=='2026-10-01.1' and len(data['models'])==7
    for model in data['models']:
        assert model['sources'] and model['ommEvaluation'] is None
        assert all(claim['basis'] in {'publisher','editorial'} for claim in model['strengths']+model['cautions'])
        assert isinstance(model['summary'],str)


def test_exact_checkpoint_and_reviewed_gguf_relation_match():
    direct=model_wiki.describe({'repo_id':'Qwen/Qwen3-8B'})
    package=model_wiki.describe({'repo_id':'Qwen/Qwen3-8B-GGUF'})
    assert direct['id']==package['id']=='qwen3-8b'
    assert direct['match']=='exact_repository'
    assert package['match']=='declared_base_repository'
    assert package['matchSource']['source'].startswith('https://huggingface.co/')


def test_other_versions_sizes_and_uploaders_are_not_guessed():
    for repo in ('Qwen/Qwen3.8-8B','Qwen/Qwen3-32B','Qwen/Qwen2.5-8B','other/Qwen3-8B-GGUF'):
        assert model_wiki.describe({'repo_id':repo}) is None
    assert model_wiki.is_stale('2020-01-01') is True
    assert model_wiki.is_stale('invalid') is True
