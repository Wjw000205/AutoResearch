"""Lossless source selection for the versioned metric-capability review.

Legacy excerpts keep their original scope and representation. Explicit source
policy 2 selects declarations from a complete frozen module, with its module
binding dependencies. Every omitted byte remains an on-demand fragment.
"""
from __future__ import annotations

import ast
import builtins
import copy
import difflib
import functools
import hashlib
import json
import symtable
from pathlib import PurePosixPath

VERSION = 1
ENGINE_PATH = 'autoresearch/engine.py'
REQUIRED_STAGES = frozenset({'architecture_tuning', 'design', 'implementation', 'experiments',
    'tuning', 'validation_review', 'revision', 'final_tests'})
APPENDIX_STAGES = frozenset({'intake', 'literature', 'related_work', 'innovation', 'debate', 'report'})
STAGES = REQUIRED_STAGES | APPENDIX_STAGES


class SourceScopeError(ValueError):
    def __init__(self, code, message, *, path=None, line=None):
        self.diagnostic = {'kind': 'metric_review_source_scope', 'code': code,
            'message': message, 'path': path, 'line': line, 'scientific_approval': False}
        super().__init__(message)


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _sha(value):
    return hashlib.sha256(_encoded(value).encode('utf-8')).hexdigest()


def _text_sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _lf(text):
    return text.replace('\r\n', '\n').replace('\r', '\n')


def _rows(values):
    if not isinstance(values, (list, tuple)):
        raise SourceScopeError('invalid_inventory', 'Capability sources must be an ordered source inventory')
    result, paths = [], set()
    for row in values:
        if not isinstance(row, dict) or not isinstance(row.get('source'), str):
            raise SourceScopeError('invalid_source', 'Capability source text must be an exact string')
        path, digest = row.get('path'), row.get('sha256')
        if (not isinstance(path, str) or not path or '\\' in path or ':' in path
                or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts
                or PurePosixPath(path).as_posix() != path or path in paths
                or not isinstance(digest, str) or len(digest) != 64
                or any(c not in '0123456789abcdef' for c in digest)):
            raise SourceScopeError('invalid_identity', 'Capability source path/file SHA must be unique and canonical', path=path)
        _encoded(row)
        paths.add(path)
        result.append(copy.deepcopy(row))
    return result


def _contains_stage(node):
    return any(isinstance(n, ast.Name) and n.id == 'stage' for n in ast.walk(node))


def _pure_guard(node):
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        return all(_pure_guard(child) for child in node.values)
    if not (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == 'stage'
            and len(node.ops) == 1 and len(node.comparators) == 1):
        return False
    value=node.comparators[0]
    return (isinstance(value,ast.Constant) and isinstance(value.value,str)
        or isinstance(value,(ast.Set,ast.List,ast.Tuple)) and all(
            isinstance(child,ast.Constant) and isinstance(child.value,str) for child in value.elts))


def _guard(node, *, line):
    """Conservative possible stages; unknown stage syntax is never discarded."""
    if not _contains_stage(node):
        return STAGES
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        values = [_guard(v, line=line) for v in node.values]
        if isinstance(node.op, ast.Or) and any(not _contains_stage(v) for v in node.values):
            raise SourceScopeError('unsupported_stage_guard', 'A mixed stage/non-stage OR needs an explicit scope rule', path=ENGINE_PATH, line=line)
        result = set(STAGES) if isinstance(node.op, ast.And) else set()
        for value in values:
            result = result & value if isinstance(node.op, ast.And) else result | value
        return frozenset(result)
    if (not isinstance(node, ast.Compare) or len(node.ops) != 1 or len(node.comparators) != 1
            or not isinstance(node.left, ast.Name) or node.left.id != 'stage'):
        raise SourceScopeError('unsupported_stage_guard', 'Unknown dispatcher stage test; current source cannot be silently narrowed', path=ENGINE_PATH, line=line)
    operation, operand = node.ops[0], node.comparators[0]
    if isinstance(operation, (ast.Eq, ast.NotEq)) and isinstance(operand, ast.Constant) and isinstance(operand.value, str):
        values = {operand.value}
    elif isinstance(operation, (ast.In, ast.NotIn)) and isinstance(operand, (ast.Set, ast.List, ast.Tuple)) and all(
            isinstance(n, ast.Constant) and isinstance(n.value, str) for n in operand.elts):
        values = {n.value for n in operand.elts}
    else:
        raise SourceScopeError('unsupported_stage_guard', 'Dispatcher stages require literal, recognized stage identities', path=ENGINE_PATH, line=line)
    if not values or values - STAGES:
        raise SourceScopeError('unknown_stage', 'Dispatcher contains an unclassified stage: ' + ', '.join(sorted(values - STAGES)), path=ENGINE_PATH, line=line)
    return frozenset(STAGES - values if isinstance(operation, (ast.NotEq, ast.NotIn)) else values)


