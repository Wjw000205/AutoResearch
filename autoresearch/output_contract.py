"""Shared response contracts and a concise pre-submission checklist."""

READING_PLAN_SCHEMA = {
    'type': 'object',
    'properties': {
        'core_paper_ids': {'type': 'array', 'items': {'type': 'string'}, 'uniqueItems': True},
        'pending_paper_ids': {'type': 'array', 'items': {'type': 'string'}, 'uniqueItems': True},
        'selection_rationale': {'type': 'string', 'minLength': 1},
    },
    'required': ['core_paper_ids', 'pending_paper_ids', 'selection_rationale'],
    'additionalProperties': False,
}

LITERATURE_READING_RESULT_SCHEMA = {
    'type': 'object',
    'properties': {'summary': {'type': 'string'}, 'reading_plan': READING_PLAN_SCHEMA},
    'required': ['summary', 'reading_plan'],
    'additionalProperties': False,
}

OUTPUT_CHECKLIST = """
OUTPUT SELF-CHECK: Before submitting, silently verify your entire response against the required JSON Schema.
1. Emit exactly one complete JSON object. Keep every field inside the same root; no Markdown, prose suffix,
   detached fields, duplicate keys, trailing commas, NaN/Infinity or unescaped quotes/backslashes.
2. Choose either the final-result branch or the allowed tool_calls branch; never combine them.
3. Check ALL required fields recursively, types, enums, bounds, uniqueItems and additionalProperties.
   A reading result requires both summary and reading_plan; reading_plan requires core_paper_ids,
   pending_paper_ids and selection_rationale. Do not close the root after summary.
4. Use only exact supplied/verified paper IDs and tool-returned source/citation IDs. Check saved tool receipts;
   downloading/indexing is not reading, and a failed note write is not a saved note.
5. Never fabricate content to satisfy a field. Use empty arrays only when permitted and factually correct;
   explain unavailable evidence in the schema's limitation fields. Preserve scientific and resource gates.
6. Correct any failed check before sending. Do not add a self_check field or print this checklist.
""".strip()
