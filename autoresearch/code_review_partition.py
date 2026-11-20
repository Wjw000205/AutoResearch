"""Host-owned assignment of a frozen code delivery to independent reviewers."""
from __future__ import annotations

import ast
import copy
import json
import re
from collections import defaultdict
from pathlib import PurePosixPath

from .code_repair_contract import sha

FIELD = 'code_review_assignment'
COLLECTION = 'code_review_batches'
VERSION = 1
BINDINGS = ('proposal_hash', 'spec_hash', 'code_sha256', 'delivery_hash')
PLAN_FIELD = 'code_review_source_plan'
REVIEW_RULES = {'source_owner': 'one_role_per_file_and_content',
                'auxiliary_owner': 'planned_module_or_unique_requirement_entry_owner',
                'interface_review': 'each_endpoint_owner_reviews_its_side',
                'approval': 'all_roles_and_complete_delivery_coverage',
                'rejection': 'file_location_change_and_verification'}
ARCHITECTURE_REVIEW_RULES = {**REVIEW_RULES, 'auxiliary_owner': 'planned_module_or_unique_planned_caller_role'}
TOOLS = {'artifacts.list', 'artifacts.read', 'code.locate', 'code.read', 'code.record_notes',
         'documents.list', 'documents.read', 'documents.search', 'documents.recall', 'materials.request_read'}
GUIDANCE = (
    '\n本次由宿主按冻结交付分派互斥源码。只核对 code_review_assignment 中的本角色文件及适用验收项。'
    '优先按验收项读取对应实现、测试与批准条款。同一范围的读证可支持多项义务，引用已有回执。'
    '跨组依赖双方各核对自己负责的一端，并提交各自意见。归口角色负责该验收项的总体意见；'
    '不能确认端点、影响或兼容性时在 issues/fixes 明确报告，保持否决。'
    '已记录的宿主检查只支持它实际覆盖的行为。不要用检查通过或目录存在推定未验证的接口。'
    'interface_findings.id 使用每条接口的 id。offset/limit 引用自己实际读到的端点范围。'
    '全局核对依据是共享清单、验收项归口、端点和本角色证据。对不完整的入口到输出调用链、跨组协议冲突或缺少验证，保留否决。'
    '每条否决的 fixes.location 使用本角色文件路径:行号或路径:符号，给出应改行为及验收办法。'
    '覆盖本角色 scope 的全部可读源码。'
)


def _module(path):
    name = path.removesuffix('.py').removesuffix('.pyi').replace('/', '.')
    return name.removesuffix('.__init__')


def source_plan(workflow_id, index, specification, roles, *, architecture_scope=None):
    """Freeze the intended source boundaries before creating a writer request."""
    from .delivery_manifest import _relative
    if architecture_scope and 'source_mapping_protocol' in architecture_scope:
        version = architecture_scope['source_mapping_protocol']
        if type(version) is not int or version != 1:
            raise ValueError('Unsupported architecture source mapping protocol')

    spec = specification['spec']
    checks = {row['id'] for row in spec['acceptance_checks']}
    planned = defaultdict(set)
    shared = {'implementation.json'}
    hints = defaultdict(set)
    declared_edges = {}
    for row in spec['implementation_map']:
        for entry in re.split('[；;]', row['location']):
            location = re.split(r':|\s', entry.strip(), maxsplit=1)[0]
            _relative(location)
            if not PurePosixPath(location).suffix or row['requirement_id'] not in checks:
                raise ValueError('实现规范缺少明确的计划交付路径：' + row['location'])
            if location not in shared:
                planned[row['requirement_id']].add(location)
                if architecture_scope:
                    for consumer, consumer_mark, provider, provider_mark in re.findall(
                            r'\[interface:([^:\s\]]+):([^\s\]]+)\s*->\s*([^:\s\]]+):([^\s\]]+)\]',
                            row.get('behavior', '')):
                        pair = (consumer, provider)
                        markers = {'consumer': consumer_mark, 'provider': provider_mark}
                        if pair in declared_edges and declared_edges[pair] != markers:
                            raise ValueError('跨模块接口标记不一致：' + str(pair))
                        declared_edges[pair] = markers
                    assigned = set(re.findall(r'\[review-role:([^\]]+)\]', row.get('behavior', '')))
                    if 'responsibilities' in architecture_scope and (len(assigned) != 1 or not assigned <= set(roles)):
                        raise ValueError('计划文件必须标明一个已冻结的评审角色：' + location)
                    hints[location].update(assigned.intersection(roles))
    if set(planned) != checks:
        raise ValueError('计划源码范围没有覆盖全部已批准验收项')
    if architecture_scope and architecture_scope.get('source_mapping_protocol') == 1:
        missing = sorted(key for key, paths in planned.items()
                         if not any(path.endswith(('.py', '.pyi')) for path in paths))
        if missing:
            raise ValueError('Python behavior evidence mapping requires author clarification before release: '
                + ', '.join(missing) + '; retain every original duty and map its relevant Python behavior '
                'together with documentation/configuration. Do not add unrelated anchors or waive a requirement.')
    modules = defaultdict(set)
    for path in set().union(*planned.values()):
        parent = PurePosixPath(path).parent.as_posix()
        modules[path if parent in {'.', 'tests', 'test-results'} else parent].add(path)
    groups = []
    loads = dict.fromkeys(roles, 0)
    seen = {role: set() for role in roles}
    target = max(len(paths) for paths in modules.values()) + sum(map(len, modules.values())) // len(roles)
    for paths in sorted(modules.values(), key=lambda paths: (-len(paths), sorted(paths))):
        requirements = {key for key, locations in planned.items() if locations & paths}
        empty = [role for role in roles if not loads[role]]
        available = [role for role in roles if loads[role] + len(paths) <= target]
        candidates = empty or available or roles
        preferred = set().union(*(hints[path] for path in paths))
        if len(preferred) > 1:
            raise ValueError('同一模块的实现位置存在相互冲突的评审角色：' + ', '.join(sorted(paths)))
        role = next(iter(preferred)) if preferred else min(
            candidates, key=lambda role: (-len(seen[role] & requirements), loads[role]))
        groups.append({'role': role, 'paths': sorted(paths), 'requirements': sorted(requirements)})
        loads[role] += len(paths)
        seen[role].update(requirements)
    leads = {key: max(roles, key=lambda role: sum(
        sum(path.endswith(('.py', '.pyi')) for path in paths.intersection(group['paths']))
        for group in groups if group['role'] == role))
        for key, paths in planned.items()}
    all_paths = set().union(*planned.values())
    if any(not set(pair) <= all_paths for pair in declared_edges):
        raise ValueError('显式跨模块接口端点必须属于计划交付路径')
    body = {'version': VERSION, 'workflow_id': workflow_id, 'attempt': index, 'roles': list(roles),
            'spec_hash': specification['spec_hash'], 'groups': groups, 'shared_paths': sorted(shared),
            'requirement_leads': leads,
            'declared_interfaces': [{'consumer': pair[0], 'provider': pair[1], **{
                f'{side}_marker': marker for side, marker in marks.items()}}
                for pair, marks in sorted(declared_edges.items())],
            'review_rules': ARCHITECTURE_REVIEW_RULES,
            'requirements': {key: sorted(paths) for key, paths in sorted(planned.items())}}
    if architecture_scope:
        body['effective_implementation_map'] = copy.deepcopy(spec['implementation_map'])
    return {'body': body, 'sha256': sha(body)}


