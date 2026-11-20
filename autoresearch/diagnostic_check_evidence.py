"""Read existing, source-bound diagnostic Runner check reports. Never dispatch checks."""
import hashlib
import json
import re
from pathlib import Path
import xml.etree.ElementTree as ET

from .deletion import _plain_path

ROOT = Path(__file__).resolve().parent.parent
FAMILY = 'diagnostic-staging-observations'
TEST = 'tests/test_check_workspace.py'


def _reports(root):
    directory = root / 'outputs' / 'diagnostics'
    for path in sorted(directory.glob(FAMILY + '-*.json')):
        if not re.fullmatch(FAMILY + r'-[0-9a-f]{12}\.json', path.name):
            continue
        _plain_path(path, root)
        raw = path.read_bytes()
        report = json.loads(raw)
        if report.get('provenance') == 'actual_isolated_cpu_checks_not_research_measurements':
            yield path, hashlib.sha256(raw).hexdigest(), report


def archived_module(name, expected, root=None):
    key = 'autoresearch/' + name
    for _, report_sha, report in _reports(root or ROOT):
        if report['source_hashes'].get(key) != expected:
            continue
        source = report.get('archived_sources', {}).get(key)
        if source is not None:
            raw = source.encode('utf-8')
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError('Archived host module differs from its frozen file digest')
            return raw, report_sha


def matching(frozen, *, root=None):
    """Only expose actual cases for the frozen host, with the original regression source identity."""
    if not frozen:
        return []
    root = root or ROOT
    expected = {'autoresearch/' + name: value for name, value in frozen.items()}
    result = []
    for path, checksum, report in _reports(root):
        if report.get('source_unchanged_during_checks') is not True:
            continue
        if any(report['source_hashes'].get(k) != v for k, v in expected.items()):
            continue
        if not any(arg.partition('::')[0] == TEST for arg in report['command']):
            continue
        item = {'report': path.name, 'report_sha256': checksum, 'test_source_sha256': report['source_hashes'][TEST],
                'provenance': report['provenance'], 'exit_code': report['exit_code'],
                'source_unchanged_during_checks': report['source_unchanged_during_checks'], 'cases': []}
        modules = sorted({arg.partition('::')[0] for arg in report['command'] if arg.startswith('tests/')})
        if len(modules) > 1:
            item['test_sources'] = {name: report['source_hashes'][name] for name in modules}
        for suffix, field in (('.log', 'log'), ('.xml', 'junit')):
            if not report.get(field + '_path'):
                continue
            original = path.with_suffix(suffix)
            _plain_path(original, root)
            if original != Path(report[field + '_path']):
                raise ValueError('Diagnostic check output must belong to the same report')
            raw = original.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest != report[field + '_sha256']:
                raise ValueError('Diagnostic check output differs from its original digest')
            item[field + '_sha256'] = digest
            if field == 'junit':
                for case in ET.fromstring(raw).iter('testcase'):
                    issue = next((c.tag for c in case if c.tag in ('failure', 'error', 'skipped')), None)
                    item['cases'].append({'name': case.get('name'), 'outcome': issue or 'passed'})
        result.append(item)
    return result


def source_locations(frozen):
    """Index the Runner's documented staging, release and archival entry points in rendered source."""
    from .experiment_files import _host_source
    anchors = {'lab.py': ('class _WindowsJob:', "result['check_input_staging'] = stage_invocation",
                          'if not kernel.QueryInformationJobObject(ctypes.c_void_p(self.handle), 9,', 'job = _WindowsJob(', '(path / ".start").write_text', 'cpu = job.total_cpu_seconds()',
                          'job.close_and_wait()', 'observations.verify(metrics)',
                          "result['received_cpu_observations'] = observations.values"),
               'check_workspace.py': ('def stage_invocation(',),
               'pathlib_check_runtime.py': ('def prepare_delivery_invocation(',),
               'diagnostic_observations.py': ('class Capture:',)}
    pages = []
    for path, markers in anchors.items():
        if path not in frozen:
            continue
        text, checksum = _host_source(path, frozen[path])
        for marker in markers:
            for match in re.finditer(re.escape(marker), text):
                start = text.rfind(chr(10), 0, match.start()) + 1
                excerpt = text[start:start + 650]
                pages.append({'symbol_or_call':marker, 'line':text.count(chr(10), 0, start) + 1,
                    'excerpt':excerpt,
                    'read_call': {'name':'experiments.files', 'arguments':{'area':'host', 'path':path,
                        'offset':start, 'expected_sha256':checksum, 'limit':2400}}})
    return pages
