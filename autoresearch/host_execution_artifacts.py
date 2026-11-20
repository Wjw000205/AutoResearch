"""Read exact receipt-owned output files, separately from delivered model code."""
from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
import re

from .code_repair_contract import sha

FIELD='host_execution_artifacts'
PREFIX=':host-evidence:'
REFERENCE_POLICY='host_artifact_reference_version'
RECEIPT_FIELD='host_execution_receipts'


def receipts(context):
    result={}
    for ref in [context.get('host_verification'),*context.get(RECEIPT_FIELD,[])]:
        if isinstance(ref,dict) and ref.get('id') and ref.get('sha256'):
            if ref['id'] in result and result[ref['id']]!=ref:
                # A view can add display fields; the sealed identity must agree.
                old=result[ref['id']]
                if any(old.get(k)!=ref.get(k) for k in ('sha256','identity','artifacts')):
                    raise ValueError('Conflicting frozen host receipt references')
            result.setdefault(ref['id'],ref)
    # Evidence work carries authenticated receipt facts into the independent
    # review, even when its current-source receipt belongs to a later delivery.
    facts=context.get('implementation_evidence_work_item',{}).get('evidence',{}).get('host_receipt_facts',[])
    for fact in facts:
        identity=fact.get('receipt_id');digest=fact.get('receipt_sha256')
        if not identity or not digest:continue
        if identity in result:
            if result[identity]['sha256']!=digest:raise ValueError('Conflicting host evidence work receipt')
            continue
        result[identity]={'id':identity,'sha256':digest,'artifacts':fact['archived_artifacts'],
            'bound_reference_version':1,'source_inventory':fact.get('source_inventory')}
    return list(result.values())


def _path(value):
    return str(value).replace('\\','/').casefold()


def catalog(context):
    rows=[]
    for ref in receipts(context):
        for item in ref.get('artifacts',[]):
            if not item.get('path') or not re.fullmatch('[a-fA-F0-9]{64}',item.get('sha256','')):
                continue
            identity=str(ref['id']).split(':',1)[0]+PREFIX+sha({
                'receipt_id':ref['id'],'receipt_sha256':ref['sha256'],
                'path':item['path'],'sha256':item['sha256']})
            rows.append({'artifact_id':identity,'path':item['path'],'sha256':item['sha256'],
                'receipt_id':ref['id'],'receipt_sha256':ref['sha256']})
    return {'H'+str(i):row for i,row in enumerate(sorted(rows,key=lambda r:_path(r['path'])),1)}


def resolve(context,args,*,entries=None):
    if entries is None:entries=catalog(context)
    for field in ('id','path'):
        value=args.get(field)
        if value in entries:
            return entries[value]
        found=[item for item in entries.values() if value and
            (value==item['artifact_id'] or _path(value)==_path(item['path']))]
        if len(found)==1:
            return found[0]
    found=[item for item in entries.values() if item['sha256'].casefold()==str(args.get('expected_sha256','')).casefold()]
    return found[0] if len(found)==1 else None


def external_locator(args):
    """Unknown absolute locations go to the reader for a scoped error, not a file vote."""
    value=args.get('path',args.get('id',''))
    return isinstance(value,str) and (PREFIX in value or PureWindowsPath(value).is_absolute()
        or PurePosixPath(value).is_absolute() or re.fullmatch('H[0-9]+',value) is not None)


def read(engine,run_id,role,handoff_id,args):
    frozen=engine.store.get('handoff_inputs',handoff_id,{})
    hand=engine.store.get('handoffs',handoff_id,{})
    if (hand.get('run_id')!=run_id or hand.get('to_role')!=role or frozen.get('run_id')!=run_id
            or hand.get('input_hash')!=sha({k:frozen.get(k) for k in ('instruction','context','schema')})):
        raise ValueError('Host artifact requires the exact frozen role assignment')
    context=frozen['context'];entries=catalog(context)
    bound=resolve(context,args)
    if not bound:
        raise ValueError('Host artifact is outside this frozen receipt')
    from . import code_verification, hrp_host_timing
    ref=next(r for r in receipts(context) if r['id']==bound['receipt_id'])
    collection=ref.get('receipt_collection',code_verification.RECEIPTS)
    if collection not in {code_verification.RECEIPTS,hrp_host_timing.RECEIPTS}:
        raise ValueError('Unsupported host execution receipt type')
    record=engine.store.get(collection,ref['id'],{})
    if ref.get('bound_reference_version')==1 and record.get('sha256')!=ref['sha256']:
        alternate=engine.store.get(hrp_host_timing.RECEIPTS,ref['id'],{})
        if alternate.get('sha256')==ref['sha256']:record=alternate
    identity_matches=(record.get('identity',{}).get('source_sha256')==ref.get('source_inventory')
        if ref.get('bound_reference_version')==1 else record.get('identity')==ref.get('identity'))
    if (record.get('run_id')!=run_id or record.get('sha256')!=ref['sha256'] or not identity_matches
            or not any(a.get('path')==bound['path'] and a.get('sha256')==bound['sha256']
                for a in record.get('artifacts',[]))):
        raise ValueError('Host artifact receipt identity changed')
    code_verification._sealed_receipt(engine,record,record['identity'])
    path=Path(bound['path']);root=engine.run_dir(run_id).resolve()
    from .deletion import _plain_path
    _plain_path(path.absolute(),engine.data_dir)
    if not path.resolve().is_relative_to(root):
        raise ValueError('Host artifact escaped the run')
    from .delivery_files import read_page
    page=read_page(path,{**bound,'bytes':path.stat().st_size},offset=args.get('offset',0),
        limit=args.get('limit',4000),expected_sha256=args.get('expected_sha256'))
    return {**page,'artifact_id':bound['artifact_id'],'receipt_id':ref['id'],
        'receipt_sha256':ref['sha256'],'provenance':'sealed_host_execution_artifact',
        'scientific_approval':False}
