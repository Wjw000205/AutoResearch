"""Read the developer's hash-linked CPU evidence when freezing a code delivery."""
import hashlib
import json
from pathlib import Path, PurePosixPath

from .deletion import _plain_path
from .delivery_files import io_path
from .implementation_documents import _write_immutable


GUIDE = """开发 CPU 检查沿用现有交付摘要。需评审具体用例时，摘要可附 development_cpu 对象，其中 receipt_path、events_path 指向项目 artifacts/ 下保存的真实 JSON 回执和 JSONL 事件，receipt_sha256、events_sha256 为各自文件的实际摘要，review_case_ids 列出待核对的完整案号。摘要顶层 source_files_sha256 绑定受测源码。宿主在冻结交付时读取原件、核对摘要，将所选原始事件交由两位评审。请用宿主可读取的普通项目证据目录保存或逐字节导出原件；临时进程的私有目录可能在结束后不可访问。原始日志留在证据目录。"""


def _same_sources(sources,versions,runtime):
    return (isinstance(sources,dict) and bool(sources)
            and all(p in versions and versions[p]==h for p,h in sources.items())
            and all(sources.get(f['path'])==f['sha256'] for f in runtime))


def export_sources(previous):
    """Bind unreadable worker files to the prior frozen summary, including older capture records."""
    snapshot=previous['snapshot']
    versions={r['path']:r['sha256'] for r in snapshot['files']}
    pinned=[]
    for item in previous.get('development_check_evidence',[]):
        if 'unavailable' not in item or item['summary'] not in versions: continue
        raw=io_path(Path(snapshot['workspace'])/item['summary']).read_bytes()
        if versions[item['summary']]!=item['summary_sha256'] or hashlib.sha256(raw).hexdigest()!=item['summary_sha256']:
            raise ValueError('Previous development summary changed')
        summary=json.loads(raw)
        sources=summary.get('source_files_sha256')
        runtime=previous['acceptance']['runtime_bundle']['files']
        if not _same_sources(sources,versions,runtime):
            continue
        entry=summary['development_cpu']
        missing=False
        for k in ('receipt','events'):
            try: _source(io_path(Path(previous['checkpoint']['workspace'])),entry[k+'_path'],entry[k+'_sha256'])
            except (FileNotFoundError,PermissionError): missing=True
            except (OSError,ValueError): break
        else:
            if missing and item.get('unavailable_kind') in (None,'source_file_unavailable'):
                pinned.append({'summary_sha256':item['summary_sha256'], **{k:entry[k] for k in
                    ('receipt_path','receipt_sha256','events_path','events_sha256')}})
    return pinned


def export_instruction(workspace,pinned):
    if not pinned: return ''
    root=io_path(Path(workspace))
    folder=root/'artifacts'/'review-evidence'
    _plain_path(folder,root)
    folder.mkdir(parents=True,exist_ok=True)
    return ('\n宿主进程实际未读到前一交付声明的开发原件：'+json.dumps(pinned,ensure_ascii=False)
            +'。若原件及被测源码未变化，直接逐字节导出同一回执和事件，沿用其原 SHA，无需重跑。'
            '当两份退回意见只要求恢复已有原件访问时，本次修订只需导出并更新交付摘要的路径和来源，保留已测代码。'
            '宿主已建立项目共享目录 artifacts/review-evidence。直接在该目录新建两个普通文件，'
            '文件名各用真实 SHA 加 .json 或 .jsonl，写入原文件字节并验证。摘要 receipt_path、events_path 填这两个公开副本的相对路径；在 development_cpu.export_from_summary_sha256 填原摘要 SHA。'
            '在该共享目录中直接创建文件以继承目录访问权限，勿复制私有目录或其访问权限。')


def _source(root,ref,sha):
    relative=PurePosixPath(ref)
    if (not relative.parts or relative.parts[0]!='artifacts' or any(part in ('.','..') or ':' in part
            or '\\' in part for part in relative.parts) or relative.as_posix()!=ref):
        raise ValueError('Development check reference must be relative to project artifacts')
    p=root/relative
    _plain_path(p,root)
    if p.stat().st_size>16*1024**2: raise ValueError('Development check attachment exceeds 16 MiB')
    raw=p.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=sha: raise ValueError('Development check attachment SHA256 mismatch')
    return raw


