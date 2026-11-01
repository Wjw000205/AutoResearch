"""Exhaustive, non-executing checks of generated YAML review metadata.

This verifies data consistency and examples with the host JSON Schema validator;
it does not execute the candidate validator, models, optimizers or toy checks.
"""
import ast
from collections import Counter
import hashlib
import json

from jsonschema import Draft202012Validator, validators

from .experiment_workflow import sha

GENERATED = {'_delivery/constraint_map.json', '_delivery/schema_examples.json',
             '_delivery/schema_inventory.json'}
StrictValidator = validators.extend(Draft202012Validator, type_checker=
    Draft202012Validator.TYPE_CHECKER.redefine('integer', lambda _, value: type(value) is int))


def audit(files):
    def require(ok, message):
        if not ok:
            raise ValueError('Generated review metadata: ' + message)
    def decode(name):
        def pairs(items):
            result = {}
            for key, value in items:
                require(key not in result, 'duplicate JSON key in ' + name)
                result[key] = value
            return result
        def nonfinite(value):
            raise ValueError('Non-finite review JSON: ' + value)
        return json.loads(files[name], object_pairs_hook=pairs, parse_constant=nonfinite)
    schema_name = 'configs/hyperparameters.schema.json'
    schema = decode(schema_name)
    schema_sha = hashlib.sha256(files[schema_name]).hexdigest()
    inventory = decode('_delivery/schema_inventory.json')
    mapping = decode('_delivery/constraint_map.json')
    examples = decode('_delivery/schema_examples.json')
    order = decode('work_order.json')
    for data in (mapping, examples):
        require(data['work_order_sha256'] == hashlib.sha256(files['work_order.json']).hexdigest()
                and data['repair_source'] == order['repair_source'], 'work order/source identity')
    require(all(d['schema_sha256'] == schema_sha for d in (inventory, mapping, examples)), 'schema identity')
    require(mapping['inventory_sha256'] == hashlib.sha256(files['_delivery/schema_inventory.json']).hexdigest(),
            'inventory identity')
    nodes = {}
    def walk(node, path='$'):
        require(type(node) in (dict, bool), 'non-schema node at ' + path)
        nodes[path] = node
        if type(node) is bool:
            return
        require(not any(k in node for k in ('$ref', '$dynamicRef')), 'unresolved schema references')
        for key in ('properties', 'patternProperties', '$defs', 'definitions'):
            for name, value in node.get(key, {}).items():
                walk(value, path + '/' + key + '/' + name)
        for key in ('items', 'additionalProperties', 'if', 'then', 'else', 'not', 'contains'):
            if key in node:
                walk(node[key], path + '/' + key)
        for key in ('allOf', 'anyOf', 'oneOf', 'prefixItems'):
            for i, value in enumerate(node.get(key, [])):
                walk(value, path + '/' + key + '/' + str(i))
    walk(schema)
    expected_nodes = {path: {'path': path, 'keywords': sorted(node) if isinstance(node, dict) else [],
                      **({'boolean_schema': node} if type(node) is bool else {})} for path,node in nodes.items()}
    require(len(inventory['nodes']) == len(nodes)
            and {n['path']: n for n in inventory['nodes']} == expected_nodes, 'schema node coverage')
    expected = {(path, key): value for path,node in nodes.items()
                for key,value in (node.items() if type(node) is dict else [('$boolean', node)])}
    expected.update({(path, '$audit_unknown'): 'reject unknown keywords even in unselected branches' for path in nodes})
    constraints = inventory['constraints']
    require(len(constraints) == len(expected)
            and {(r['schema_path'], r['keyword']): r['constraint'] for r in constraints} == expected,
            'constraint coverage/value mismatch')
    require(len(mapping['constraints']) == len(constraints), 'constraint mapping count')
    symbols = {}
    def check_ref(ref):
        path = ref['path']
        require(path in files, 'unavailable source reference ' + path)
        if path not in symbols:
            symbols[path] = {(n.name, n.lineno, n.end_lineno) for n in ast.walk(ast.parse(files[path]))
                             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        require(ref['source_sha256'] == hashlib.sha256(files[path]).hexdigest()
                and (ref['symbol'], ref['line'], ref['end_line']) in symbols[path], 'source symbol/hash mismatch')
    tests = set()
    for i, (row, mapped) in enumerate(zip(constraints, mapping['constraints'])):
        require(mapped['inventory_index'] == i and all(mapped[k] == row[k] for k in ('schema_path','keyword','tests')),
                'mapping differs from inventory')
        require(row['tests'] and len(set(row['tests'])) == len(row['tests'])
                and mapped['example_keys'] == row['tests'] and mapped['examples_file'] == '_delivery/schema_examples.json',
                'missing/duplicate example references')
        require(mapped['behavior_status'] == 'not_run' and mapped['independent_review'] == 'pending',
                'metadata claims unexecuted behavior passed')
        for field in ('validator', 'test_source', 'example_generator'):
            check_ref(mapped[field])
        tests.update(row['tests'])
    cases = examples['cases']
    require(set(cases) == tests, 'missing or extra example cases')
    for row in constraints:
        keyword=row['keyword'];node=nodes[row['schema_path']]
        if keyword in ('$schema','$comment','$audit_unknown'):
            continue
        fragment=(node if keyword=='$boolean' else {k:node[k] for k in ('if','then')}
                  if keyword in ('if','then') else {keyword:node[keyword]})
        for identity in row['tests']:
            require(sha(cases[identity].get('fragment'))==sha(fragment),
                    'example fragment is not the actual constraint: ' + identity)
    checked, audit_cases = 0, 0
    for identity, case in cases.items():
        require(case['status'] == 'not_run', 'example claims candidate execution')
        require(identity.split('::')[0] in nodes, 'example node does not exist')
        if case['validator'] == 'validate_schema':
            fragment = case['fragment']
            # These bounded schemas cannot acquire network access.
            def no_refs(value):
                if isinstance(value, dict):
                    require(not any(k in value for k in ('$ref','$dynamicRef')), 'example reference')
                    for v in value.values(): no_refs(v)
                elif isinstance(value,list):
                    for v in value: no_refs(v)
            no_refs(fragment)
            Draft202012Validator.check_schema(fragment)
            validator = StrictValidator(fragment)
            require(case['accepted_examples'] or case['rejected_examples'], 'empty examples')
            for value in case['accepted_examples']:
                require(validator.is_valid(value), 'invalid positive example ' + identity)
                checked += 1
            for value in case['rejected_examples']:
                require(not validator.is_valid(value), 'valid negative example ' + identity)
                checked += 1
        else:
            require(case['validator'] == 'audit_schema', 'unknown example validator')
            if identity.endswith('::audit:unknown'):
                recipe = case['rejected_schema_recipe']
                node = nodes[identity.split('::')[0]]
                expected_node = dict(node, unsupported_keyword=True) if isinstance(node, dict) else {'unsupported_keyword': True}
                require(recipe == {'replace_node': identity.split('::')[0], 'with': expected_node}
                        and case['accepted_schema'] == schema_name, 'unknown-keyword recipe mismatch')
            else:
                path, keyword = identity.split('::audit:')
                require(keyword in ('$schema','$comment') and case['accepted_examples'] == [{keyword:nodes[path][keyword]}]
                        and case['rejected_examples'] == [{keyword:1}], 'annotation type examples')
            audit_cases += 1
    return {'version': 1, 'provenance': 'host_static_json_consistency_audit',
        'files': {n: {'sha256':hashlib.sha256(files[n]).hexdigest(),'bytes':len(files[n])} for n in sorted(GENERATED)},
        'schema_sha256':schema_sha, 'schema_nodes':len(nodes), 'constraints':len(constraints),
        'example_cases':len(cases), 'host_example_values_checked':checked, 'annotation_and_unknown_cases':audit_cases,
        'integer_semantics':'Explicit integer type (1.0 is rejected), matching the declared strict configuration contract',
        'constraint_keyword_counts':dict(sorted(Counter(r['keyword'] for r in constraints).items())),
        'all_constraints_mapped':True, 'all_source_references_verified':True,
        'candidate_validator_executed':False, 'model_or_optimizer_executed':False,
        'independent_approval':False, 'scientific_approval':False,
        'review_requirement':'Review the complete schema, generator, candidate validator and test source. Raw mapped JSON remains available for targeted inspection. This static audit never replaces candidate CPU behavior checks.'}
