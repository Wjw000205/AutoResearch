"""Uncapped response JSON strings; identity, structure and value checks remain.

Transform schema keywords only, never frozen requests or literal schema data.
"""
import copy

from jsonschema import Draft202012Validator, validate as strict_validate

POLICY = 'all-response-json-strings-without-character-caps-v2'


def applicable(schema):
    return isinstance(schema, dict)


def relax(schema):
    """Walk schema keywords only; never rewrite const/default/example values."""
    if not isinstance(schema, dict):return copy.deepcopy(schema)
    result=copy.deepcopy(schema)
    result.pop('maxLength',None)
    for key in ('properties','patternProperties','$defs','definitions','dependentSchemas'):
        if isinstance(schema.get(key),dict):
            result[key]={name:relax(child) for name,child in schema[key].items()}
    for key in ('items','additionalItems','additionalProperties','unevaluatedProperties',
                'unevaluatedItems','contains','propertyNames','contentSchema','if','then','else','not'):
        if isinstance(schema.get(key),(dict,bool)):result[key]=relax(schema[key])
        elif key=='items' and isinstance(schema.get(key),list):result[key]=[relax(child) for child in schema[key]]
    for key in ('anyOf','oneOf','allOf','prefixItems'):
        if isinstance(schema.get(key),list):result[key]=[relax(child) for child in schema[key]]
    if isinstance(schema.get('dependencies'),dict):
        result['dependencies']={name:relax(child) if isinstance(child,(dict,bool)) else copy.deepcopy(child)
                                for name,child in schema['dependencies'].items()}
    return result


def validator(schema, *args, **kwargs):
    return Draft202012Validator(relax(schema),*args,**kwargs)


def validate(instance, schema, *args, **kwargs):
    return strict_validate(instance, relax(schema), *args, **kwargs)