def writer_instruction(plan):
    auxiliary = ('新增辅助模块沿冻结的调用方归属；遇到另一个已冻结的角色边界后，其下游归该角色。'
                 '多个角色调用的新增模块需要明确归属和跨组接口。'
                 if plan['body'].get('review_rules') == ARCHITECTURE_REVIEW_RULES
                 else '计划入口委托给新增模块时，该模块须保留该验收归口；')
    return ('\n宿主已冻结本次写码范围，按下列组实现。按计划位置组织模块，保持组内职责和紧耦合调用链。'
            '新增辅助模块或测试应在 implementation.json 的交付文件和对应验收项 locations 中登记，'
            + auxiliary + '显式接口端点保留冻结的源码标记。'
            '不得跨组复制同一实现。实际验收位置必须包含计划路径或能从计划入口验证到该位置的源码调用链。'
            '共享接口明确声明输入输出、错误和恢复约束。遇到范围冲突应报告具体验收项和路径，'
            '由 Harness 核对后重新冻结，不自行更换批准义务。\n'
            + ('保留上次已冻结的接口义务。若需移除接口或变更端点，请报告需要重审的原接口。\n'
               if plan['body'].get('previous_review_interfaces') else '')
            + json.dumps(plan, ensure_ascii=False, sort_keys=True))


