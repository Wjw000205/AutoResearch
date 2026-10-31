"""Conservative domain anchors; a venue match is never a relevance match."""
import re

VERSION = 3


def text(paper):
    return ' '.join(str(paper.get(k) or '') for k in ('title', 'abstract', 'abstract_excerpt'))


def domain_match(query, paper):
    # Quoted subject phrases come from task intake; synonyms are alternatives.
    anchors = re.findall(r'"([^"\n]{1,100})"', str(query))
    normalize = lambda value: re.sub(r'[\s_-]+', ' ', value.casefold()).strip()
    content = normalize(text(paper))
    return not anchors or any(normalize(anchor) in content for anchor in anchors)


def subject_query(query):
    """A recall query drops facet conjunctions, never the research domain."""
    phrases = re.findall(r'"([^"\n]{1,100})"', query)
    return phrases[0] if phrases else query


def task_query(engine, run_id):
    run = engine.store.get('runs', run_id)
    from .literature_rounds import enabled, phase
    if not enabled(run) or phase(run) != 'idea':
        return ''
    intake = engine.outputs(run_id).get('intake', {})
    terms = intake.get('domain_terms')
    if not isinstance(terms, list):
        terms = [intake['domain']] if intake.get('domain') else []
    return ' '.join('"' + term.replace('"', '') + '"' for term in terms[:12]
                    if isinstance(term, str) and 0 < len(term.strip()) <= 100)


def filter_task(engine, run_id, papers):
    query = task_query(engine, run_id)
    from .reference_papers import is_uploaded_paper
    return [p for p in papers if is_uploaded_paper(engine.store, run_id, p) or domain_match(query, p)]
