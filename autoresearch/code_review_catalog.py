"""Agent-facing code review citations expand to the canonical, frozen host contract."""
import copy
from . import host_execution_artifacts as host_artifacts
from .scoped_file_reference import ChoiceNeeded, document_path, matches, select

PROTOCOL = 'code_review_reference_protocol'
CATALOG = 'code_review_reference_catalog'
VERSION = 2
HEADERS = ('proposal_hash','spec_hash','code_sha256','delivery_hash','document_sha256','alignment_binding')
READ_TOOLS = {'documents.read','documents.search','artifacts.read','code.read'}


def _locator_call(call, context):
    return call.get('name') in READ_TOOLS or (call.get('name') == 'documents.list'
        and 'code_review_assignment' in context and bool(call.get('arguments')))


def document_catalog(context):
    docs = {row['document_id']: row for row in context['indexed_documents']}
    return {f'D{i}':docs[key] for i,key in enumerate(sorted(docs),1)}


def catalog(context, *, restricted=False):
    files = {row['path']: row for row in context['delivery_artifacts']}
    docs = document_catalog(context)
    if restricted:
        from .code_whole_review import reading_documents
        keys = {(doc['document_id'], doc['sha256']) for doc in reading_documents(context)}
        docs = {alias: row for alias, row in docs.items() if (row['document_id'], row['sha256']) in keys}
    return (docs,
            {f'F{i}':files[key] for i,key in enumerate(sorted(files),1)})


def labels(context, docs, files, *, architecture_on_demand=False):
    roles = {r['document_id']:r['source_role'] for r in context['implementation_alignment_contract']['primary_sources']}
    view = {'documents':{alias:{'title':d.get('title'), 'kind':d.get('kind'), 'role':roles.get(d['document_id'])}
                         for alias,d in docs.items()},
            'files':{alias:f['path'] for alias,f in files.items()},
            'implementation_document':next(a for a,d in docs.items()
                                           if d['document_id']==context['implementation_document']['document_id'])}
    if context.get(PROTOCOL) == VERSION:
        for alias, doc in docs.items():
            view['documents'][alias]['path'] = document_path(doc)
    from .independent_full_review import code_delivery_scope
    if code_delivery_scope(context):
        from .code_whole_review import reading_documents
        required = reading_documents(context, required=architecture_on_demand)
        for alias, doc in docs.items():
            view['documents'][alias]['required'] = doc in required
    return view


def replace_fields(schema, remove, add):
    for field in remove:
        schema['properties'].pop(field, None)
    schema['properties'].update(add)
    schema['required'] = [f for f in schema['required'] if f not in remove] + list(add)


def locator_fields(schema, remove, name, sha_name, *, optional=False):
    replace_fields(schema, remove, {})
    for field in (name, 'path', sha_name):
        schema['properties'][field] = {'type':'string','maxLength':1600}
    if not optional:
        schema.setdefault('allOf', []).append({'anyOf':[{'required':[k]} for k in (name,'path',sha_name)]})


def read_schema(name, schema):
    if name == 'documents.list':
        locator_fields(schema['oneOf'][1], ('document_id', 'expected_sha256'), 'document_id', 'expected_sha256')
        return schema
    key = 'document_id' if name.startswith('documents.') else 'id'
    locator_fields(schema, (key,'expected_sha256'), key,'expected_sha256')
    return schema


def directory_for(context, docs, files, design, *, response_policy=1, architecture_on_demand=False):
    from .implementation_alignment import _sources
    directory = labels(context, docs, files, architecture_on_demand=architecture_on_demand)
    allowed = _sources(context, design)
    source_ids = [a for a,d in docs.items() if d['document_id'] in allowed]
    from .code_repair_clarification import enabled as clarification_enabled
    clarification = clarification_enabled(context)
    from pathlib import PurePosixPath
    from .review_response_policy import TEXT_SUFFIXES
    code_ids = [a for a,f in files.items() if f['path'].lower().endswith(('.py','.pyi'))
                or clarification and f['path']=='implementation.json']
    if context[PROTOCOL] == VERSION:
        directory.update(version=VERSION,source_documents=source_ids,code_files=code_ids)
        if response_policy >= 2:
            directory['response_policy_version'] = response_policy
            directory['supporting_files'] = [a for a,f in files.items() if a not in code_ids
                and PurePosixPath(f['path']).suffix.lower() in TEXT_SUFFIXES]
    return directory,source_ids,code_ids


