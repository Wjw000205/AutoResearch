"""Read-only runtime assets and actionable receipts for isolated host tests."""
from pathlib import Path
import xml.etree.ElementTree as ET

from .code_bundle import relative_file

PACKAGE_DATA = ('autoresearch/official_model_prices.json', 'autoresearch/ccf_catalog.json')


def add_package_data(files):
    root = Path(__file__).resolve().parents[1]
    for name in PACKAGE_DATA:
        if name not in files:
            path = root / name
            if path.is_symlink() or path.resolve() != path:
                raise ValueError('Linked package data cannot enter the host check: ' + name)
            files[name] = path.read_bytes()


def test_root(root, hashes, temporary):
    """For pre-migration deliveries, stage the exact candidate plus missing package data."""
    from .bounded_host_repair import checksum
    missing = [name for name in PACKAGE_DATA if name not in hashes]
    if not missing:
        return root, {}
    staged = temporary / 'candidate'
    additions = {}
    add_package_data(additions)
    for name in (*hashes, *missing):
        relative_file(name)
        dest = staged / name
        raw = additions[name] if name in missing else (root / name).read_bytes()
        if name in hashes and checksum(raw) != hashes[name]:
            raise ValueError('Host check input changed: ' + name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
    return staged, {name: checksum(additions[name]) for name in missing}


def failed_cases(checks, catalog):
    """Bind pytest collection errors and assertion failures to their actual test locations."""
    xml = checks.get('junit_xml')
    if not xml:
        return []
    try:
        report = ET.fromstring(xml)
    except ET.ParseError:
        return []
    tests = {path.removesuffix('.py').replace('/', '.'): path for path in checks['selected_tests']}
    index = {(row['path'], row['symbol']): row for row in catalog}
    failures = {}
    for case in report.iter('testcase'):
        error = next((child for child in case if child.tag in ('error', 'failure')), None)
        if error is None:
            continue
        classname, name = case.get('classname', ''), case.get('name', '')
        module = next((m for m in tests if classname == m or classname.startswith(m + '.') or name == m), None)
        if not module:
            return []
        path = tests[module]
        symbol = '.'.join(filter(None, (classname.removeprefix(module).strip('.'), name.split('[')[0])))
        location = index.get((path, symbol)) or index.get((path, '<module>'))
        if not location:
            return []
        message = (error.text or error.get('message') or '').strip()
        failures.setdefault(location['location_id'], []).append(f'{path}::{name}\n{message[-2000:]}')
    return [{'location_id': key, 'problem': '\n'.join(messages),
             'change': '按 JUnit 中的具体异常修复对应实现或测试依赖，保留原断言和故障覆盖。',
             'acceptance': '原失败用例可收集、执行并通过，累计宿主测试通过并留下原始回执。'}
            for key, messages in failures.items()]
