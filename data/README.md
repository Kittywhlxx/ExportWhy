# Data

Three sources, virtualised by Zetaris into one schema (`depriskr.*`). SAMPLE_MODE serves the same
schema from `data/sample/depriskr_sample.sqlite` so the agent runs with no network and no Zetaris.

| Source | Kind | Where | License / terms | What we keep |
|---|---|---|---|---|
| Internal inventory | **synthetic** Postgres (`infra/orgdb`) | `data/sample/*.csv` (seed) | made up for this demo; no real people, systems or secrets | 4 services, 13 pins, 8 license rules, 3 exceptions, 4 adoption requests |
| PyPI JSON API | public REST, per release `https://pypi.org/pypi/<pkg>/<ver>/json` | `data/sample/raw/pypi_version_*.json` | PyPI terms of use; metadata is as published by each project | `info` subset (no long description, no file list) + `vulnerabilities` (OSV-fed) |
| deps.dev v3 API | public REST (Google), version / package / project endpoints | `data/sample/raw/depsdev_*.json` | CC-BY 4.0 (deps.dev data) | full responses; `versions` lists are large for old projects |

Packages covered: cryptography 3.3.2, httpx 0.23.0, pillow 9.0.0 / 13.0.0, pyjwt 1.7.1 / 2.15.1,
python-jose 3.3.0 / 3.5.0, pyyaml 5.3.1, requests 2.31.0 / 2.34.2, urllib3 1.26.5. These are real
public releases; the pins and requests that reference them are synthetic.

## Regenerate

```bash
# cached API responses were fetched on 2026-10-23; refetch with (no key needed):
#   curl https://pypi.org/pypi/<pkg>/<ver>/json
#   curl https://api.deps.dev/v3/systems/pypi/packages/<pkg>/versions/<ver>
#   curl https://api.deps.dev/v3/systems/pypi/packages/<pkg>
#   curl https://api.deps.dev/v3/projects/github.com%2F<owner>%2F<repo>
.venv/bin/python data/build_sample.py          # -> data/sample/flat/*.csv + depriskr_sample.sqlite
.venv/bin/python data/zetaris/register.py      # -> registers the three sources in Zetaris (needs ZETARIS_API_KEY)
```

## Limits

- Vulnerability data is a point-in-time copy; the live Zetaris REST tables call the APIs on each query.
- PyPI lists one record per advisory id (GHSA and PYSEC entries for the same CVE both appear); the agent de-duplicates by CVE alias.
- deps.dev `licenses` is a detected SPDX list (`non-standard` when it cannot classify); PyPI `license` is free text. The two disagree on purpose for some packages and the agent reports that.
- Pre-existing components: none. The sample files were fetched during the build window.