def dependencies(texts, *, with_lines=False):
    """Find direct static imports within the delivery, preserving both endpoints."""
    modules = defaultdict(set)
    initializers = defaultdict(set)
    for path in texts:
        name = _module(path)
        modules[name].add(path)
        aliases = [name]
        if name.startswith('src.'):
            modules[name[4:]].add(path)
            aliases.append(name[4:])
        if PurePosixPath(path).stem == '__init__' and ast.parse(texts[path], filename=path).body:
            for alias in aliases:
                initializers[alias].add(path)
    edges = defaultdict(set)
    for path, text in texts.items():
        package = _module(path)
        if PurePosixPath(path).stem != '__init__':
            package = package.rpartition('.')[0]
        tree = ast.parse(text, filename=path)
        attributes = defaultdict(set)
        for attribute in ast.walk(tree):
            if isinstance(attribute, ast.Attribute):
                receiver, names = attribute, []
                while isinstance(receiver, ast.Attribute):
                    names.append(receiver.attr)
                    receiver = receiver.value
                if isinstance(receiver, ast.Name):
                    chain = [receiver.id, *reversed(names)]
                    attributes['.'.join(chain[:-1])].add(chain[-1])
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
                symbols = {alias.name: attributes[alias.asname or alias.name] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    parts = package.split('.') if package else []
                    base = parts[:len(parts) - node.level + 1] if node.level <= len(parts) else []
                else:
                    base = []
                prefix = '.'.join([*base, *([node.module] if node.module else [])])
                children = {a.name: f'{prefix}.{a.name}'.lstrip('.') for a in node.names}
                names = [name for name in children.values() if name in modules]
                symbols = {name: set() for name in names}
                exported = {a.name for a in node.names if children[a.name] not in modules}
                if exported:
                    names.append(prefix)
                    symbols[prefix] = exported - {'*'}
                for alias in node.names:
                    child = children[alias.name]
                    if child in symbols:
                        symbols[child] = attributes[alias.asname or alias.name]
            else:
                continue
            for name in names:
                for target in modules.get(name, ()):
                    if target != path:
                        edges[(path, target)].add((node.lineno, tuple(sorted(symbols[name]))))
                parts = name.split('.')
                for length in range(1, len(parts)):
                    for target in initializers.get('.'.join(parts[:length]), ()):
                        if target != path:
                            edges[(path, target)].add((node.lineno, ('__package_initialization__',)))
    return edges if with_lines else sorted(edges)


def endpoint_ranges(text, lines):
    chunks = text.splitlines(keepends=True)
    starts, cursor = [], 0
    for chunk in chunks:
        starts.append(cursor)
        cursor += len(chunk)
    return [[starts[line - 1], starts[line - 1] + len(chunks[line - 1])]
            for line in sorted(lines) if 0 < line <= len(chunks)]


def compile_scopes(files, requirements, texts, roles, *, plan=None, previous_owners=None, supporting=None):
    """Place whole files together using authenticated imports and requirement locations."""
    by_path = {row['path']: row for row in files}
    if len(by_path) != len(files) or len({row['artifact_id'] for row in files}) != len(files):
        raise ValueError('交付目录中存在重复源码身份')
    if 'implementation.json' not in by_path:
        raise ValueError('分派缺少冻结实现清单')
    members = defaultdict(list)
    shared = by_path['implementation.json']['sha256']
    for path in sorted(set(by_path) - {'implementation.json'}):
        if by_path[path]['sha256'] == shared:
            raise ValueError('实现清单的字节同时被声明为独占源码：' + path)
        members[by_path[path]['sha256']].append(path)
    if not members:
        raise ValueError('分派缺少冻结源码')
    groups = {path: identity for identity, paths in members.items() for path in paths}
    locations = {}
    for row in requirements:
        paths = {entry.split(':', 1)[0] for entry in row['locations']} - {'implementation.json'}
        if row['id'] in locations or not paths <= set(groups) or not paths.intersection(texts):
            raise ValueError('验收项缺少唯一且有效的冻结源码位置：' + row['id'])
        locations[row['id']] = paths

    import_lines = dependencies(texts, with_lines=True)
    declared = {(row['consumer'], row['provider']): row
                for row in (plan or {}).get('body', {}).get('declared_interfaces', [])}
    edges = sorted(set(import_lines) | set(declared))
    for pair in (plan or {}).get('body', {}).get('previous_review_interfaces', []):
        if tuple(pair) not in edges:
            raise ValueError('修复交付无法定位原跨组接口，需要重新审核范围：' + ' -> '.join(pair))
    related = defaultdict(set)
    preferred_roles = defaultdict(set)
    for paths in locations.values():
        identities = {groups[path] for path in paths}
        for identity in identities:
            related[identity].update(identities - {identity})
    for source, target in edges:
        left, right = groups[source], groups[target]
        if left != right:
            related[left].add(right)
            related[right].add(left)
    weights = {identity: max(1, by_path[paths[0]]['bytes']) for identity, paths in members.items()}
    loads = dict.fromkeys(roles, 0)
    owners = {}
    if plan is not None:
        if plan['body'].get('roles', list(roles)) != list(roles):
            raise ValueError('实际评审角色与写码前冻结的规则不一致')
        for group in plan['body']['groups']:
            role = group['role']
            for path in group['paths']:
                identity = groups[path]
                if identity in owners and owners[identity] != role:
                    raise ValueError('跨职责组重复交付同一源码：' + path)
                if identity not in owners:
                    owners[identity] = role
                    loads[role] += weights[identity]
        for group in plan['body']['groups']:
            for requirement in group['requirements']:
                for path in locations[requirement]:
                    preferred_roles[groups[path]].add(group['role'])
    for path, role in sorted((previous_owners or {}).items()):
        if path in groups:
            identity = groups[path]
            if identity in owners and owners[identity] != role:
                raise ValueError('修复交付改变了已有源码的评审归属：' + path)
            if identity not in owners:
                owners[identity] = role
                loads[role] += weights[identity]
    if plan is not None:
        module_roles = defaultdict(set)
        for group in plan['body']['groups']:
            for path in group['paths']:
                directory = PurePosixPath(path).parent.as_posix()
                if directory not in {'.', 'tests', 'test-results'}:
                    module_roles[directory].add(group['role'])
        module_constraints = defaultdict(set)
        for path in groups:
            roles_in_module = module_roles[PurePosixPath(path).parent.as_posix()]
            if len(roles_in_module) == 1:
                module_constraints[groups[path]].update(roles_in_module)
        for identity, constraints in sorted(module_constraints.items()):
            if len(constraints) != 1 or identity in owners and owners[identity] not in constraints:
                raise ValueError('相同源码字节属于多个冻结模块职责：' + ', '.join(members[identity]))
            if identity not in owners:
                role = next(iter(constraints))
                owners[identity] = role
                loads[role] += weights[identity]
    leads = (plan or {}).get('body', {}).get('requirement_leads', {})
    if plan and plan['body'].get('review_rules') == ARCHITECTURE_REVIEW_RULES:
        downstream = defaultdict(set)
        for consumer, provider in edges:
            downstream[consumer].add(provider)
        reachable_roles = defaultdict(set)
        for anchor, identity in groups.items():
            if identity not in owners:
                continue
            role = owners[identity]
            pending = list(downstream[anchor])
            while pending:
                path = pending.pop()
                child = groups[path]
                if child in owners or role in reachable_roles[path]:
                    continue
                reachable_roles[path].add(role)
                pending.extend(downstream[path])
        by_content = defaultdict(set)
        for path, inherited in reachable_roles.items():
            by_content[groups[path]].update(inherited)
        for identity, inherited in sorted(by_content.items()):
            if len(inherited) != 1:
                raise ValueError('新增辅助模块有多个调用方职责，需要明确归属及跨组接口：' + members[identity][0])
            role = next(iter(inherited))
            owners[identity] = role
            loads[role] += weights[identity]
    elif leads:
        adjacency = defaultdict(set)
        for consumer, provider in edges:
            adjacency[consumer].add(provider)
        required_owners = defaultdict(set)
        all_planned = {path for group in plan['body']['groups'] for path in group['paths']}
        for key, lead in sorted(leads.items()):
            anchors = set(plan['body']['requirements'][key])
            visited, pending = set(), [path for path in anchors if owners.get(groups[path]) == lead]
            while pending:
                path = pending.pop()
                if path in visited:
                    continue
                visited.add(path)
                if owners.get(groups[path], lead) != lead:
                    continue
                pending.extend(adjacency[path] - visited)
            for path in (visited & locations[key] & texts.keys()) - all_planned:
                if groups[path] in owners and owners[groups[path]] != lead:
                    continue
                required_owners[groups[path]].add(lead)
        for identity, required in sorted(required_owners.items()):
            if len(required) != 1:
                raise ValueError('新增辅助模块属于多个验收归口，需要重新审核范围：' + members[identity][0])
            role = next(iter(required))
            if identity in owners and owners[identity] != role:
                raise ValueError('新增辅助模块与冻结归属冲突：' + members[identity][0])
            if identity not in owners:
                owners[identity] = role
                loads[role] += weights[identity]
        for key, lead in sorted(leads.items()):
            candidates = {groups[path] for path in locations[key] if path in texts}
            if lead in {owners.get(identity) for identity in candidates}:
                continue
            reachable, pending = set(), [path for path in plan['body']['requirements'][key]
                                         if owners.get(groups[path]) == lead]
            while pending:
                path = pending.pop()
                if path not in reachable:
                    reachable.add(path)
                    if owners.get(groups[path], lead) == lead:
                        pending.extend(adjacency[path] - reachable)
            eligible = {groups[path] for path in reachable & locations[key] if path in texts}
            free = sorted(eligible - owners.keys())
            if not free:
                raise ValueError('验收项缺少归口角色可验证的源码调用链：' + key)
            identity = min(free, key=lambda value: members[value][0])
            owners[identity] = lead
            loads[lead] += weights[identity]
    target = (sum(weights.values()) + len(roles) - 1) // len(roles)
    order = sorted(set(members) - set(owners),
                   key=lambda item: (-weights[item], -len(related[item]), members[item][0]))
    for index, identity in enumerate(order):
        empty = [role for role in roles if loads[role] == 0]
        if len(order) - index <= len(empty):
            candidates = empty
        else:
            candidates = [role for role in roles if loads[role] + weights[identity] <= target] or roles
        role = min(candidates, key=lambda role: (
            role not in preferred_roles[identity] if preferred_roles[identity] else False,
            -sum(owners.get(neighbor) == role for neighbor in related[identity]), loads[role], roles.index(role)))
        owners[identity] = role
        loads[role] += weights[identity]
    path_owner = {path: owners[identity] for path, identity in groups.items()}
    checks = {}
    for key, paths in locations.items():
        own_paths = {role: sorted(path for path in paths if path_owner[path] == role) for role in roles}
        # A path backed by Python is a better source of code evidence than a
        # configuration or report cited by the same requirement.
        lead = leads.get(key) or max(roles, key=lambda role: (
            sum(p in texts for p in own_paths[role]), len(own_paths[role])))
        if not any(p in texts for p in own_paths[lead]):
            raise ValueError('归口角色缺少验收项的实际 Python 证据位置：' + key)
        checks[key] = {'lead': lead, 'paths': own_paths}
    interfaces = []
    for consumer, provider in edges:
        marks = declared.get((consumer, provider))
        sources = {**(supporting or {}), **texts}
        if marks:
            lines = {side: [number for number, line in enumerate(sources.get(path, '').splitlines(), 1)
                            if marks[f'{side}_marker'] in line]
                     for side, path in (('consumer', consumer), ('provider', provider))}
            if any(not values for values in lines.values()):
                raise ValueError('交付未实现冻结接口端点：' + consumer + ' -> ' + provider)
        else:
            imports = import_lines[(consumer, provider)]
            symbols = {name for _, names in imports for name in names}
            declared_lines = []
            for node in ast.parse(texts[provider]).body:
                names = set()
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(node.name)
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    names.update(target.id for target in targets if isinstance(target, ast.Name))
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    names.update(alias.asname or alias.name.split('.')[0] for alias in node.names)
                if names & symbols or '__package_initialization__' in symbols:
                    declared_lines.append(node.lineno)
            lines = {'consumer': {line for line, _ in imports}, 'provider': declared_lines}
        if path_owner[consumer] != path_owner[provider]:
            edge = {'consumer': consumer, 'provider': provider, 'consumer_role': path_owner[consumer],
                    'provider_role': path_owner[provider],
                    'consumer_ranges': endpoint_ranges(sources[consumer], lines['consumer']),
                    'provider_ranges': endpoint_ranges(sources[provider], lines['provider'])}
            if marks:
                edge['contract'] = marks
            elif not edge['provider_ranges']:
                edge['provider_unresolved'] = True
            interfaces.append({'id': sha(edge), **edge})
    return path_owner, checks, interfaces


def validate_source_plan(plan, *, workflow_id, index, spec_hash, paths, requirements, edges):
    body = plan['body']
    if (plan['sha256'] != sha(body) or body.get('version') != VERSION
            or (body['workflow_id'], body['attempt'], body['spec_hash']) != (workflow_id, index, spec_hash)):
        raise ValueError('写码范围未绑定当前实现请求和批准规范')
    children = defaultdict(set)
    for source, target in edges:
        children[source].add(target)
    by_id = {row['id']: {item.split(':', 1)[0] for item in row['locations']} for row in requirements}
    if set(by_id) != set(body['requirements']):
        raise ValueError('实际交付与写码范围的验收项不一致')
    for key, anchors in body['requirements'].items():
        for anchor in anchors:
            if anchor not in paths:
                raise ValueError('实际交付缺少计划源码：' + anchor)
            seen, pending = set(), [anchor]
            while pending:
                path = pending.pop()
                if path in seen:
                    continue
                seen.add(path)
                pending.extend(children[path] - seen)
            declared = seen.intersection(by_id[key])
            # Older approved plans list reports and tests alongside runtime
            # anchors. A non-runtime file can support any named obligation,
            # provided its exact file exists and is registered in the manifest.
            supplemental = (anchor.startswith(('tests/', 'test-results/')) or
                            PurePosixPath(anchor).suffix in {'.md', '.rst'})
            if not declared and not supplemental:
                raise ValueError('验收项无法从计划源码追溯到实际实现：' + key + ' / ' + anchor)


def validate_writer_instruction(attempt, workflow_id, spec_hash, *, release=None):
    plan = attempt.get(PLAN_FIELD)
    if release and (not plan or plan['body'].get('document_scope_release_sha256') != release['sha256']
                    or plan['body'].get('effective_implementation_map') != release['effective_implementation_map']
                    or plan['body'].get('scope_revision_document') != (
                        (release.get('mapping_revision') or {}).get('document') or release.get('inherited_scope_document'))):
        raise ValueError('当前实现缺少获批的写码范围或分工修订绑定')
    if plan and (plan['sha256'] != sha(plan['body'])
                 or (plan['body']['workflow_id'], plan['body']['attempt'], plan['body']['spec_hash'])
                 != (workflow_id, attempt['index'], spec_hash)
                 or writer_instruction(plan) not in attempt['instruction']):
        raise ValueError('已冻结的评审规则与 Codex 写码指令不一致')


def check_delivery_plan(plan, workflow_id, index, spec_hash, snapshot):
    from pathlib import Path
    from .delivery_files import io_path

    root = Path(snapshot['workspace'])
    files = snapshot['files']
    paths = {row['path'] for row in files}
    manifest = json.loads(io_path(root / 'implementation.json').read_bytes())
    texts = {path: io_path(root / path).read_bytes().decode('utf-8') for path in paths
             if path.endswith(('.py', '.pyi'))}
    declarations = plan['body'].get('declared_interfaces', [])
    explicit = [(row['consumer'], row['provider']) for row in declarations]
    validate_source_plan(plan, workflow_id=workflow_id, index=index, spec_hash=spec_hash,
                         paths=paths, requirements=manifest['requirements'], edges=[*dependencies(texts), *explicit])
    supporting = {path: io_path(root / path).read_bytes().decode('utf-8')
                  for pair in explicit for path in pair if path not in texts}
    compile_scopes([{**row, 'artifact_id': row['path']} for row in files],
                   manifest['requirements'], texts, plan['body'].get('roles', ('director', 'engineer', 'scout')),
                   plan=plan, supporting=supporting)
    owners = {path: group['role'] for group in plan['body']['groups'] for path in group['paths']}
    seen = {}
    for row in files:
        path = row['path']
        if path in owners and seen.setdefault(row['sha256'], owners[path]) != owners[path]:
            raise ValueError('冻结职责组之间存在重复源码：' + path)


def from_legacy_batch(batch, workflow_id, index, roles):
    owners = batch['owners']
    if set(roles) != set(batch['assignments']):
        raise ValueError('旧交付的评审角色与修订不一致')
    requirements, leads = {}, {}
    for row in batch['requirements']:
        key = row['id']
        requirements[key] = sorted({location.split(':', 1)[0] for location in row['locations']
                                    if location.split(':', 1)[0] in owners})
        applicable = {role: batch['assignments'][role]['context'][FIELD]['checks'].get(key) for role in roles}
        leads[key] = next(item['lead'] for item in applicable.values() if item)
    body = {'version': VERSION, 'workflow_id': workflow_id, 'attempt': index, 'roles': list(roles),
            'spec_hash': batch['bindings']['spec_hash'], 'requirements': requirements,
            'requirement_leads': leads, 'shared_paths': ['implementation.json'],
            'groups': [{'role': role, 'paths': sorted(path for path in owners if owners[path] == role),
                        'requirements': sorted(key for key, paths in requirements.items()
                                               if any(owners[path] == role for path in paths))} for role in roles],
            'declared_interfaces': [], 'legacy_delivery_hash': batch['bindings']['delivery_hash'],
            'review_rules': ARCHITECTURE_REVIEW_RULES}
    return with_previous_interfaces({'body': body, 'sha256': sha(body)}, batch)


def with_previous_interfaces(plan, batch):
    endpoints = {(edge['consumer'], edge['provider'])
                 for assignment in batch['assignments'].values()
                 for edge in assignment['context'][FIELD]['interfaces']}
    body = {**plan['body'], 'previous_review_batch_sha256': sha(batch),
            'previous_review_purpose': batch['purpose'],
            'previous_review_interfaces': [list(pair) for pair in sorted(endpoints)]}
    return {'body': body, 'sha256': sha(body)}


def load(store, run_id, purpose):
    key = f'{run_id}:{purpose}'
    row = store.get(COLLECTION, key)
    if row is None:
        return None
    payload = row['payload']
    if (row.get('id') != key or row.get('run_id') != run_id or payload.get('purpose') != purpose
            or payload.get('version') != VERSION or row.get('sha256') != sha(payload)):
        raise ValueError('代码评审批次的冻结输入发生变化')
    return payload


def clarification_parent(engine, run_id, purpose, source):
    """Authenticate a complete original batch, never reconstruct it from a role view."""
    from .codex_workflow import _frozen_spec_assignment, ROLES
    batch = load(engine.store, run_id, purpose)
    if (not batch or set(batch['assignments']) != set(ROLES)
            or set(source['source_contexts']) != set(ROLES)):
        raise ValueError('Clarification requires the exact original three-role batch')
    for role in ROLES:
        original = source['source_contexts'][role]
        frozen = _frozen_spec_assignment(engine, run_id, purpose, role)
        assignment = batch['assignments'][role]
        if (not frozen or any(frozen[k] != assignment[k] for k in ('context', 'instruction', 'schema'))
                or original != assignment['context'] or original[FIELD]['role'] != role
                or original[FIELD]['version'] != VERSION
                or batch['bindings'] != {k: original[k] for k in BINDINGS}
                or original['spec_hash'] != source['spec_hash']
                or original['delivery_hash'] != source['delivery_hash']):
            raise ValueError('Clarification parent role/source identity changed')
    return batch


def freeze_clarification(engine, run_id, purpose, context, instruction, schema, roles):
    from . import code_repair_clarification as clarification
    request = context[clarification.PARTITION]
    if type(request.get('version')) is not int or request['version'] != 1 or tuple(roles) != ('director', 'engineer', 'scout'):
        raise ValueError('Unsupported clarification partition contract')
    source = request['source']
    parent = clarification_parent(engine, run_id, request['parent_purpose'], source)
    if sha(parent) != request['parent_batch_sha256'] or sha(source) != request['source_sha256']:
        raise ValueError('Clarification parent batch/source drift')
    assignments = {}
    from .code_review_catalog import PROTOCOL
    for role in roles:
        scoped = clarification.context_for(source, request['proposal'], role=role, **request['options'])
        if PROTOCOL in context:
            scoped[PROTOCOL] = context[PROTOCOL]
        scoped[clarification.PARTITION] = {k: request[k] for k in (
            'version', 'parent_purpose', 'parent_batch_sha256', 'source_sha256')}
        scoped[clarification.PARTITION]['role'] = role
        assignments[role] = {'context': scoped, 'instruction': instruction + GUIDANCE,
            'schema': response_schema(schema, scoped[FIELD], supplement=bool(scoped.get('scope_revision_document')))}
    payload = {**copy.deepcopy(parent), 'purpose': purpose, 'assignments': assignments,
               'clarification_request_sha256': sha(request),
               'clarification_parent': {k: request[k] for k in (
                   'version', 'parent_purpose', 'parent_batch_sha256', 'source_sha256')}}
    key = f'{run_id}:{purpose}'
    row = {'id': key, 'run_id': run_id, 'sha256': sha(payload), 'payload': payload}
    def preserve(previous):
        if previous and previous != row:
            raise ValueError('Clarification batch already frozen with different inputs')
        return previous or row
    engine.store.mutate(COLLECTION, key, preserve)
    return payload


def freeze(engine, run_id, purpose, context, instruction, schema, roles):
    from .implementation_alignment_evidence import _code_text

    files = context['delivery_artifacts']
    manifest = next((row for row in files if row['path'] == 'implementation.json'), None)
    if manifest is None:
        raise ValueError('分派缺少冻结实现清单')
    manifest_data = json.loads(_code_text(engine, run_id, manifest['artifact_id'], manifest))
    rows = context['implementation_alignment_manifest_requirements']
    ids = context['implementation_alignment_acceptance_ids']
    if (not isinstance(manifest_data, dict) or manifest_data.get('requirements') != rows
            or not ids or len(ids) != len(set(ids)) or set(ids) != {row['id'] for row in rows}):
        raise ValueError('交付清单与批准验收项不一致')
    declared = manifest_data.get('delivery_files')
    if declared is not None and set(declared) != {row['path'] for row in files}:
        raise ValueError('交付目录与冻结清单不一致')
    texts = {row['path']: _code_text(engine, run_id, row['artifact_id'], row) for row in files
             if row['path'].endswith(('.py', '.pyi'))}
    plan = context.get(PLAN_FIELD)
    if plan is not None and (plan['sha256'] != sha(plan['body'])
                            or plan['body']['spec_hash'] != context['spec_hash']):
        raise ValueError('写码范围与批准规范的冻结身份不一致')
    previous = context.get('previous_partition_purpose')
    previous_batch = load(engine.store, run_id, previous) if previous else None
    if previous and (not previous_batch or previous_batch['bindings']['spec_hash'] != context['spec_hash']):
        raise ValueError('修复复审缺少同规范的原范围')
    previous_sha = (plan or {}).get('body', {}).get('previous_review_batch_sha256')
    if previous_sha and (not previous_batch or sha(previous_batch) != previous_sha):
        raise ValueError('修复复审的原批次与写码前冻结范围不一致')
    endpoints = {item[side] for item in (plan or {}).get('body', {}).get('declared_interfaces', [])
                 for side in ('consumer', 'provider')}
    supporting = {row['path']: _code_text(engine, run_id, row['artifact_id'], row) for row in files
                  if row['path'] in endpoints and row['path'] not in texts}
    from .code_repair_contract import FIELD as REPAIR
    previous_owners = previous_batch['owners'] if previous_batch else {}
    if not previous_batch and context.get('partition_redispatch') and context.get(REPAIR):
        prior_owners = defaultdict(set)
        delivered = {row['path'] for row in files} - {'implementation.json'}
        for issue in context[REPAIR]['issues']:
            for fix in issue['fixes']:
                path = fix['location'].split(':', 1)[0]
                if path in delivered:
                    prior_owners[path].add(issue['role'])
        if any(len(owners) != 1 for owners in prior_owners.values()):
            raise ValueError('遗留修复意见对同一文件的归属冲突，需要先核对分工')
        previous_owners = {path: next(iter(roles_for_path)) for path, roles_for_path in prior_owners.items()}
    owners, checks, interfaces = compile_scopes(files, rows, texts, roles, plan=plan, supporting=supporting,
                                               previous_owners=previous_owners)
    inventory = sha(files)
    assignments = {}
    file_hashes = {row['sha256'] for row in files}
    file_ids = {row['artifact_id'] for row in files}
    documents = [doc for doc in context.get('indexed_documents', [])
                 if doc.get('sha256') not in file_hashes and doc.get('artifact_id') not in file_ids
                 and not file_ids.intersection(doc.get('artifact_ids', []))]
    required_docs = {r['document_id'] for r in context.get('implementation_alignment_contract', {}).get('primary_sources', [])}
    if context.get('implementation_document'):
        required_docs.add(context['implementation_document']['document_id'])
    if not required_docs <= {doc['document_id'] for doc in documents}:
        raise ValueError('批准来源和交付源码使用了同一个文档身份，须确认来源范围')
    for role in roles:
        allowed = [row for row in files if owners.get(row['path']) == role or row['path'] == 'implementation.json']
        own_checks = {key: {'lead': row['lead'], 'paths': row['paths'][role]} for key, row in checks.items()
                      if any(path in texts for path in row['paths'][role])}
        owned_checks = {key for key, row in checks.items() if row['paths'][role]}
        scope = {'version': VERSION, 'role': role, 'inventory_sha256': inventory,
                 'checks': own_checks, 'files': [row['path'] for row in allowed if row['path'] != 'implementation.json'],
                 'interfaces': [row for row in interfaces if role in (row['consumer_role'], row['provider_role'])],
                 'complete_source_reads': True}
        scope['strict_fix_locations'] = True
        scoped = {**context, FIELD: scope, 'delivery_artifacts': allowed, 'indexed_documents': documents}
        from .supplemental_reads import PROTOCOL, VERSION as READ_VERSION, GUIDANCE as READ_GUIDANCE
        scoped[PROTOCOL] = READ_VERSION
        scoped['supplemental_code_sources'] = copy.deepcopy(files)
        if plan and plan['body'].get('effective_implementation_map'):
            scoped['approved_source_scope'] = {
                'plan_sha256': plan['sha256'],
                'scope_release_sha256': plan['body'].get('document_scope_release_sha256'),
                'mapping': [row for row in plan['body']['effective_implementation_map']
                            if row['requirement_id'] in owned_checks]}
        scoped.pop(PLAN_FIELD, None)
        scoped.pop('previous_partition_purpose', None)
        scoped.pop('definition_code_sources', None)
        scoped.pop('delivery_files', None)
        if context.get(REPAIR):
            repair = context[REPAIR]
            own_paths = set(scope['files']) | {'implementation.json'}
            scoped[REPAIR] = {**repair, 'issues': [row for row in repair['issues'] if row['role'] == role],
                'changed_paths': [path for path in repair['changed_paths'] if path in own_paths],
                'previous_artifacts': [row for row in repair['previous_artifacts'] if row['path'] in own_paths],
                'document_memory_handoffs': [row for row in repair['document_memory_handoffs'] if row['role'] == role]}
        # The full manifest mapping is authenticated by the validator; the
        # model receives only the role's locations through the assignment.
        scoped['implementation_alignment_manifest_requirements'] = [
            {**row, 'locations': [location for location in row['locations']
                                  if location.split(':', 1)[0] in scope['files']]}
            for row in rows if row['id'] in own_checks]
        allowed_ids = {row['artifact_id'] for row in allowed}
        implementation = scoped.get('implementation')
        if isinstance(implementation, dict) and implementation.get('artifact_id') not in allowed_ids:
            scoped['implementation'] = {k: v for k, v in implementation.items() if k != 'artifact_id'}
        supplement = (' 当前分工修订属于批准输入；请用 documents.read 阅读 scope_revision_document，'
                      '每个负责的验收项及跨组端点都用 implementation_refs 引用原规范和同一补充版本。'
                      if scoped.get('scope_revision_document') else '')
        assignments[role] = {'context': scoped, 'instruction': instruction + GUIDANCE + supplement + READ_GUIDANCE,
                             'schema': response_schema(schema, scope, supplement=bool(supplement))}
    payload = {'version': VERSION, 'purpose': purpose, 'bindings': {k: context[k] for k in BINDINGS},
               'inventory': files, 'owners': owners, 'requirements': rows, 'assignments': assignments,
               'source_plan_sha256': (plan or {}).get('sha256'),
               'scope_revision_document': context.get('scope_revision_document')}
    key = f'{run_id}:{purpose}'
    row = {'id': key, 'run_id': run_id, 'sha256': sha(payload), 'payload': payload}
    def preserve(previous):
        if previous and previous != row:
            raise ValueError('并发派发产生了不同的代码评审范围')
        return previous or row
    engine.store.mutate(COLLECTION, key, preserve)
    return payload


def response_schema(schema, scope, *, supplement=False):
    result = copy.deepcopy(schema)
    checks = sorted(scope['checks'])
    for field in ('alignment_findings', 'code_evidence'):
        properties = result['properties'][field]['items']['properties']
        properties['requirement_id'] = {'enum': checks} if checks else {'not': {}}
    endpoints = [edge['id'] for edge in scope['interfaces']]
    result['properties']['interface_findings'] = {
        'type': 'array', 'maxItems': len(endpoints), 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['id', 'status', 'evidence', 'offset', 'limit'],
            'properties': {'id': {'enum': endpoints} if endpoints else {'not': {}},
                           'status': {'enum': ['aligned', 'mismatch', 'pending']},
                           'evidence': {'type': 'string', 'minLength': 1},
                           'offset': {'type': 'integer', 'minimum': 0},
                           'limit': {'type': 'integer', 'minimum': 0, 'maximum': 16000}}}}
    result['required'].append('interface_findings')
    if supplement:
        from .implementation_alignment import SOURCE_REFS
        item = result['properties']['interface_findings']['items']
        item['properties']['implementation_refs'] = SOURCE_REFS
        item['required'].append('implementation_refs')
    return result


