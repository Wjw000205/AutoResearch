"""Read-only qualified source bindings for an unfrozen consensus assignment."""
from .idea_provenance import _full_text
from .literature_reading import _run_papers


def binding_report(store, run_id, proposal, qualified_notes, document_loader):
    from .paper_identity import resolve, same_paper, task_paper
    papers = _run_papers(store, run_id)
    cited = set(proposal.get('references', []))
    cited.update(x['paper_id'] for x in proposal.get('idea_rationale', {}).get('evidence', []))
    claimed = {x['paper_id']: x['evidence_level'] for x in proposal.get('idea_rationale', {}).get('evidence', [])}
    rows = []
    for pid in sorted(cited):
        paper = task_paper(store, run_id, pid, papers)
        if not paper:
            continue
        owned_notes = [note for note in qualified_notes if (noted := task_paper(store, run_id, note.get('paper_id'), papers))
                       and same_paper(store, paper, noted)]
        passages, seen = [], set()
        # Prefer a retained PDF over an abstract page, without upgrading either
        # passage to complete-paper reading or scientific verification.
        for note in sorted(owned_notes, key=lambda n: n.get('created_at', ''), reverse=True):
            for citation in note.get('citations', []):
                locator = citation.get('locator')
                if not isinstance(locator, str) or locator in seen:
                    continue
                seen.add(locator)
                if not _full_text(store, run_id, pid, locator, document_loader):
                    continue
                document = document_loader(run_id, note['source_id'])
                passages.append({'note_id': note['id'], 'note_sha256': note.get('note_sha256'),
                    'source_id': note['source_id'], 'text_sha256': note['text_sha256'],
                    'content_sha256': note.get('content_sha256'), 'format': document.get('format'),
                    'locator': locator, 'char_start': citation.get('char_start'),
                    'char_end': citation.get('char_end'), 'reading_scope': note['reading_scope'],
                    'source_binding_valid': True, 'complete_paper_reading': False})
        passages.sort(key=lambda x: x['format'] != 'pdf')
        rows.append({'paper_id': pid, 'canonical_paper_id': resolve(store, paper), 'claimed_evidence_level': claimed.get(pid),
            'saved_metadata_abstract_available': bool(str(paper.get('abstract') or '').strip()),
            'qualified_note_count': len(owned_notes),
            'qualified_passage_count': len(passages), 'passage_examples': passages[:3],
            'next_action': '复用合格笔记与原文定位，修正提案证据层级并独立核对比较范围。' if passages
                else '保留未读或缺失状态；仅在决定性比较缺证据时补检索。'})
    return {'derived': True, 'papers': rows, 'scientific_claims_verified': False,
        'scope': '只读重验已有获取、原文字节、定位与合格笔记。示例按版本ID可重新读取，不代表全文已读或共识通过。'}
