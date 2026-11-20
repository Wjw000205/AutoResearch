"""Resolve review citations exclusively against a frozen inventory, without filesystem lookup."""
import re
from pathlib import PurePosixPath


def relative_path(value):
    if not isinstance(value,str):
        return None
    value = value.replace('\\','/')
    if not value or value.startswith('/') or ':' in value or '\x00' in value or '..' in value.split('/'):
        return None
    path = PurePosixPath(value).as_posix()
    return path if path != '.' else None


def document_path(doc):
    artifact = doc.get('artifact_id','')
    return (relative_path(artifact.partition(':')[2]) or relative_path(doc.get('path'))
            or relative_path(doc.get('title')))


def matches(label, entries, *, document):
    if not isinstance(label,str):
        return set()
    if label in entries:
        return {label}
    identity = 'document_id' if document else 'artifact_id'
    exact_id = {a for a,e in entries.items() if e.get(identity)==label}
    if exact_id:
        return exact_id
    if re.fullmatch('[0-9A-Fa-f]{64}',label):
        hashes = {a for a,e in entries.items() if e['sha256']==label.lower()}
        if hashes:
            return hashes
    path = relative_path(label)
    if path is None:
        return set()
    paths = {a:document_path(e) if document else relative_path(e['path']) for a,e in entries.items()}
    exact = {a for a,p in paths.items() if p==path}
    if exact:
        return exact
    candidates = {a for a,p in paths.items() if p and p.casefold()==path.casefold()}
    if not candidates and '/' not in path:
        candidates = {a for a,p in paths.items() if p and PurePosixPath(p).name.casefold()==path.casefold()}
    return candidates


class ChoiceNeeded(ValueError):
    def __init__(self, location, entries, choices):
        super().__init__('引用未能唯一定位当前交付中的文件。')
        self.location = location
        self.choices = sorted(choices or entries)


def select(ref, entries, *, document, location, default=None, selection=None):
    name = 'document' if document else 'file'
    sha_field = 'document_sha256' if document else 'sha256'
    path_fields = (name,'path','document_id' if document else 'artifact_id')
    candidate_sets = [matches(ref[k],entries,document=document) for k in path_fields if k in ref]
    sha = ref.get(sha_field)
    if isinstance(sha,str) and re.fullmatch('[a-fA-F0-9]{64}',sha):
        candidate_sets.append({a for a,e in entries.items() if e['sha256']==sha.lower()})
    matched = [s for s in candidate_sets if s]
    if not matched and not any(k in ref for k in (*path_fields,sha_field)) and default in entries:
        return entries[default]
    common = set.intersection(*matched) if matched else set()
    if len(common)==1:
        return entries[common.pop()]
    choices = common or set().union(*matched) or set(entries)
    if selection in choices:
        return entries[selection]
    raise ChoiceNeeded(location,entries,choices)