def validate_ballot(context, role, result):
    scope = context[FIELD]
    if scope['role'] != role:
        raise ValueError('评审意见角色与冻结范围不一致')
    findings = result.get('interface_findings')
    if not isinstance(findings, list):
        raise ValueError('评审意见缺少接口核对结果')
    expected = {edge['id'] for edge in scope['interfaces']}
    ids = [row['id'] for row in findings]
    if len(ids) != len(set(ids)) or not set(ids) <= expected:
        raise ValueError('接口意见越出冻结范围或重复')
    if result['approve'] and (set(ids) != expected or any(row['status'] != 'aligned' for row in findings)):
        raise ValueError('仍有跨组接口证据缺失或冲突')
    if not result['approve'] and scope.get('strict_fix_locations'):
        files = set(scope['files']) | {'implementation.json'}
        for fix in result.get('fixes', []):
            path, separator, location = fix['location'].partition(':')
            if path not in files or not separator or not location.strip():
                raise ValueError('否决位置必须是本角色负责的文件及行号或符号：' + fix['location'])


def validate_batch(batch, roles, votes):
    if len(votes) != len(roles) or {row['role'] for row in votes} != set(roles):
        raise ValueError('代码评审批次缺少独立角色意见')
    if not all(row['approve'] for row in votes):
        return
    sources, requirements, endpoints = [], set(), defaultdict(set)
    for vote in votes:
        role = vote['role']
        context = batch['assignments'][role]['context']
        validate_ballot(context, role, vote)
        scope = context[FIELD]
        sources.extend(scope['files'])
        requirements.update(row['requirement_id'] for row in vote['code_evidence'])
        led = {key for key, assignment in scope['checks'].items() if assignment['lead'] == role}
        if not led <= {row['requirement_id'] for row in vote['code_evidence']}:
            raise ValueError('验收项缺少冻结归口角色的代码证据')
        for finding in vote['interface_findings']:
            endpoints[finding['id']].add(role)
    if (len(sources) != len(set(sources)) or set(sources) != set(batch['owners'])
            or requirements != {row['id'] for row in batch['requirements']}
            or any(len(roles) != 2 for roles in endpoints.values())):
        raise ValueError('源码、验收项或跨组端点未完成三方覆盖')


