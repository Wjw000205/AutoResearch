"""One structural contract for new manifests; legacy bytes are not upgraded."""
import copy

VERSION = 1
FIELD = 'delivery_manifest_contract_version'
SHA = {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}
TEXT = {'type': 'string', 'minLength': 1, 'pattern': r'\S'}
PATH = {'type': 'string', 'minLength': 1, 'pattern': r'^[^\u0000\\:]+$'}


def schema(*, layout_version=2, document_sha256=None):
    if type(layout_version) is not int or layout_version not in (1, 2):
        raise ValueError('Unsupported directory layout contract')
    if document_sha256 is not None:
        from jsonschema import validate
        validate(document_sha256, SHA)
    files = {'type': 'array', 'uniqueItems': True, 'items': PATH}
    properties = {FIELD: {'const': VERSION}, 'code_layout': {'const': 'modular'},
        'directory_layout_version': {'type': 'integer', 'const': layout_version},
        'runtime_files': {**files, 'minItems': 1}, 'delivery_files': {**files, 'minItems': 1},
        'test_result_files': files, 'explanation': TEXT,
        'dependencies': {'type': 'array', 'maxItems': 40, 'items': {'type': 'string',
            'pattern': r'^[A-Za-z0-9_.-]+(?:[<>=!~]+[A-Za-z0-9.*+_-]+)?$'}},
        'requirements': {'type': 'array', 'minItems': 1, 'items': {
            'type': 'object', 'required': ['id', 'locations', 'verification'],
            'properties': {'id': TEXT, 'locations': {'type': 'array', 'minItems': 1, 'items': TEXT},
                           'verification': TEXT}, 'additionalProperties': True}}}
    if document_sha256 is not None:
        properties['implementation_document_sha256'] = {**SHA, 'const': document_sha256}
    return copy.deepcopy({'type': 'object', 'required': list(properties), 'properties': properties,
                          'additionalProperties': True})


def validate(manifest, *, expected=None):
    """Semantic file/read/resource checks remain with their existing consumers."""
    version = manifest.get(FIELD)
    if expected is None and FIELD not in manifest:
        return  # Explicit legacy adapter: no invented fields or document SHA.
    if version != VERSION or type(version) is not int:
        raise ValueError('Missing or unsupported delivery manifest contract')
    from jsonschema import validate as validate_schema, ValidationError
    try:
        validate_schema(manifest, expected or schema(layout_version=manifest.get('directory_layout_version')))
    except ValidationError as exc:
        raise ValueError('Invalid delivery manifest structure at ' + '/'.join(map(str, exc.absolute_path))) from exc


def instruction(document_sha256):
    import json
    return ('\n新交付 implementation.json 的结构合同如下；implementation_document_sha256 必须等于宿主冻结批准文档。'
        '文件存在性、内容、路径安全、完整需求映射、资源及真实检查另行核验，结构通过不是批准。\n'
        + json.dumps(schema(document_sha256=document_sha256), ensure_ascii=False))