@functools.lru_cache(maxsize=32)
def _appendix_ranges(source):
    # capability_sources preserves class-method indentation, followed by module
    # constants. A synthetic parsing wrapper changes no published source byte.
    try:
        tree, adjustment = ast.parse(source), 0
    except SyntaxError:
        try:
            tree, adjustment = ast.parse('if True:\n' + source), 1
        except SyntaxError as exc:
            raise SourceScopeError('unsupported_syntax', 'Frozen Engine source cannot be safely parsed for metric scope', path=ENGINE_PATH, line=exc.lineno) from exc
    functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == '_stage']
    if len(functions) != 1:
        raise SourceScopeError('unknown_dispatcher', 'Expected exactly one frozen Engine._stage dispatcher', path=ENGINE_PATH)
    function = functions[0]
    if not any(a.arg == 'stage' for a in [*function.args.posonlyargs, *function.args.args]):
        raise SourceScopeError('unknown_dispatcher', 'Dispatcher stage argument changed', path=ENGINE_PATH)
    if any(isinstance(n, ast.Name) and n.id == 'stage' and isinstance(n.ctx, (ast.Store, ast.Del)) for n in ast.walk(function)):
        raise SourceScopeError('mutable_stage', 'Dispatcher mutates its stage selector; omission would be unsafe', path=ENGINE_PATH)
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for value in lines: offsets.append(offsets[-1] + len(value))
    ranges = []
    for node in function.body:
        if not isinstance(node, ast.If) or not _contains_stage(node.test):
            continue  # Shared setup, guards, nested dependencies and fallback remain complete.
        stages = _guard(node.test, line=node.lineno - adjustment)
        if stages and stages <= APPENDIX_STAGES:
            if not _pure_guard(node.test):
                raise SourceScopeError('impure_optional_guard', 'An optional stage guard has non-stage evaluation or side effects', path=ENGINE_PATH, line=node.lineno - adjustment)
            if node.orelse:
                raise SourceScopeError('unsupported_stage_else', 'An optional stage branch with else/elif needs an explicit scope rule', path=ENGINE_PATH, line=node.lineno - adjustment)
            if not _terminates(node.body):
                raise SourceScopeError('stage_fallthrough', 'An optional stage branch can fall through into shared metric code', path=ENGINE_PATH, line=node.lineno - adjustment)
            ranges.append((offsets[node.lineno - adjustment - 1], offsets[node.end_lineno - adjustment], sorted(stages)))
    return ranges


def _terminates(statements):
    if not statements:return False
    last=statements[-1]
    return isinstance(last,(ast.Return,ast.Raise)) or (isinstance(last,ast.If)
        and bool(last.orelse) and _terminates(last.body) and _terminates(last.orelse))


def _policy(row):
    if 'source_policy' not in row:return None
    policy=row['source_policy']
    if (not isinstance(policy,dict) or set(policy)!={'version','required_symbols'}
            or type(policy.get('version')) is not int or policy['version']!=2
            or not isinstance(policy.get('required_symbols'),list) or not policy['required_symbols']
            or any(not isinstance(n,str) or not n or any(not part.isidentifier() for part in n.split('.'))
                for n in policy['required_symbols'])
            or len(set(policy['required_symbols']))!=len(policy['required_symbols'])):
        raise SourceScopeError('unknown_source_policy','Unknown or invalid metric source policy',path=row['path'])
    return policy