def project(prepared, context, *, design=None):
    """Leave tool branches and all fields containing the reviewer's judgments intact."""
    if context.get(PROTOCOL) not in (1, VERSION):
        return prepared
    from .review_response_policy import acceptance_schema, FIELD as POLICY, VERSION as POLICY_VERSION
    schema = acceptance_schema(prepared['schema'])
    branches = [b for b in schema.get('anyOf',[schema]) if 'code_evidence' in b.get('properties',{})
                and 'alignment_findings' in b['properties'] and 'alignment_binding' in b['properties']]
    if not branches:
        return prepared
    from .code_whole_review import RESTRICTED_VIEW, RESTRICTED_VIEW_VERSION, restricted_view
    restricted = restricted_view(prepared['context'])
    docs, files = catalog(context, restricted=restricted)
    directory,source_ids,code_ids = directory_for(context,docs,files,design,response_policy=POLICY_VERSION,
        architecture_on_demand=prepared['context'].get(RESTRICTED_VIEW) == RESTRICTED_VIEW_VERSION)
    for branch in branches:
        replace_fields(branch, HEADERS, {})
        finding = branch['properties']['alignment_findings']['items']
        src = finding['properties']['source_refs']['items']
        item = branch['properties']['code_evidence']['items']
        impl = item['properties']['implementation_refs']['items']
        if context[PROTOCOL] == VERSION:
            locator_fields(src, ('document_id','document_sha256'), 'document','document_sha256')
            locator_fields(item, ('artifact_id','sha256'), 'file','sha256')
            locator_fields(impl, ('document_id','document_sha256'), 'document','document_sha256',optional=True)
            interface = branch['properties'].get('interface_findings', {}).get('items', {}).get('properties', {})
            if 'implementation_refs' in interface:
                interface_ref = interface['implementation_refs']['items']
                locator_fields(interface_ref, ('document_id', 'document_sha256'), 'document', 'document_sha256', optional=True)
                interface_ref['properties']['document_id'] = {'type': 'string', 'minLength': 1}
            for ref, key in ((src,'document_id'),(item,'artifact_id'),(impl,'document_id')):
                ref['properties'][key] = {'type':'string','minLength':1}
                if ref is not impl:
                    ref['allOf'][-1]['anyOf'].append({'required':[key]})
            # Older ballots may echo the original host fields. Accept exact echoes only.
            canonical = next(b for b in prepared['schema'].get('anyOf',[prepared['schema']])
                             if 'alignment_binding' in b.get('properties',{}))
            for field in HEADERS:
                if field in canonical['properties']:
                    expected = context[field] if field != 'alignment_binding' else {
                        k:context['implementation_alignment_contract'][k] for k in canonical['properties'][field]['required']}
                    branch['properties'][field] = {'const':expected}
        else:
            replace_fields(src, ('document_id','document_sha256'), {'document':{'enum':source_ids}})
            replace_fields(item, ('artifact_id','sha256'), {'file':{'enum':code_ids}} if code_ids
                           else {'file':{'type':'string','not':{}}})
            replace_fields(impl, ('document_id','document_sha256'), {})
    host_catalog = {} if restricted else host_artifacts.catalog(context)
    return {**prepared, 'schema':schema,
        'context':{**prepared['context'], CATALOG:directory, POLICY:POLICY_VERSION,
                   **({host_artifacts.FIELD:host_catalog} if host_catalog else {})},
        'task':prepared['task'] + '\n本次最终代码评审按本请求的输出 Schema 填写。冻结版本由宿主绑定。'
        'source_refs 用 code_review_reference_catalog.documents 的文档编号，code_evidence 用 files 的文件编号。'
        'implementation_refs 只引用实际阅读的 section_id、offset、limit。保留独立结论、理由与证据。'
        + (' 当前分工修订与原实现文档均须引用。用目录中的文档编号区分两个版本。'
           if context.get('scope_revision_document') else ' 默认指向本次实现文档。')
        + (' 读文件、读或检索文档也可直接用 path 填写相对路径和文件名，或单独传 expected_sha256。'
           '路径、编号或内容哈希中有一项能唯一定位即可。'
           if context[PROTOCOL] == VERSION else '')
        + (' 宿主执行日志和报告单列于 host_execution_artifacts，使用其 H 编号、完整路径或哈希读取；'
           '它们不是交付代码，不要从 F 文件中另选替代。' if host_catalog else '')}


