"""Deterministic, capacity-bounded research working sets; never a model summary.

The input/archive is immutable. Catalog cards are copied facts with versioned
locators. Current primary reads, failures, proposals and unresolved decisions are
mandatory. If these cannot fit, fail before dispatch rather than delete evidence.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from collections import Counter

from .context_artifacts import _object
from .compression_safety import CLASSES, pinned, protected, protect_card

VERSION = 2


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


@dataclass(frozen=True)
class Profile:
    name: str
    target: int
    reduction_trigger: int
    detail_cards: int


def profile_for(context):
    from .training_context import enabled as training_context
    if training_context(context):
        return Profile('experiment_execution', 12000, 16000, 0)
    purpose = str(context.get('_purpose', '')).split(':', 1)[0]
    if purpose == 'deep_paper':
        return Profile('deep_single_paper', 96000, 128000, 0)
    if purpose == 'context_evidence':
        return Profile('evidence_extraction', 24000, 36000, 0)
    if purpose in {'literature', 'literature_reading', 'evidence', 'related_work_queries'}:
        return Profile('reading', 48000, 96000, 8)
    if purpose in {'innovation_candidate', 'innovation_compare', 'innovation_review', 'proposal', 'revise', 'vote'}:
        return Profile('research_judgment', 64000, 96000, 12)
    if purpose in {'implementation', 'implementation_spec', 'repair', 'review', 'code_review'}:
        return Profile('implementation', 80000, 128000, 8)
    return Profile('research', 48000, 80000, 8)


def effort_profile(profile, effort):
    """Soft working-set targets, independent of provider window and output reserve."""
    if profile.name in {'evidence_extraction', 'implementation', 'experiment_execution'} or effort not in {'high', 'max'}:
        return profile
    if profile.name == 'deep_single_paper':
        return Profile(profile.name, 150000, 150000, profile.detail_cards)
    target = 90000 if effort == 'max' else 40000
    return Profile(profile.name, target, max(target, profile.reduction_trigger), profile.detail_cards)


class ContextBudgetExceeded(ValueError):
    def __init__(self, manifest):
        self.manifest = manifest
        limit_name = 'configured_input_limit' if manifest.get('task_ceiling_enforced') is False else 'task_input_limit'
        super().__init__('Context compiler requires a smaller evidence task: '
                         f"profile={manifest['profile']}, estimated_input={manifest['input_tokens']}, "
                         f"{limit_name}={manifest['ceiling_tokens']}. "
                         'Mandatory evidence was retained; no request was dispatched.')


def _terms(text):
    text = str(text).lower()
    words = re.findall(r'[a-z0-9_]{3,}', text)
    for phrase in re.findall(r'[\u3400-\u9fff]+', text):
        words.extend(phrase[i:i+2] for i in range(len(phrase)-1))
    return set(words)


def _adverse(value):
    """Keep explicit uncertainty/failures, without interpreting paper prose."""
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    return {key: copy.deepcopy(item) for key, item in value.items()
            if item is False or item is None or key in {
                'status', 'note', 'reason', 'error', 'errors', 'conflicts', 'metadata_conflicts',
                'limitations', 'missing_information', 'warnings', 'issues', 'eligible', 'rank'}
            or (key not in {'method', 'evidence_url', 'metadata_sha256', 'canonical_title', 'canonical_year',
                            'venue_metadata', 'title_match', 'identifier_match', 'author_match', 'year_match',
                            'abbr', 'catalog_page', 'catalog_version', 'source_url', 'kind', 'venue_name', 'venue_names'}
                and item not in (True, [], {}, ''))}


def paper_card(paper):
    if not isinstance(paper, dict) or not paper.get('id'):
        return copy.deepcopy(paper)
    fields = {'id', 'title', 'year', 'publication_date', 'url', 'verified', 'provenance',
              'full_text_status', 'metadata_conflicts', 'scope', 'rag_entry_id', 'rag_version_id'}
    result = {key: copy.deepcopy(value) for key, value in paper.items() if key in fields}
    for field in ('verification', 'ccf'):
        if field in paper:
            result[field] = _adverse(paper[field])
    # Unknown schema extensions must not disappear as bibliographic noise.
    known = fields | {'doi', 'arxiv_id', 'authors', 'abstract', 'abstract_excerpt', 'abstract_truncated',
        'date_granularity', 'fulltext_url', 'identity_aliases', 'matched_queries', 'source', 'sources',
        'checked_at', 'venue', 'verification', 'ccf', 'relevance', 'search', 'rag_analysis_at',
        'rag_score', 'retrieval_origin', 'cache_hit'}
    result.update({key: copy.deepcopy(value) for key, value in paper.items() if key not in known})
    result['record_sha256'] = fingerprint(paper)
    return result


def note_card(note, path, *, details):
    if not isinstance(note, dict):
        return copy.deepcopy(note)
    # Findings, measured results and adverse facts are mandatory for EVERY note.
    fields = {'id', 'paper_id', 'source_id', 'text_sha256', 'note_sha256', 'summary', 'results',
        'limitations', 'missing_information', 'reading_scope', 'generation', 'retrieval_status',
        'scientific_claims_verified', 'document_identity_verified', 'paper_identity_verified',
        'source_text_truncated', 'source_untrusted', 'scope_note'}
    detail_fields = {'research_question', 'method', 'datasets', 'baselines', 'relevance'}
    if details:
        fields |= detail_fields
    known = fields | detail_fields | {'excerpts', 'citations', 'content_sha256', 'memory_chunk_ids',
        'memory_document_id', 'created_at', 'run_id', 'role', 'source_read_receipts',
        'source_read_event_ids', 'reused_from'}
    card = {key: copy.deepcopy(value) for key, value in note.items() if key in fields or key not in known}
    # Preserve exact annotations if excerpt/citation schemas have new fields.
    citation_fields = {'locator', 'quote', 'kind', 'index', 'char_start', 'char_end', 'page_number', 'source_read_event_ids'}
    annotated = [copy.deepcopy(row) for key in ('excerpts', 'citations') for row in note.get(key, [])
                 if isinstance(row, dict) and set(row) - citation_fields]
    if annotated:
        card['annotated_citations'] = annotated
    anchors = [{key: row[key] for key in ('locator', 'char_start', 'char_end', 'page_number') if key in row}
               for row in note.get('excerpts', []) if isinstance(row, dict) and row.get('locator')]
    if anchors:
        card['source_anchors'] = anchors
    card['archive_pointer'] = path
    card['detail_level'] = 'findings_and_method' if details else 'findings_and_gaps'
    card['historical_excerpt_text_displayed'] = False
    return card


def catalog_view(document, artifact_id, sha, query, profile):
    notes, papers = document.get('notes'), document.get('papers')
    if (not isinstance(notes, list) or not isinstance(papers, list)
            or any(not isinstance(row, dict) for row in [*notes, *papers])):
        raise ValueError('Unknown catalog shape')
    terms = _terms(query)
    ranked = sorted(range(len(notes)), key=lambda i: (-len(terms & _terms(encoded(notes[i]))), i))
    # Reading plans also enumerate rejected/background/search candidates. Their
    # IDs are navigation, not a requirement to load every paper into synthesis.
    # Keep the entire plan itself, but pin only active analysis/gate/query papers.
    bindings = encoded({key: document.get(key) for key in ('analysis', 'reading_gate')}) + query
    required_ids = {paper.get('id') for paper in papers if isinstance(paper, dict)
                    and isinstance(paper.get('id'), str) and paper['id'] in bindings}
    selected_indices = set(ranked[:profile.detail_cards]) | {
        i for i, note in enumerate(notes) if isinstance(note, dict) and (note.get('paper_id') in required_ids
            or pinned(note))}
    # Do not let lexical similarity crowd out limits from other scoped papers.
    adverse_candidates = [i for i in ranked if protected(notes[i])]
    selected_indices.update(adverse_candidates[:max(1, profile.detail_cards // 3)])
    selected_ids = required_ids | {notes[i].get('paper_id') for i in selected_indices if isinstance(notes[i], dict)}
    ranked_papers = sorted(range(len(papers)), key=lambda i: (-len(terms & _terms(encoded(papers[i]))), i))
    selected_ids |= {papers[i].get('id') for i in ranked_papers[:max(12, profile.detail_cards)] if isinstance(papers[i], dict)}
    # No model-derived summaries. Existing analysis, reading gates and coverage
    # limitations are preserved exactly, including negative judgments.
    result = {key: copy.deepcopy(value) for key, value in document.items() if key not in {'papers', 'notes'}}
    result['papers'] = [paper_card(paper) for paper in papers if not isinstance(paper, dict) or paper.get('id') in selected_ids]
    result['notes'] = [note_card(note, f'/notes/{i}', details=True) for i, note in enumerate(notes) if i in selected_indices]
    result['selection'] = {'paper_count': len(papers), 'papers_selected': len(result['papers']),
        'note_count': len(notes), 'notes_selected': len(result['notes']),
        'adverse_candidates': len(adverse_candidates),
        'adverse_selected': sum(protected(notes[i]) for i in selected_indices),
        'method': 'Explicit current paper IDs and pinned negative findings, an independent limitations stratum, then lexical relevance; reading plan retained as navigation.',
        'omission_policy': 'Other catalog entries remain in the versioned archive. Selection is not rejection, '
                           'proof of absence or evidence that the entire catalog was scientifically read.'}
    view = {'artifact_id': artifact_id, 'sha256': sha, 'document': result,
        'policy': 'Historical catalog cards, not a new reading. Each selected note preserves its finding, numerical result, '
                  'limitation, missing-information item and verification status is retained. '
                  'Historical excerpt text and unselected method details are archived, not displayed. '
                  'Use evidence.read when allowed, with artifact_id, expected_sha256 and archive_pointer to inspect the original note; '
                  'otherwise use the supplied RAG/memory/source tools. '
                  'then sources.read/pdf.read for primary evidence when tools are open. If tools are closed, '
                  'use only displayed evidence and state remaining gaps. No inferred novelty or approval.'}
    if profile.name == 'reading':
        from .context_navigation import reading_catalog
        view = reading_catalog(view)
    return view


def _catalogs(run_id, rows, valid_page, query, profile):
    groups, catalogs, covered = {}, [], {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        call, page = row.get('call'), row.get('result')
        if isinstance(call, dict) and call.get('name') == 'evidence.catalog' and isinstance(page, dict):
            view, args = page.get('catalog'), call.get('arguments', {})
            if (isinstance(view, dict) and isinstance(args, dict)
                    and str(view.get('artifact_id', '')).startswith(f'{run_id}:literature/index/')
                    and view.get('artifact_id') == args.get('artifact_id') and view.get('sha256') == args.get('expected_sha256')
                    and page.get('directory_sha256') == fingerprint(view)
                    and page.get('provenance') == 'local_catalog_projection' and page.get('primary_source_reading') is False):
                from .context_navigation import reading_catalog
                catalogs.append(reading_catalog(view) if profile.name == 'reading' else copy.deepcopy(view))
                covered[index] = len(catalogs)-1
            continue
        if not isinstance(call, dict) or call.get('name') != 'artifacts.read' or not valid_page(page):
            continue
        args = call.get('arguments', {})
        if (not page['artifact_id'].startswith(f'{run_id}:literature/index/')
                or not isinstance(args, dict) or args.get('id') != page['artifact_id']
                or args.get('offset', 0) != page['offset']):
            continue
        groups.setdefault((page['artifact_id'], page['sha256'], page['total_chars']), []).append((index, page))
    for (artifact, sha, total), entries in groups.items():
        pages, conflict = {}, False
        for _, page in entries:
            if page['offset'] in pages and pages[page['offset']] != page['text']:
                conflict = True
            pages[page['offset']] = page['text']
        cursor, parts = 0, []
        for offset, text in sorted(pages.items()):
            if offset != cursor:
                conflict = True
                break
            parts.append(text)
            cursor += len(text)
        raw = ''.join(parts)
        if conflict or cursor != total or hashlib.sha256(raw.encode()).hexdigest() != sha:
            continue
        try:
            document = json.loads(raw, object_pairs_hook=_object)
            if not isinstance(document, dict) or not {'analysis', 'reading_gate', 'papers', 'notes'} <= document.keys():
                continue
            view = catalog_view(document, artifact, sha, query, profile)
        except (ValueError, TypeError):
            continue
        catalogs.append(view)
        for index, _ in entries:
            covered[index] = len(catalogs) - 1
    return catalogs, covered


def source_page(row, valid_page):
    """Only metadata is reduced. Primary text and its exact boundaries survive."""
    call, result = row.get('call'), row.get('result')
    if (not isinstance(call, dict) or call.get('name') not in
            {'sources.read', 'sources.fetch', 'sources.open_reference', 'pdf.read', 'context.expand'}
            or not isinstance(result, dict) or result.get('error')
            or result.get('source_id') != result.get('artifact_id')
            or result.get('untrusted') is not True or result.get('sha256') != result.get('text_sha256')):
        return None
    page = {key: result.get(key) for key in ('text', 'artifact_id', 'offset', 'total_chars', 'next_offset', 'sha256')}
    page['truncated'] = result.get('page_truncated')
    if not valid_page(page):
        return None
    # Only these known positive bookkeeping/navigation fields are externalized.
    omitted_fields = {'source_links', 'bytes', 'name', 'kind', 'run_id', 'created_at', 'checked_at',
        'fetched_at', 'original_url', 'original_artifact_id', 'content_sha256', 'extracted_text_sha256',
        'original_bytes_available', 'memory_document_id', 'chunk_count', 'index_version', 'index_status'}
    # Keep unexpected source-link annotations and failed/unknown metadata.
    links = result.get('source_links')
    if not isinstance(links, list) or any(not isinstance(link, dict)
            or set(link) != {'url', 'verified', 'retrieved', 'relation'} or link.get('verified') is not False
            or link.get('retrieved') is not False or link.get('relation') != 'link_in_retrieved_source' for link in links):
        omitted_fields.discard('source_links')
    omitted_fields -= {key for key in omitted_fields if result.get(key) is False or result.get(key) is None
                       or result.get(key) in ('failed', 'error', 'unknown')}
    projected = {key: copy.deepcopy(value) for key, value in result.items() if key not in omitted_fields}
    projected['metadata_archive_sha256'] = fingerprint(result)
    return {**copy.deepcopy(row), 'result': projected}


def page_key(row):
    page = row['result']
    return fingerprint({k: page[k] for k in ('source_id', 'text_sha256', 'offset', 'text')})


def current_review(run_id, context, rows, valid_page):
    """A targeted reader uses the fully delivered current gate, not old votes.

    Only same-proposal historical novelty reviews may be superseded. Partial,
    foreign or unverified artifacts and unrelated decisions cannot hide dissent.
    """
    if not str(context.get('_purpose', '')).startswith('literature_reading:novelty-evidence:'):
        return None
    ref = context.get('rejection_index', {})
    if not isinstance(ref, dict) or not str(ref.get('artifact_id', '')).startswith(run_id + ':literature/index/'):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        page = row.get('result') if isinstance(row, dict) else None
        if (row.get('call', {}).get('name') != 'artifacts.read' or not valid_page(page)
                or page['artifact_id'] != ref.get('artifact_id') or page['sha256'] != ref.get('sha256')
                or page['offset'] != 0 or len(page['text']) != page['total_chars']
                or hashlib.sha256(page['text'].encode()).hexdigest() != page['sha256']):
            continue
        try:
            gate = json.loads(page['text'], object_pairs_hook=_object)
        except (ValueError, TypeError):
            continue
        if (isinstance(gate, dict) and gate.get('proposal_hash') == context.get('proposal_hash')
                and gate.get('approved') is False and isinstance(gate.get('votes'), list)
                and len(gate['votes']) == 3
                and {v.get('role') for v in gate['votes'] if isinstance(v, dict)} == {'director', 'engineer', 'scout'}
                and all(v.get('proposal_hash') == context['proposal_hash'] for v in gate['votes'])):
            return gate
    return None


def share_source_metadata(context):
    """Hoist exactly repeated source metadata; negative flags remain verbatim."""
    groups = {}
    for row in context.get('tool_results', []):
        page = row.get('result') if isinstance(row, dict) else None
        if isinstance(page, dict) and page.get('metadata_archive_sha256'):
            groups.setdefault((page['source_id'], page['text_sha256']), []).append(page)
    library = {}
    page_fields = {'source_id', 'artifact_id', 'text_sha256', 'sha256', 'text', 'offset', 'next_offset',
        'total_chars', 'page_sha256', 'page_truncated', 'pdf_page', 'page_offset', 'next_page_offset',
        'page_text_truncated', 'citations', 'metadata_archive_sha256', 'evidence_card', 'primary_text_displayed'}
    for pages in groups.values():
        if len(pages) < 2:
            continue
        common = {k: v for k,v in pages[0].items() if k not in page_fields and all(p.get(k) == v and k in p for p in pages[1:])}
        if len(encoded(common)) < 500:
            continue
        key = f's{len(library)}'
        library[key] = common
        for page in pages:
            for field in common:
                page.pop(field)
            page['source_metadata_ref'] = f'/context/source_metadata/{key}'
    if library:
        context['source_metadata'] = library
        context['source_metadata_policy'] = 'Each source_metadata_ref supplies exact shared fields of its page, including verification failures and extraction limitations. Interpret these fields together; no scientific evidence was deleted.'


def exact_text_table(result, run_id, valid_page):
    """Opportunistic lossless interning, including small working sets.

    Only complete, hash-verified internal JSON receipts can change from a JSON
    string to a parsed value. No primary-source text is parsed or summarized.
    Every repeated string stays verbatim in the visible table.
    """
    value = copy.deepcopy(result)
    for row in value.get('context', {}).get('tool_results', []):
        page, call = row.get('result'), row.get('call', {})
        if (call.get('name') == 'artifacts.read' and isinstance(page, dict) and valid_page(page)
                and str(page.get('artifact_id', '')).startswith(f'{run_id}:literature/index/')
                and page.get('offset') == 0 and len(page['text']) == page['total_chars']
                and hashlib.sha256(page['text'].encode()).hexdigest() == page['sha256']):
            try:
                parsed = json.loads(page['text'], object_pairs_hook=_object)
            except (ValueError, TypeError):
                continue
            page.pop('text')
            page['json_content'] = parsed
            page['content_encoding'] = 'Parsed complete internal JSON; raw bytes and hash retained in original receipt.'
    counts = Counter()
    def count(item):
        if isinstance(item, str) and len(item) >= 32:
            counts[item] += 1
        elif isinstance(item, dict):
            if '$text' in item or 'shared_context_text' in item:
                raise ValueError('Reserved context interning key already present')
            for sub in item.values():
                count(sub)
        elif isinstance(item, list):
            for sub in item:
                count(sub)
    try:
        # The manifest stays directly readable. Counting its strings would
        # create table entries that cannot save any of those occurrences.
        count({key: item for key, item in value.items() if key != 'context_compilation'})
    except ValueError:
        return result
    refs = {text: str(i) for i, (text, n) in enumerate(counts.items()) if n > 1}
    def pack(item):
        if isinstance(item, str) and item in refs:
            return {'$text': refs[item]}
        if isinstance(item, dict):
            return {k: pack(v) for k,v in item.items()}
        if isinstance(item, list):
            return [pack(v) for v in item]
        return item
    packed = pack({key: item for key, item in value.items() if key != 'context_compilation'})
    packed['shared_context_text'] = {key: text for text, key in refs.items()}
    packed['shared_context_text_policy'] = ('LOSSLESS TEXT TABLE: every object with exactly one $text field stands for the '
        'complete verbatim string at shared_context_text[ID]. Resolve references before interpreting any ID, hash, '
        'quotation, limitation or JSON pointer. All evidence strings are present; no evidence was summarized or omitted.')
    # Keep the public budget manifest directly readable; its strings are small.
    packed['context_compilation'] = result['context_compilation']
    return packed if len(encoded(packed).encode('utf-8')) < len(encoded(result).encode('utf-8')) else result


def exact_catalog_table(result):
    """Intern identical JSON records only within already validated catalogs.

    Different versions, unknown fields, numbers and adverse findings remain
    exact. The visible table reconstructs every view without archive access.
    """
    catalogs = result.get('context', {}).get('evidence_catalogs')
    if not isinstance(catalogs, list) or len(catalogs) < 2:
        return result
    counts = Counter()
    def reserved(item):
        if isinstance(item, dict):
            return ('$catalog_record' in item or 'shared_context_catalog_records' in item
                    or any(reserved(v) for v in item.values()))
        return isinstance(item, list) and any(reserved(v) for v in item)
    if reserved(result):
        return result  # Literal untrusted extensions must not become table references.
    def count(item):
        if isinstance(item, (dict, list)):
            serial = encoded(item)
            if len(serial.encode('utf-8')) >= 512:
                counts[serial] += 1
            for child in (item.values() if isinstance(item, dict) else item):
                count(child)
    count(catalogs)
    repeated = {serial for serial, n in counts.items() if n > 1}
    if not repeated:
        return result
    records, refs = {}, {}
    def children(item):
        if isinstance(item, dict):
            return {key: pack(value) for key, value in item.items()}
        return [pack(value) for value in item]
    def pack(item):
        if not isinstance(item, (dict, list)):
            return copy.deepcopy(item)
        serial = encoded(item)
        if serial not in repeated:
            return children(item)
        if serial not in refs:
            identity = str(len(refs))
            refs[serial] = identity
            records[identity] = children(item)
        return {'$catalog_record': refs[serial]}
    packed = copy.deepcopy(result)
    packed['context']['evidence_catalogs'] = pack(catalogs)
    packed['shared_context_catalog_records'] = records
    packed['shared_context_catalog_records_policy'] = (
        'LOSSLESS CATALOG RECORD TABLE: each object with exactly one $catalog_record field '
        'stands for the complete JSON value at shared_context_catalog_records[ID]. Expand '
        'nested record references, then resolve any $text references using shared_context_text. '
        'All catalog versions, fields, numbers, quotations, limitations, disagreements and unknown '
        'extensions remain present. Resolve references before interpreting source hashes or JSON '
        'pointers. This is exact deduplication, not new evidence, reading credit or approval.')
    return packed if len(encoded(packed).encode('utf-8')) < len(encoded(result).encode('utf-8')) else result


class ContextCompiler:
    def build(self, run_id, active, system, hard_limit, *, estimate, valid_page, evidence_memory=None, image_tokens=0, effort=None, working_set=None):
        options = dict(estimate=estimate, valid_page=valid_page, evidence_memory=evidence_memory,
                       image_tokens=image_tokens, effort=effort)
        if not working_set:
            return self._build(run_id, active, system, hard_limit, **options)
        profile = effort_profile(profile_for(active['context']), effort)
        detail = working_set.get('_detail_target', int(profile.target * .35))
        has_details = any(rid in working_set.get('consumed', ()) for rid in working_set.get('views', {}))
        budgets = list(dict.fromkeys((detail, detail // 2, 0))) if has_details else [detail]
        candidates, failures = [], []
        for index, budget in enumerate(budgets):
            # Each view comes from the immutable records, never a prior summary.
            # Try the leanest view even when the first already meets its target.
            candidate = copy.deepcopy(working_set)
            candidate.update(_detail_target=budget, _recompile_pass=index,
                             _compression_candidates=len(budgets))
            try:
                candidates.append(self._build(run_id, active, system, hard_limit,
                                              working_set=candidate, **options))
            except ContextBudgetExceeded as error:
                failures.append(error)
        if not candidates:
            raise min(failures, key=lambda error: error.manifest['input_tokens'])
        # Prefer the fuller first view on a tie. Guard inventories can be larger
        # than full records, so blindly selecting the final pass wastes context.
        return min(candidates, key=lambda item: item[1]['input_tokens'])

    def _build(self, run_id, active, system, hard_limit, *, estimate, valid_page, evidence_memory=None, image_tokens=0, effort=None, working_set=None):
        original = copy.deepcopy(active)
        result = copy.deepcopy(active)
        context = result['context']
        profile = effort_profile(profile_for(context), effort)
        # Profiles control packing effort, never override the user's/model's
        # admissible input capacity. A large window is not a filling target.
        ceiling = hard_limit
        query = '\n'.join(str(value) for value in (active['task'], context.get('selected_idea', ''),
                          context.get('proposal', ''), context.get('unresolved_issues', '')))
        rows = context.get('tool_results', [])
        if not isinstance(rows, list):
            rows = []
        working_actions = []
        if working_set:
            from .working_context import select
            rows, working_actions = select(run_id, rows, working_set, evidence_memory, valid_page,
                                           estimate=estimate, target=profile.target)
            if working_set.get('commit'):
                context['working_state'] = copy.deepcopy(working_set['commit']['payload']['state'])
        issue_index = (context.get('rejection_index') or {}).get('artifact_id')
        for row in rows:
            value = row.get('result') if isinstance(row, dict) else None
            if issue_index and isinstance(value, dict) and value.get('artifact_id') == issue_index:
                query += '\n' + str(value.get('text', ''))
        catalogs, covered = _catalogs(run_id, rows, valid_page, query, profile)
        latest_review = current_review(run_id, context, rows, valid_page)
        packed, emitted, actions = [], set(), []
        for index, row in enumerate(rows):
            if index in covered:
                catalog_index = covered[index]
                if catalog_index not in emitted:
                    emitted.add(catalog_index)
                    packed.append({'catalog_ref': f'/context/evidence_catalogs/{catalog_index}',
                                   'kind': 'archived_catalog_receipts', 'primary_source_reading': False})
                continue
            if not isinstance(row, dict):
                packed.append(copy.deepcopy(row))
                continue
            projected = source_page(row, valid_page)
            if projected is not None:
                # The v3 lifecycle already selected committed cards. A new or
                # explicitly reopened page must not be hidden by the legacy pass.
                digest = None if working_set else (evidence_memory or {}).get(page_key(row))
                rejected = False
                if digest:
                    try:
                        digest = protect_card(digest, projected['result'])
                    except (ValueError, TypeError, KeyError):
                        # An invalid cached extraction must never hide raw evidence.
                        digest, rejected = None, True
                not_smaller = bool(digest and len(encoded(digest)) >= len(encoded(projected['result']['text'])))
                if not_smaller:
                    digest = None
                if digest:
                    page = projected['result']
                    page.pop('text')
                    page.update(evidence_card=copy.deepcopy(digest), primary_text_displayed=False,
                        text_policy='Exact page text remains in the source archive and original tool receipt. '
                        'Anchored quotations and protected numerical/conditional/adverse source spans are displayed. Findings are a model extraction, '
                        'not independent verification, a complete reading certificate or a novelty decision.')
                packed.append(projected)
                actions.append({'receipt_index': index, 'kind': 'source_metadata', 'text_preserved': not bool(digest),
                                'evidence_card': bool(digest), 'invalid_card_rejected': rejected,
                                'card_not_smaller': not_smaller})
                continue
            projected = copy.deepcopy(row)
            name = row.get('call', {}).get('name')
            value = row.get('result')
            if name in {'literature.search', 'web.search'} and isinstance(value, list):
                projected['result'] = [paper_card(paper) for paper in value]
            elif name == 'literature.verify' and isinstance(value, dict) and not value.get('error'):
                projected['result'] = paper_card(value)
            elif name == 'sources.list' and isinstance(value, list):
                from .context_navigation import source_directory_entry
                projected['result'] = [source_directory_entry(item, run_id) for item in value]
            packed.append(projected)
        if catalogs:
            context['evidence_catalogs'] = catalogs
        if 'tool_results' in context:
            context['tool_results'] = packed
        share_source_metadata(context)
        # Keep all unresolved review turns and the two newest other deliveries.
        history, optional = result.get('recent_turns', []), []
        retained, superseded = [], []
        for turn in reversed(history):
            scope = turn.get('scope', {}) if isinstance(turn, dict) else {}
            value = turn.get('content', {}) if isinstance(turn, dict) else {}
            # A replacement gate can supersede decisions, never the underlying
            # measurements, limitations or negative findings in an older review.
            scientific_facts = protected({k: v for k, v in value.items() if k not in {
                'approve', 'approved', 'required_changes', 'unresolved', 'unresolved_issues', 'open_questions', 'gaps'}},
                exact=True) if isinstance(value, dict) else False
            if (latest_review and scope.get('proposal_hash') == latest_review['proposal_hash']
                    and str(scope.get('purpose', '')).startswith('innovation_review:') and not scientific_facts):
                superseded.append(turn.get('id'))
                continue
            mandatory = protected(value, exact=True) or (isinstance(value, dict)
                and value.get('historical_response_directory') is True)
            if mandatory or len(optional) < 2:
                retained.append(turn)
                if not mandatory:
                    optional.append(turn)
        result['recent_turns'] = list(reversed(retained))
        manifest = {'version': 3 if working_set else VERSION, 'profile': profile.name, 'target_tokens': min(profile.target, ceiling),
            'reasoning_effort': effort, 'compression_classes': CLASSES,
            'ceiling_tokens': ceiling, 'budget_policy': 'configured_capacity_v2',
            'reduction_trigger_tokens': min(profile.reduction_trigger, ceiling),
            'source_context_sha256': fingerprint(original),
            'catalog_count': len(catalogs), 'catalog_page_receipts': len(covered),
            'source_metadata_projections': actions,
            'history_retained': len(retained), 'history_archived': len(history)-len(retained),
            'historical_reviews_replaced_by_current_gate': superseded,
            'policy': 'Current primary pages remain as exact text or validated anchored evidence cards; '
                      'failures, proposals and explicit unresolved judgments remain. '
                      'Catalog cards are historical findings; undisplayed details remain retrievable. '
                      'Packing targets and reduction triggers are soft; only configured/provider capacity is a hard input limit. '
                      'Input capacity never implies evidence approval or permission to run experiments.'}
        from .code_review_context import directory_view
        result['context'] = directory_view(result['context'])
        result['context_compilation'] = manifest
        if working_set:
            manifest.update(working_set_actions=working_actions, working_set_budget_version=3,
                working_set_slots=working_set.get('selection_budget', {}), budget_policy='configured_capacity_working_set_v3',
                recompile_passes=working_set.get('_recompile_pass', 0),
                compression_candidates_evaluated=working_set.get('_compression_candidates', 1),
                minimization_policy='Choose the smallest estimated wire input from original-record views and lossless packing, even below the soft target.',
                slot_targets={key: int(profile.target * share) for key, share in {
                    'system_rules': .075, 'current_task': .05, 'research_state': .125,
                    'recent_reasoning': .10, 'relevant_evidence': .35, 'counterevidence': .15,
                    'exact_excerpts': .10, 'safety_reserve': .05}.items()},
                slot_policy='Allocation targets are advisory. Mandatory evidence and constraints may borrow detail capacity; no slot target permits dropping counterevidence or gates.',
                capacity_ceiling_tokens=ceiling, task_ceiling_enforced=False,
                recompile_trigger_tokens=min(int(profile.target * 1.2), ceiling),
                routing_policy='Minimize from committed records on every compilation. Only configured/model input capacity blocks dispatch; if mandatory evidence exceeds it, narrow or split the task. Never trim gates or contrary facts.')
            manifest['reduction_trigger_tokens'] = min(manifest['recompile_trigger_tokens'], ceiling)
            manifest['policy'] = ('Fresh primary pages stay exact or become source-validated anchored cards. '
                'Committed historical records contribute exact facts, conditions and objections plus task-selected prose; originals remain expandable. '
                'Packing targets are advisory; only configured/model input capacity blocks oversized dispatch. '
                'Undisplayed material must be expanded before decisive judgments; no reading, consensus or execution gate is relaxed.')
        if image_tokens:
            manifest['image_tokens_allowance'] = image_tokens
        manifest['input_tokens'] = 0
        if working_set:
            # Full diagnostics belong in the saved snapshot, not every model
            # input. Keep policy and preflight fields visible and uninterned.
            result['context_compilation'] = {key: manifest[key] for key in (
                'version', 'profile', 'reasoning_effort', 'target_tokens', 'ceiling_tokens',
                'budget_policy', 'task_ceiling_enforced', 'reduction_trigger_tokens',
                'source_context_sha256', 'policy', 'input_tokens')}
            manifest['diagnostics_stored_outside_model_input'] = True
        def measure():
            for _ in range(3):
                manifest['input_tokens'] = estimate(system + encoded(result)) + image_tokens
                manifest['target_met'] = manifest['input_tokens'] <= manifest['target_tokens']
                result['context_compilation'].update(input_tokens=manifest['input_tokens'],
                                                     target_met=manifest['target_met'])
        measure()
        if working_set or manifest['input_tokens'] > manifest['target_tokens']:
            from .context_navigation import source_directory_entry
            evidence = result.get('context', {}).get('evidence')
            if isinstance(evidence, dict) and isinstance(evidence.get('sources'), list):
                # Known uninspected links are navigation, not scientific evidence.
                # They retain hashes and the existing versioned sources.links tool.
                evidence['sources'] = [source_directory_entry(row, run_id) for row in evidence['sources']]
            interned = exact_text_table(result, run_id, valid_page)
            if estimate(system + encoded(interned)) < estimate(system + encoded(result)):
                result = interned
            manifest['exact_text_table'] = 'shared_context_text' in result
            interned = exact_catalog_table(result)
            if estimate(system + encoded(interned)) < estimate(system + encoded(result)):
                result = interned
            manifest['exact_catalog_record_table'] = 'shared_context_catalog_records' in result
            from .context_records import pack as pack_records
            interned = pack_records(result)
            if estimate(system + encoded(interned)) < estimate(system + encoded(result)):
                result = interned
            manifest['exact_record_table'] = 'shared_context_records' in result
            measure()
        if manifest['input_tokens'] > ceiling:
            raise ContextBudgetExceeded(manifest)
        return result, manifest