def authenticate(engine, run_id, role, handoff_id, context):
    from .code_navigation import frozen

    if frozen(engine, run_id, role, handoff_id) != context:
        raise ValueError('源码权限没有绑定实际冻结交接')
    hand = engine.store.get('handoffs', handoff_id)
    packet = load(engine.store, run_id, hand['purpose'])
    if not packet or packet['assignments'].get(role, {}).get('context') != context:
        raise ValueError('源码权限没有绑定宿主冻结的职责分配')
    return packet


def read_page(engine, run_id, role, handoff_id, context, source, text, offset, limit, *, allow_eof=False):
    """Return the first unread contiguous portion and retain exact receipt references."""
    if offset > len(text) or offset == len(text) and text and not allow_eof:
        raise ValueError('Code read range exceeds source')
    end = min(offset + limit, len(text))
    spans = delivered_spans(engine, run_id, role, handoff_id, context, source, text)
    references = []
    for start, stop, identity in spans:
        if stop <= offset or start >= end:
            continue
        if start > offset:
            end = start
            break
        offset = min(end, max(offset, stop))
        references.append(identity)
    return {'artifact_id': source['artifact_id'], 'path': source['path'], 'sha256': source['sha256'],
            'offset': offset, 'limit': end - offset, 'text': text[offset:end], 'total_chars': len(text),
            'next_offset': end if end < len(text) else None, 'truncated': end < len(text),
            'confirmed_receipts': list(dict.fromkeys(references)),
            **({'empty_source': True} if not text else {})}