def capture(engine,run_id,attempt,document_sha256):
    snapshot=attempt['snapshot']
    versions={r['path']:r['sha256'] for r in snapshot['files']}
    outputs=[]
    for name,sha in versions.items():
        if not (name.startswith('test-results/') and name.endswith('.json')): continue
        raw=io_path(Path(snapshot['workspace'])/name).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=sha: raise ValueError('Frozen test summary changed')
        try: summary=json.loads(raw)
        except (ValueError,UnicodeError): continue
        entry=summary.get('development_cpu') if isinstance(summary,dict) else None
        if not isinstance(entry,dict) or not entry.get('review_case_ids'): continue
        item={'summary':name,'summary_sha256':sha,'provenance':'developer_cpu_check'}
        try:
            root=io_path(Path(attempt['checkpoint']['workspace']))
            if ('export_from_summary_sha256' in entry or any(str(entry.get(k,'')).startswith('artifacts/review-evidence/')
                    for k in ('receipt_path','events_path'))):
                pin=next((p for p in attempt.get('development_export_sources',[])
                          if p['summary_sha256']==entry.get('export_from_summary_sha256')),None)
                if not pin or any(pin[k]!=entry.get(k) for k in ('receipt_sha256','events_sha256')):
                    raise ValueError('Exported bytes must match the original frozen development summary')
            sources=summary['source_files_sha256']
            runtime=attempt['acceptance']['runtime_bundle']['files']
            if not _same_sources(sources,versions,runtime):
                raise ValueError('Tested source hashes differ from frozen delivery')
            ids=entry['review_case_ids']
            if not isinstance(ids,list) or not all(isinstance(i,str) and i for i in ids) or len(ids)!=len(set(ids)):
                raise ValueError('Invalid requested development case IDs')
            receipt,events=(_source(root,entry[k+'_path'],entry[k+'_sha256']) for k in ('receipt','events'))
            header=json.loads(receipt)
            if (summary.get('implementation_document_sha256')!=document_sha256
                    or header.get('implementation_document_sha256')!=document_sha256
                    or header.get('source_files_sha256')!=sources or header.get('scope')!='development_cpu'
                    or header.get('events_sha256')!=entry['events_sha256']):
                raise ValueError('Receipt does not match the document, source, CPU scope or supplied events')
            lines=[json.loads(line) for line in events.splitlines() if line.strip()]
            if (lines!=header['cases'] or any(c.get('scope')!='development_cpu' for c in lines)
                    or len({c['case_id'] for c in lines})!=len(lines)):
                raise ValueError('Original events differ from receipt, scope or case inventory')
            selected=[c for c in lines if c.get('case_id') in ids]
            if {c['case_id'] for c in selected}!=set(ids):
                raise ValueError('Requested cases absent from development receipt or events')
            for k,data in (('receipt',receipt),('events',events)):
                path=io_path(engine.run_dir(run_id)/'implementation-checks'/entry[k+'_sha256'])
                path.parent.mkdir(parents=True,exist_ok=True)
                _write_immutable(path,data)
            item.update(receipt_sha256=entry['receipt_sha256'],events_sha256=entry['events_sha256'],
                document_sha256=document_sha256,environment=header.get('environment'),events=selected)
        except (OSError,ValueError,KeyError,TypeError,AttributeError) as exc:
            item['unavailable']=str(exc)
            item['unavailable_kind']='source_file_unavailable' if isinstance(exc,(FileNotFoundError,PermissionError)) else 'invalid_evidence'
        outputs.append(item)
    return outputs


def verified(engine,run_id,attempt):
    entries=attempt['development_check_evidence']
    snapshot=attempt['snapshot']
    versions={r['path']:r['sha256'] for r in snapshot['files']}
    for item in entries:
        if 'unavailable' in item: continue
        if item['summary'] not in versions: raise ValueError('Development summary is outside the frozen delivery')
        summary=io_path(Path(snapshot['workspace'])/item['summary']).read_bytes()
        if hashlib.sha256(summary).hexdigest()!=versions[item['summary']] or item['summary_sha256']!=versions[item['summary']]:
            raise ValueError('Archived development summary changed')
        declared=json.loads(summary)['development_cpu']
        blobs={}
        for key in ('receipt_sha256','events_sha256'):
            sha=item[key]
            if sha!=declared[key] or len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha):
                raise ValueError('Invalid archived development evidence identity')
            raw=io_path(engine.run_dir(run_id)/'implementation-checks'/sha).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=sha:
                raise ValueError('Archived development check evidence changed')
            blobs[key]=raw
        header=json.loads(blobs['receipt_sha256'])
        ids=set(declared['review_case_ids'])
        original=[e for e in header['cases'] if e['case_id'] in ids]
        if (header['events_sha256']!=item['events_sha256'] or header['implementation_document_sha256']!=item['document_sha256']
                or original!=item['events'] or header.get('environment')!=item.get('environment')):
            raise ValueError('Development evidence projection differs from its original receipt')
    return entries
