from omm import cli, linker


def test_missing_engines_note_is_none_when_all_installed():
    installed = {spec.key: True for spec in linker.ENGINES}

    assert cli._missing_engines_note(installed) is None


def test_missing_engines_note_counts_and_links_when_some_missing():
    installed = {spec.key: spec.key == "ollama" for spec in linker.ENGINES}
    missing_count = len(linker.ENGINES) - 1

    note = cli._missing_engines_note(installed)

    assert note is not None
    assert f"+ {missing_count} program(s) not installed" in note
    assert cli.COMPATIBLE_PROGRAMS_URL in note


def test_missing_engines_note_counts_all_when_none_installed():
    installed = {spec.key: False for spec in linker.ENGINES}

    note = cli._missing_engines_note(installed)

    assert note is not None
    assert f"+ {len(linker.ENGINES)} program(s) not installed" in note
