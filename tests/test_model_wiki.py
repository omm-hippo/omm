from omm import model_wiki


def test_bundled_version_preserves_claim_basis_and_sources():
    data=model_wiki.catalog()
    assert data['contentVersion']=='2026-10-23.1' and len(data['models'])>=100
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
    for repo in ('Qwen/Qwen3.8-8B','Qwen/Qwen2.5-8B','other/Qwen3-8B-GGUF'):
        assert model_wiki.describe({'repo_id':repo}) is None
    assert model_wiki.is_stale('2020-01-01') is True
    assert model_wiki.is_stale('invalid') is True


def test_new_size_is_an_independent_checkpoint_not_a_copy_of_existing_model():
    small = model_wiki.describe({'repo_id':'Qwen/Qwen3-8B'})
    large = model_wiki.describe({'repo_id':'Qwen/Qwen3-32B'})
    assert small['id'] != large['id']
    assert large['sizeLabel'] == '32B'
    assert large['match'] == 'exact_repository'
    assert large['packageRepository'] == 'Qwen/Qwen3-32B'


def test_all_checkpoints_have_distinct_ids_sources_and_honest_unknowns():
    catalog = model_wiki.load()
    models = catalog['models']
    assert len({m['repository'].casefold() for m in models}) == len(models)
    assert len({m['id'] for m in models}) == len(models)
    assert len({m['publisher'] for m in models}) >= 10
    unknown_context = 0
    for model in models:
        source_ids = {s['id'] for s in model['sources']}
        assert all(s['url'].startswith('https://') for s in model['sources'])
        for claim in model['strengths'] + model['cautions']:
            assert claim['text']['ko'] and claim['text']['en']
            if claim['basis'] == 'publisher':
                assert claim['sourceId'] in source_ids
        if model['context']['tokens'] is None:
            unknown_context += 1
            assert model['context']['basis'] == 'unknown'
        assert model['toolCalling'] in (None, True, False)
        assert model['ommEvaluation'] is None
    assert unknown_context > 0  # Missing sources were not filled with guessed limits.


def test_expansion_rebuilds_offline_and_preserves_curated_descriptions():
    import json
    from pathlib import Path
    from scripts.build_model_wiki import build
    root = Path(__file__).resolve().parents[1]
    curated = json.loads((root/'scripts/data/model-wiki-curated-v1.json').read_text(encoding='utf-8'))
    reviewed = json.loads((root/'scripts/data/model-wiki-reviewed-20261023.json').read_text(encoding='utf-8'))
    for record in reviewed['records']:
        for source in record['sources']:
            assert len(source['revision']) == 40
            assert source['revision'] in source['url']
    current = model_wiki.load()
    assert build(curated, reviewed) == current
    assert current['models'][:len(curated['models'])] == curated['models']


def test_metadata_tool_template_is_not_promoted_to_a_working_tool_claim():
    model = model_wiki.describe({'repo_id':'Qwen/Qwen3-4B'})
    assert model['toolFormatDeclared'] is True
    assert model['toolCalling'] is None
    assert model['ommEvaluation'] is None


def test_korean_models_and_local_reasoning_models_are_in_catalog():
    for repo in ('LGAI-EXAONE/EXAONE-4.0-1.2B', 'LGAI-EXAONE/EXAONE-3.5-7.8B-Instruct',
                 'openai/gpt-oss-20b', 'HuggingFaceTB/SmolLM2-135M-Instruct', 'ibm-granite/granite-3.3-8b-instruct'):
        assert model_wiki.describe({'repo_id':repo})['repository'] == repo


def test_recommend_detail_shows_use_cases_limits_source_or_missing():
    from omm import recommend_ui
    lines=[text for text,_ in recommend_ui.wiki_detail_lines(model_wiki.describe({'repo_id':'Qwen/Qwen3-8B-GGUF'},'en'))]
    joined='\n'.join(lines)
    assert any(line.startswith('Use cases: ') for line in lines)
    assert any(line.startswith('Limits: ') for line in lines)
    assert 'reviewed ' in joined and 'https://' in joined
    assert 'base checkpoint Qwen/Qwen3-8B' in joined
    missing=recommend_ui.wiki_detail_lines(model_wiki.describe({'repo_id':'other/Qwen3-8B-GGUF'},'en'))
    assert len(missing)==1 and '정보 없음' in missing[0][0]