def _assigned(node):
    if isinstance(node,ast.Name):return [node.id]
    if isinstance(node,(ast.Tuple,ast.List)):
        return [name for child in node.elts for name in _assigned(child)]
    if isinstance(node,ast.Starred):return _assigned(node.value)
    return []


@functools.lru_cache(maxsize=32)
def _policy_plan(path,source,policy_json):
    """Pure source-byte analysis; never cache receipt or filesystem authority."""
    policy=json.loads(policy_json)
    try:
        tree=ast.parse(source);table=symtable.symtable(source,path,'exec')
    except SyntaxError as exc:
        raise SourceScopeError('unsupported_syntax','Complete source module cannot be safely parsed',path=path,line=exc.lineno) from exc
    lines=source.splitlines(keepends=True);offsets=[0]
    for line in lines:offsets.append(offsets[-1]+len(line))
    def span(node):
        start=min([node.lineno,*[d.lineno for d in getattr(node,'decorator_list',[])]])
        return offsets[start-1],offsets[node.end_lineno]
    entries={};globals_index={};class_index={};mutations={};future=[];classes={}
    def register(name,node,owner='',dynamic=False):
        qualified=(owner+'.' if owner else '')+name
        entry=(qualified,node,owner,dynamic)
        entries.setdefault(qualified,[]).append(entry)
        (class_index.setdefault(owner,{}) if owner else globals_index).setdefault(name,[]).append(entry)
    def declare(node,owner='',dynamic=False,container=None):
        actual=container or node
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
            register(node.name,actual,owner,dynamic)
            if isinstance(node,ast.ClassDef) and not dynamic:
                qualified=(owner+'.' if owner else '')+node.name;classes[qualified]=node
                for child in node.body:declare(child,qualified)
        elif isinstance(node,(ast.Assign,ast.AnnAssign)):
            targets=node.targets if isinstance(node,ast.Assign) else [node.target]
            for target in targets:
                for name in _assigned(target):register(name,actual,owner,dynamic)
                if not owner and isinstance(target,(ast.Subscript,ast.Attribute)):
                    base=target
                    while isinstance(base,(ast.Subscript,ast.Attribute)):base=base.value
                    if isinstance(base,ast.Name):mutations.setdefault(base.id,[]).append(actual)
        elif isinstance(node,(ast.Import,ast.ImportFrom)):
            if isinstance(node,ast.ImportFrom) and node.module=='__future__':future.append(node)
            else:
                for alias in node.names:
                    if alias.name!='*':register(alias.asname or (alias.name.split('.')[0] if isinstance(node,ast.Import) else alias.name),actual,owner,dynamic)
        elif isinstance(node,ast.AugAssign) and isinstance(node.target,ast.Name):
            if not owner:mutations.setdefault(node.target.id,[]).append(actual)
        elif isinstance(node,ast.Expr) and isinstance(node.value,ast.Call) and isinstance(node.value.func,ast.Attribute):
            base=node.value.func.value
            while isinstance(base,(ast.Subscript,ast.Attribute)):base=base.value
            if not owner and isinstance(base,ast.Name):mutations.setdefault(base.id,[]).append(actual)
        elif isinstance(node,(ast.If,ast.Try,ast.TryStar,ast.For,ast.AsyncFor,ast.While,ast.With,ast.AsyncWith,ast.Match)):
            # Conditional/import fallback definitions are not guessed. If they
            # are needed, require an explicit supported declaration contract.
            for child in ast.iter_child_nodes(node):
                if isinstance(child,ast.stmt):declare(child,owner,True,actual)
                elif isinstance(child,(ast.ExceptHandler,ast.match_case)):
                    for statement in child.body:declare(statement,owner,True,actual)
    for node in tree.body:declare(node)
    def dynamic_namespace(node):
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.Lambda)):return False
        if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id in {'exec','eval','globals','locals'}:return True
        return any(dynamic_namespace(child) for child in ast.iter_child_nodes(node))
    if any(dynamic_namespace(node) for node in tree.body):
        raise SourceScopeError('unsupported_dynamic_binding','Dynamic module namespace access cannot be silently scoped',path=path)
    tables={}
    def index_table(current):
        tables.setdefault((current.get_name(),current.get_lineno()),[]).append(current)
        for child in current.get_children():index_table(child)
    index_table(table)
    def table_globals(current):
        result={s.get_name() for s in current.get_symbols() if s.is_global()}
        for child in current.get_children():result.update(table_globals(child))
        return result
    module_globals=table_globals(table)
    optional_stage=[]
    stage_nodes=[n for name,items in entries.items() for _,n,_,dynamic in items
        if not dynamic and isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name=='_stage']
    implicit=set(dir(builtins))|{'__name__','__file__','__package__','__doc__','__spec__','__loader__','__builtins__','__annotations__','__path__'}
    selected=set();ranges=[];visited_nodes=set();class_headers=set()
    def unique(candidates,name):
        if not candidates:raise SourceScopeError('unknown_symbol','Unknown frozen source symbol: '+name,path=path)
        if len(candidates)!=1 and not all(isinstance(entry[1],(ast.Assign,ast.AnnAssign)) and not entry[3] for entry in candidates):
            raise SourceScopeError('ambiguous_symbol','Multiple source declarations for: '+name,path=path)
        entry=candidates[0]
        if entry[3]:raise SourceScopeError('unsupported_binding','Conditional/dynamic source binding needs an explicit adapter: '+name,path=path,line=entry[1].lineno)
        return entry
    def dependency(name,owner=''):
        candidates=class_index.get(owner,{}).get(name) if owner else None
        if candidates:return unique(candidates,owner+'.'+name)
        if name in globals_index:return unique(globals_index[name],name)
        if name in implicit:return None
        raise SourceScopeError('unknown_dependency','Unresolved module-level source dependency: '+name,path=path)
    def loads(nodes,known=None):
        result=set()
        def visit(child,bound=frozenset()):
            if child is None:return
            if isinstance(child,ast.Name) and isinstance(child.ctx,ast.Load) and child.id not in bound and (known is None or child.id in known):
                position=offsets[child.lineno-1]
                if not any(a<=position<b for a,b,_ in optional_stage):result.add(child.id)
            if isinstance(child,(ast.ListComp,ast.SetComp,ast.DictComp,ast.GeneratorExp)):
                local=set(bound)
                for generator in child.generators:
                    visit(generator.iter,local);local.update(_assigned(generator.target))
                    for condition in generator.ifs:visit(condition,local)
                for expression in ([child.key,child.value] if isinstance(child,ast.DictComp) else [child.elt]):visit(expression,local)
                return
            if isinstance(child,ast.Lambda):
                for default in [*child.args.defaults,*child.args.kw_defaults]:visit(default,bound)
                names={a.arg for a in [*child.args.posonlyargs,*child.args.args,*child.args.kwonlyargs,
                    *([child.args.vararg] if child.args.vararg else []),*([child.args.kwarg] if child.args.kwarg else [])]}
                visit(child.body,set(bound)|names);return
            for nested in ast.iter_child_nodes(child):visit(nested,bound)
        for node in nodes:visit(node)
        return result
    def class_header(entry):
        qualified,cls,owner,_=entry
        if qualified in class_headers:return
        class_headers.add(qualified)
        first=cls.body[0]
        first_line=min([first.lineno,*[d.lineno for d in getattr(first,'decorator_list',[])]])
        ranges.append((span(cls)[0],offsets[first_line-1]))
        for name in sorted(loads([*cls.decorator_list,*cls.bases,*[k.value for k in cls.keywords]],module_globals|set(class_index.get(owner,{})))):
            related=dependency(name,owner)
            if related:include(related)
        for statement in cls.body:
            if not isinstance(statement,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
                include_node(statement,qualified)
    def class_references(nodes,known):
        for root in nodes:
            for item in ast.walk(root):
                position=offsets[item.lineno-1] if hasattr(item,'lineno') else -1
                if any(a<=position<b for a,b,_ in optional_stage):continue
                name=member=None
                if isinstance(item,ast.Attribute) and isinstance(item.value,ast.Name):name,member=item.value.id,item.attr
                elif isinstance(item,ast.Call) and isinstance(item.func,ast.Name):name,member=item.func.id,'__init__'
                if name not in known or name not in globals_index:continue
                entry=unique(globals_index[name],name)
                if not isinstance(entry[1],ast.ClassDef):continue
                class_header(entry)
                candidates=class_index.get(entry[0],{}).get(member)
                if candidates:include(unique(candidates,entry[0]+'.'+member))
    def include_node(node,owner='',qualified=None):
        if id(node) in visited_nodes:return
        visited_nodes.add(id(node));ranges.append(span(node))
        if path==ENGINE_PATH and any(node is n or span(node)[0]<=span(n)[0] and span(n)[1]<=span(node)[1] for n in stage_nodes):
            if not optional_stage:optional_stage.extend(_appendix_ranges(source))
        if any(isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id in {'exec','eval','globals','locals'}
                and not any(a<=offsets[n.lineno-1]<b for a,b,_ in optional_stage) for n in ast.walk(node)):
            raise SourceScopeError('unsupported_dynamic_binding','Dynamic namespace access needs an explicit source adapter',path=path,line=node.lineno)
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
            local=tables.get((node.name,node.lineno),[])
            if len(local)!=1:raise SourceScopeError('ambiguous_scope','Source scope table is not unique',path=path,line=node.lineno)
            known=table_globals(local[0])
            for name in sorted(loads(node.body,known)):
                entry=dependency(name)
                if entry:include(entry)
            class_references(node.body,known)
            headers=[*node.decorator_list]
            if isinstance(node,ast.ClassDef):headers.extend([*node.bases,*[k.value for k in node.keywords]])
            else:
                headers.extend([node.returns,*node.args.defaults,*node.args.kw_defaults])
                headers.extend(a.annotation for a in [*node.args.posonlyargs,*node.args.args,*node.args.kwonlyargs,
                    *([node.args.vararg] if node.args.vararg else []),*([node.args.kwarg] if node.args.kwarg else [])])
            for name in sorted(loads(headers,module_globals|set(class_index.get(owner,{})))):
                entry=dependency(name,owner)
                if entry:include(entry)
        else:
            for name in sorted(loads([node],module_globals|set(class_index.get(owner,{})))):
                entry=dependency(name,owner)
                if entry:include(entry)
    def include(entry,*,whole_class=False):
        qualified,node,owner,_=entry
        if isinstance(node,ast.ClassDef) and not whole_class:
            class_header(entry);return
        if qualified in selected:return
        selected.add(qualified)
        if owner:
            unique(entries[owner],owner)
            class_header(unique(entries[owner],owner))
        # Unconditional value rebinding has an exact ordered declaration chain.
        # Never pick just its final value or drop its prior self-reference.
        for _,binding_node,_,_ in sorted(entries[qualified],key=lambda entry:entry[1].lineno):
            include_node(binding_node,owner,qualified)
        if not owner:
            for mutation in mutations.get(qualified,[]):include_node(mutation)
    for requested in policy['required_symbols']:
        candidates=entries.get(requested)
        if not candidates and '.' not in requested:
            candidates=[entry for name,items in entries.items() if name.rsplit('.',1)[-1]==requested for entry in items]
        include(unique(candidates,requested),whole_class=True)
    for node in future:include_node(node)
    # Exact source intervals form a partition; annotations on v2 fragments are
    # navigation only. No synthesized stub or executable source is introduced.
    boundaries={0,len(source),*[value for pair in ranges for value in pair],*[value for a,b,_ in optional_stage for value in (a,b)]}
    points=sorted(boundaries);cuts=[]
    for start,end in zip(points,points[1:]):
        stage=next((stages for a,b,stages in optional_stage if a<=start and end<=b),None)
        required=any(a<=start and end<=b for a,b in ranges) and stage is None
        if required or start==end:continue
        stages=tuple(stage or ())
        if cuts and cuts[-1][1]==start and cuts[-1][2]==stages:cuts[-1]=(cuts[-1][0],end,stages)
        else:cuts.append((start,end,stages))
    declarations=tuple(sorted((name,*span(node)) for name,items in entries.items() for _,node,_,_ in items))
    return tuple(cuts),declarations


def _source_cuts(row):
    policy=_policy(row)
    if policy is not None:return _policy_plan(row['path'],row['source'],_encoded(policy))[0]
    return _appendix_ranges(row['source']) if row['path']==ENGINE_PATH else []


def _fragment(row, side, start, end, *, stages=()):
    source = row['source']; text = source[start:end]
    body = {'side': side, 'path': row['path'], 'file_sha256': row['sha256'],
        'record_sha256': _sha(row), 'original_source_sha256': _text_sha(source),
        'char_start': start, 'char_end': end, 'source_sha256': _text_sha(text),
        'source': text, 'scope_stages': list(stages), 'provenance': 'exact_frozen_capability_fragment'}
    policy=_policy(row)
    if policy is not None:
        declarations=_policy_plan(row['path'],source,_encoded(policy))[1]
        body['scope_symbols']=sorted({name for name,a,b in declarations if a<end and start<b})
    return {'id': _sha(body), **body}


def compile_sources(current, previous=()):
    """Return exact required fragments, optional appendices and display-only diff.

    Input rows keep capability_sources()'s path/file-SHA/source contract. File
    SHA and source SHA are deliberately separate: source may be a frozen method
    excerpt rather than the entire owning file. No filesystem or model call.
    """
    current, previous = _rows(current), _rows(previous)
    # Pure frozen-byte compilation only. Role, receipt delivery, filesystem
    # state and acceptance are never cached. Detach the result for consumers.
    return copy.deepcopy(_compiled_sources(_encoded([VERSION, current, previous])))


@functools.lru_cache(maxsize=16)
def _compiled_sources(serial):
    version, current, previous = json.loads(serial)
    if version != VERSION:
        raise SourceScopeError('unknown_version', 'Pure source compilation version changed')
    required, optional, manifests = [], [], {'current': [], 'previous': []}
    for side, rows in (('current', current), ('previous', previous)):
        for row in rows:
            if _policy(row) is not None:_source_cuts(row)  # Validate explicit policy on historical rows as well.
            cuts = _source_cuts(row) if side == 'current' else []
            parts, cursor = [], 0
            for start, end, stages in cuts:
                if cursor < start:
                    fragment = _fragment(row, side, cursor, start)
                    required.append(fragment); parts.append(fragment['id'])
                fragment = _fragment(row, side, start, end, stages=stages)
                optional.append(fragment); parts.append(fragment['id']); cursor = end
            if cursor < len(row['source']) or not parts:
                fragment = _fragment(row, side, cursor, len(row['source']))
                (required if side == 'current' else optional).append(fragment); parts.append(fragment['id'])
            manifests[side].append({'metadata': {k: copy.deepcopy(v) for k, v in row.items() if k != 'source'},
                'record_sha256': _sha(row), 'source_sha256': _text_sha(row['source']),
                'source_chars': len(row['source']), 'fragments': parts})
    body = {'version': VERSION, 'source_snapshot_sha256': _sha({'current': current, 'previous': previous}),
        'required_current_sources': required, 'optional_sources': optional, 'source_manifests': manifests,
        'normalized_diff_index': _diff_index(current,previous), 'scientific_approval': False, 'reading_credit': False}
    result = {**body, 'sha256': _sha(body)}
    if restore_sources(result) != {'current': current, 'previous': previous}:
        raise SourceScopeError('reconstruction_failed', 'Metric source view changed its frozen sources')
    return result


def _diff_index(current,previous):
    old, new = {r['path']: r for r in previous}, {r['path']: r for r in current}
    differences = []
    for path in sorted(old.keys() | new.keys()):
        before, after = old.get(path), new.get(path)
        left, right = (before or {}).get('source', ''), (after or {}).get('source', '')
        if before == after: continue
        normalized_left, normalized_right = _lf(left), _lf(right)
        diff = ''.join(difflib.unified_diff(normalized_left.splitlines(keepends=True), normalized_right.splitlines(keepends=True),
            fromfile='previous/' + path, tofile='current/' + path, n=3))
        differences.append({'path': path, 'previous_file_sha256': (before or {}).get('sha256'),
            'current_file_sha256': (after or {}).get('sha256'), 'previous_source_sha256': _text_sha(left),
            'current_source_sha256': _text_sha(right), 'previous_normalized_sha256': _text_sha(normalized_left),
            'current_normalized_sha256': _text_sha(normalized_right), 'display_diff': diff,
            'display_sha256': _text_sha(diff), 'display_transform': 'lf_line_endings_only',
            'normalized_source_equal': before is not None and after is not None and normalized_left == normalized_right,
            'source_evidence': False, 'reading_credit': False})
    return differences


def restore_sources(view):
    try:
        return _restore_sources(view)
    except SourceScopeError:
        raise
    except (KeyError, TypeError, IndexError, AttributeError) as exc:
        raise SourceScopeError('invalid_view', 'Metric source view has a missing or invalid typed field') from exc


def _restore_sources(view):
    if (not isinstance(view, dict) or type(view.get('version')) is not int or view['version'] != VERSION
            or view.get('scientific_approval') is not False or view.get('reading_credit') is not False
            or view.get('sha256') != _sha({k: v for k, v in view.items() if k != 'sha256'})):
        raise SourceScopeError('invalid_view', 'Metric review source view identity changed')
    fragments = {}; used = set(); restored = {}
    for required, values in ((True, view['required_current_sources']), (False, view['optional_sources'])):
        for fragment in values:
            if (fragment.get('id') != _sha({k: v for k, v in fragment.items() if k != 'id'})
                    or fragment['source_sha256'] != _text_sha(fragment['source'])
                    or fragment['char_end'] - fragment['char_start'] != len(fragment['source'])
                    or fragment['id'] in fragments or required and (fragment['side'] != 'current' or fragment['scope_stages'])):
                raise SourceScopeError('invalid_fragment', 'Metric source fragment identity or required scope changed')
            fragments[fragment['id']] = fragment
    for side in ('current', 'previous'):
        rows = []
        for manifest in view['source_manifests'][side]:
            cursor, texts = 0, []
            for identity in manifest['fragments']:
                f = fragments[identity]
                if (identity in used or f['side'] != side or f['path'] != manifest['metadata']['path']
                        or f['file_sha256'] != manifest['metadata']['sha256'] or f['char_start'] != cursor
                        or f['record_sha256'] != manifest['record_sha256']
                        or f['original_source_sha256'] != manifest['source_sha256']):
                    raise SourceScopeError('invalid_provenance', 'Metric source fragments lost their exact original ownership/range')
                used.add(identity); texts.append(f['source']); cursor = f['char_end']
            source = ''.join(texts); row = {**copy.deepcopy(manifest['metadata']), 'source': source}
            if cursor != manifest['source_chars'] or _text_sha(source) != manifest['source_sha256'] or _sha(row) != manifest['record_sha256']:
                raise SourceScopeError('invalid_reconstruction', 'Metric source reconstruction differs from its original bytes')
            rows.append(row)
        restored[side] = rows
    if used != fragments.keys() or _sha(restored) != view['source_snapshot_sha256']:
        raise SourceScopeError('foreign_source', 'Metric source view contains missing or foreign material')
    if view['normalized_diff_index']!=_diff_index(restored['current'],restored['previous']):
        raise SourceScopeError('diff_changed', 'Display-only metric diff differs from its exact frozen sources')
    # Recompile verifies selection and diff rules as well as content hashes.
    # Avoid recursion: compare the deterministic classified ranges directly.
    for row in restored['current']:
        expected = [(a,b,list(stages)) for a,b,stages in _source_cuts(row)]
        actual = [(f['char_start'], f['char_end'], f['scope_stages']) for f in view['optional_sources']
            if f['side'] == 'current' and f['path'] == row['path']]
        if actual != expected:
            raise SourceScopeError('scope_changed', 'Metric required/optional classification changed')
    for side,rows in restored.items():
        for row in rows:
            if _policy(row) is None:continue
            for fragment in [f for f in fragments.values() if f['side']==side and f['path']==row['path']]:
                if fragment!=_fragment(row,side,fragment['char_start'],fragment['char_end'],stages=fragment['scope_stages']):
                    raise SourceScopeError('scope_changed','Metric source symbol navigation changed',path=row['path'])
    return restored
