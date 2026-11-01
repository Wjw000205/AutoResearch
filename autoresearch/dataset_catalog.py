"""Explicit public benchmark locations; never infer a paper's split/protocol.

Locations checked against the dataset author's repository and the TSLib
maintainer's linked Hugging Face data repository on 2026-10-18. Revisions are
pinned. Unknown/ambiguous names need source resolution, never a guessed URL.
"""
from __future__ import annotations

import copy


VERSION = 'public-timeseries-2026-10-18'
ETT_REVISION = '1d16c8f4f943005d613b5bc962e9eeb06058cf07'
TSLIB_REVISION = '2b66e59ee19dac8f6f19fb5d4997f289fdfea357'
ETT_REPOSITORY = 'https://github.com/zhouhaoyi/ETDataset'
TSLIB_REPOSITORY = 'https://huggingface.co/datasets/thuml/Time-Series-Library'
TSLIB_REFERENCE = 'https://github.com/thuml/Time-Series-Library#prepare-data'

CATALOG = {}
for name in ('ETTh1', 'ETTh2', 'ETTm1', 'ETTm2'):
    CATALOG[name] = {'key': name, 'filename': name + '.csv',
        'url': f'https://raw.githubusercontent.com/zhouhaoyi/ETDataset/{ETT_REVISION}/ETT-small/{name}.csv',
        'repository': ETT_REPOSITORY, 'revision': ETT_REVISION, 'source_kind': 'dataset_author',
        'references': [ETT_REPOSITORY], 'max_bytes': 32 * 1024**2}
CATALOG['ETTm1'].update(expected_bytes=10360719,
    expected_content_sha256='6ce1759b1a18e3328421d5d75fadcb316c449fcd7cec32820c8dafda71986c9e')

for name, path, size, sha in (
    ('traffic', 'traffic/traffic.csv', 136478119, 'cb06463d56fa17d87f47027cd9389ceae82a69eddee51cdb61480e120dab0b16'),
    ('electricity', 'electricity/electricity.csv', 95581762, '7e45845d54c5219bad0ae6bc1b5316cf8ff9cead5d33fa998a5a51c2e4a497ad'),
    ('weather', 'weather/weather.csv', 7235425, None),
    ('exchange_rate', 'exchange_rate/exchange_rate.csv', 637800, None),
    ('illness', 'illness/national_illness.csv', 67620, None),
):
    CATALOG[name] = {'key': name, 'filename': path.rsplit('/', 1)[-1],
        'url': f'{TSLIB_REPOSITORY}/resolve/{TSLIB_REVISION}/{path}',
        'repository': TSLIB_REPOSITORY, 'revision': TSLIB_REVISION,
        'source_kind': 'benchmark_maintainer', 'references': [TSLIB_REFERENCE, TSLIB_REPOSITORY],
        'expected_bytes': size, 'max_bytes': size + 1024**2,
        **({'expected_content_sha256': sha} if sha else {})}

ALIASES = {key.casefold(): [key] for key in CATALOG}
ALIASES.update({'ett': ['ETTh1', 'ETTh2', 'ETTm1', 'ETTm2'],
    'ett-small': ['ETTh1', 'ETTh2', 'ETTm1', 'ETTm2'],
    'ecl': ['electricity'], 'electricity consumption': ['electricity'],
    'exchange': ['exchange_rate'], 'exchange-rate': ['exchange_rate'], 'exchange rate': ['exchange_rate'],
    'ili': ['illness'], 'national_illness': ['illness'], 'national illness': ['illness']})


def candidates(name):
    if not isinstance(name, str):
        return []
    normalized = name.strip().casefold()
    if normalized.endswith('.csv'):
        normalized = normalized[:-4]
    return [copy.deepcopy(CATALOG[key]) for key in ALIASES.get(normalized, [])]


def resolve(name, key=None):
    matches = candidates(name)
    if key is not None:
        matches = [row for row in matches if row['key'] == key]
    if len(matches) != 1:
        raise ValueError('Dataset name is unknown or ambiguous; select an explicit catalog_key from discover or provide a source-bound URL')
    return matches[0]