def delivered_spans(engine, run_id, role, handoff_id, context, source, text):
    from .evidence_delivery import delivery_spans
    from .review_inheritance import consumed_receipts
    from .working_context import sha as receipt_sha

    aliases = {row['artifact_id'] for row in context['delivery_artifacts'] if row['sha256'] == source['sha256']}
    spans = [(row['char_start'], row['char_end'], row['id']) for row in delivery_spans(
        engine.store, run_id, role, source['artifact_id'], source['sha256'], handoff_id=handoff_id,
        text=text, aliases=aliases)]
    consumed = consumed_receipts(engine, run_id, role, handoff_id)
    if not consumed:
        return sorted(spans)
    progress = engine.store.get('handoff_progress', handoff_id, {})
    for row in progress.get('tool_results', []):
        call, page = row.get('call', {}), row.get('result', {})
        if (call.get('name') not in {'code.read', 'artifacts.read'} or not isinstance(page, dict)
                or page.get('error') or row.get('predecessor_handoff_id')
                or receipt_sha(row) not in consumed
                or page.get('artifact_id') not in aliases or page.get('sha256') != source['sha256']):
            continue
        start, body = page.get('offset'), page.get('text')
        if type(start) is int and start >= 0 and isinstance(body, str) and body and text[start:start + len(body)] == body:
            spans.append((start, start + len(body), receipt_sha(row)))
    return sorted(set(spans))
