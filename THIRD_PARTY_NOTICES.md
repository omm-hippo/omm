# Third-Party Notices

`omm` is licensed under the [MIT License](LICENSE). It depends on the
following third-party packages, all under permissive (non-copyleft) licenses.
This list covers the `runtime`, `dev`, `server`, and `nvidia` dependency sets
from `pyproject.toml` combined, at the versions pinned there (the set is
version-frozen to the contest submission through 2026-09-06). Regenerate with
`pip-licenses` from an environment built via
`pip install -e ".[dev,server,nvidia]"` after the freeze lifts.

| Package | License |
|---|---|
| annotated-doc | MIT |
| annotated-types | MIT |
| anyio | MIT |
| certifi | MPL-2.0 (file-level copyleft; does not affect this project's license) |
| cffi | MIT-0 |
| charset-normalizer | MIT |
| click | BSD-3-Clause |
| colorama | BSD-3-Clause |
| cryptography | Apache-2.0 OR BSD-3-Clause |
| exceptiongroup | MIT |
| fastapi | MIT |
| filelock | MIT |
| h11 | MIT |
| httpcore2 | BSD-3-Clause (republished `httpcore`) |
| httpx2 | BSD-3-Clause (republished `httpx`) |
| idna | BSD-3-Clause |
| iniconfig | MIT |
| joblib | BSD-3-Clause |
| markdown-it-py | MIT |
| mdurl | MIT |
| narwhals | MIT |
| numpy | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| nvidia-ml-py | BSD |
| packaging | Apache-2.0 OR BSD-2-Clause |
| pluggy | MIT |
| prompt_toolkit | BSD-3-Clause |
| psutil | BSD-3-Clause |
| pycparser | BSD-3-Clause |
| pydantic | MIT |
| pydantic-core | MIT |
| Pygments | BSD-2-Clause |
| pytest | MIT |
| questionary | MIT |
| requests | Apache-2.0 |
| rich | MIT |
| scikit-learn | BSD-3-Clause |
| scipy | BSD-3-Clause |
| shellingham | ISC |
| starlette | BSD-3-Clause |
| threadpoolctl | BSD-3-Clause |
| tomli | MIT |
| truststore | MIT |
| typer | MIT |
| typing_extensions | PSF-2.0 |
| typing-inspection | MIT |
| urllib3 | MIT |
| uvicorn | BSD-3-Clause |
| wcwidth | MIT |

`exceptiongroup` and `tomli` install only on Python < 3.11; `colorama` is
pulled unconditionally by the frozen pin set (harmless on non-Windows).

No dependency in this list carries a copyleft license (GPL/LGPL/AGPL) that
would impose obligations on `omm`'s own MIT license.