def expand(raw, context, canonical_schema, prepared, *, choices=None, design=None, host_references=True):
    from .engine import ResearchBlocked
    from .prose_schema import validator
    from .code_whole_review import RESTRICTED_VIEW, RESTRICTED_VIEW_VERSION, restricted_view
    restricted = restricted_view(prepared['context'])
    docs, files = catalog(context, restricted=restricted)
    version = context.get(PROTOCOL)
    from .review_response_policy import FIELD as POLICY
    response_policy = prepared['context'].get(POLICY, 1)
    directory,_,_ = directory_for(context,docs,files,design,response_policy=response_policy,
        architecture_on_demand=prepared['context'].get(RESTRICTED_VIEW) == RESTRICTED_VIEW_VERSION)
    if prepared['context'].get(CATALOG) != directory or not validator(prepared['schema']).is_valid(raw):
        raise ResearchBlocked('代码评审短引用与本次冻结目录或输出格式不一致。')
    result = copy.deepcopy(raw)
    current = context['implementation_document']
    choices = choices or {}
    if 'tool_calls' in result:
        for i,call in enumerate(result['tool_calls']):
            if not _locator_call(call, context):
                continue
            name = call['name']
            args = call['arguments']
            document = name.startswith('documents.')
            entries = docs if document else files
            key = 'document_id' if document else 'id'
            if name == 'artifacts.read' and host_references and not restricted:
                # A paid response keeps the exact host catalogue it received.
                # Execution may resolve a newly supported bound path without
                # rewriting its original/canonical paid arguments.
                bound = host_artifacts.resolve(context,args,entries=prepared['context'].get(host_artifacts.FIELD,{}))
                if bound:
                    args.pop('path',None)
                    args.update(id=bound['artifact_id'],expected_sha256=bound['sha256'])
                    continue
                if (host_artifacts.external_locator(args) or
                        (set(args).isdisjoint({'id','path'}) and args.get('expected_sha256')
                         and not matches(args['expected_sha256'],files,document=False))):
                    args['id'] = args.pop('path',args.get('id',args.get('expected_sha256')))
                    continue  # The scoped reader returns an error; no unrelated delivery-file vote.
            # artifacts.read also supports archives outside the delivery directory.
            # Keep explicit archive identities on their usual authenticated route.
            if (name=='artifacts.read' and ':' in str(args.get('id','')) and 'path' not in args
                    and not matches(args['id'],entries,document=False)
                    and not matches(args.get('expected_sha256'),entries,document=False)):
                continue
            selectors = {a:b for a,b in (('document' if document else 'file',args.get(key)),
                ('path',args.get('path')),('document_sha256' if document else 'sha256',args.get('expected_sha256')))
                if b is not None}
            location = f'tools/{i}'
            chosen = select(selectors,entries,document=document,location=location,selection=choices.get(location))
            args.pop('path',None)
            args.update({key:chosen['document_id' if document else 'artifact_id'], 'expected_sha256':chosen['sha256']})
        from .code_review_reread import strip as strip_read_intents
        return strip_read_intents(result, prepared)
    header = context['implementation_alignment_contract']
    for field in HEADERS:
        if field not in canonical_schema['properties']:
            continue
        if field == 'alignment_binding':
            result[field] = {k:header[k] for k in canonical_schema['properties'][field]['required']}
        else:
            result[field] = context[field]
    def resolve(ref, entries, document, location, default=None):
        entry = select(ref,entries,document=document,location=location,default=default,selection=choices.get(location))
        for field in (('document','document_sha256','path') if document else ('file','sha256','path')):
            ref.pop(field,None)
        return entry
    for i,finding in enumerate(result['alignment_findings']):
        for j,ref in enumerate(finding['source_refs']):
            doc = (resolve(ref,{a:docs[a] for a in directory['source_documents']},True,f'findings/{i}/source/{j}')
                   if version==VERSION else docs[ref.pop('document')])
            ref.update(document_id=doc['document_id'], document_sha256=doc['sha256'])
    def implementation_refs(item, location):
        alias = directory['implementation_document']
        eligible = {alias: current}
        supplement = context.get('scope_revision_document')
        if supplement:
            eligible.update({key: doc for key, doc in docs.items() if doc['document_id'] == supplement['document_id']})
        for j, ref in enumerate(item['implementation_refs']):
            selected = resolve(ref, eligible, True, f'{location}/implementation/{j}', default=alias) if version == VERSION else current
            ref.update(document_id=selected['document_id'], document_sha256=selected['sha256'])

    for i,item in enumerate(result['code_evidence']):
        if version==VERSION:
            locations = {row['id']:{s.split(':',1)[0] for s in row.get('locations',[])}
                         for row in context.get('implementation_alignment_manifest_requirements',[])}
            allowed_paths = locations.get(item['requirement_id'])
            from .code_repair_clarification import enabled as clarification_enabled
            if response_policy >= 2 or clarification_enabled(context):
                allowed_paths = None
            eligible = {a:files[a] for a in [*directory['code_files'], *directory.get('supporting_files', [])]
                        if allowed_paths is None or files[a]['path'] in allowed_paths}
            file = resolve(item,eligible,False,f'code/{i}/file')
        else:
            file = files[item.pop('file')]
        item.update(artifact_id=file['artifact_id'], sha256=file['sha256'])
        implementation_refs(item, f'code/{i}')
    if version == VERSION and context.get('scope_revision_document'):
        for i, item in enumerate(result.get('interface_findings', [])):
            if 'implementation_refs' in item:
                implementation_refs(item, f'interfaces/{i}')
    from .review_response_policy import acceptance_schema
    from .review_inheritance import ACK
    canonical = {key: value for key, value in result.items() if key != ACK}
    if not validator(acceptance_schema(canonical_schema)).is_valid(canonical):
        raise ResearchBlocked('展开后的代码评审未通过原始交接格式校验。')
    return result


def original_item(raw, location):
    kind,index,*_ = location.split('/')
    if kind=='supplement':
        return raw[index][int(_[0])]
    if kind=='tools':
        return raw['tool_calls'][int(index)]
    return raw['alignment_findings' if kind=='findings' else 'code_evidence'][int(index)]
