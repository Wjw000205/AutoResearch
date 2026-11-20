"""Conservative domain anchors; a venue match is never a relevance match."""
import re

VERSION = 4


def text(paper):
    return ' '.join(str(paper.get(k) or '') for k in ('title', 'abstract', 'abstract_excerpt'))


def domain_match(query, paper):
    # Quoted subject phrases come from task intake; synonyms are alternatives.
    anchors = re.findall(r'"([^"\n]{1,100})"', str(query))
    if not anchors:
        subject = subject_query(str(query))
        if subject != str(query):
            anchors = [subject]
    normalize = lambda value: re.sub(r'[\s_-]+', ' ', value.casefold()).strip()
    content = normalize(text(paper))
    def scripts(value):
        return {name for name, pattern in [('latin', r'[a-zA-Z]'), ('han', r'[\u3400-\u9fff]')]
                if re.search(pattern, value)}
    # A missing translation is uncertainty, not evidence of domain mismatch.
    # Semantic relevance remains the Scout's screening responsibility.
    comparable = [anchor for anchor in anchors if scripts(anchor) & scripts(content)]
    return not comparable or any(normalize(anchor) in content for anchor in comparable)


def subject_query(query):
    """A recall query drops facet conjunctions, never the research domain."""
    phrases = re.findall(r'"([^"\n]{1,100})"', query)
    if phrases:
        return phrases[0]
    cleaned = re.sub(r'^\s*(?:(?:19|20)\d{2}\s+)+', '', query)
    subject = re.split(r'\b(?:AND|OR|NOT|foundation|models?|methods?|architecture|calibration|CCF)\b',
                       cleaned, maxsplit=1, flags=re.I)[0].strip(' ()')
    return subject or query


def task_query(engine, run_id):
    run = engine.store.get('runs', run_id)
    from .literature_rounds import enabled, phase
    if not enabled(run) or phase(run) != 'idea':
        return ''
    intake = engine.outputs(run_id).get('intake', {})
    terms = intake.get('domain_terms')
    if not isinstance(terms, list):
        terms = [intake['domain']] if intake.get('domain') else []
    terms = [term.strip() for term in terms if isinstance(term, str) and 0 < len(term.strip()) <= 100]
    # Use the task's own search-language subjects, never a host translation or
    # generic mechanism words such as "model" as substitutes for its domain.
    for query in intake.get('search_queries', []):
        if isinstance(query, str):
            phrases = re.findall(r'"([^"\n]{1,100})"', query)
            if phrases:
                terms.append(phrases[0])
    return ' '.join('"' + term.replace('"', '') + '"' for term in dict.fromkeys(terms))


def filter_task(engine, run_id, papers):
    query = task_query(engine, run_id)
    from .reference_papers import is_uploaded_paper
    return [p for p in papers if is_uploaded_paper(engine.store, run_id, p) or domain_match(query, p)]
