"""Difference checks of deadline helpers, with a controlled clock and no probes.

Only the delivered stdlib-only deadline functions are loaded from their AST.
The research suite, check_delivery body, imports and module entrypoint never run.
These are unit-contract observations, not a new shared-60s research test batch.
"""
from __future__ import annotations

import ast
import hashlib
import math
from pathlib import Path
from types import SimpleNamespace


def check(path, *, deadline_environment_variable=None):
    from .code_verification import deadline_variable
    deadline_name = deadline_variable(path, deadline_environment_variable)
    raw = Path(path).read_bytes()
    tree = ast.parse(raw)
    names = {'remaining_time', 'run_before_deadline', 'main'}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    if {n.name for n in nodes} != names:
        raise ValueError('Delivered deadline helpers are missing')
    clock = [100.0]
    env, saved, calls = {}, [], []
    subprocess = SimpleNamespace()
    scope = {'time': SimpleNamespace(monotonic=lambda: clock[0]), 'math': math,
             'os': SimpleNamespace(environ=env), 'subprocess': subprocess,
             'save_progress': lambda p: saved.append(dict(p))}
    def suite(started, deadline, progress):
        calls.append((started, deadline))
        return 0
    scope['check_delivery'] = suite
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), scope)
    results = []
    def expect_error(fn, cls):
        try:
            fn()
        except cls:
            return
        raise AssertionError('Expected '+cls.__name__)
    def case(name, fn):
        try:
            fn()
            results.append({'name': name, 'passed': True})
        except Exception as exc:
            results.append({'name': name, 'passed': False, 'error': type(exc).__name__+': '+str(exc)})
    def expired():
        env[deadline_name] = '99'
        calls.clear()
        expect_error(scope['main'], TimeoutError)
        assert not calls and saved[-1]['passed'] is False
    def invalid():
        for value in ('nan', 'inf', '-inf', 'invalid'):
            env[deadline_name] = value
            calls.clear()
            expect_error(scope['main'], ValueError)
            assert not calls and saved[-1]['passed'] is False
    def inherited():
        env[deadline_name] = '105'
        calls.clear()
        scope['main']()
        assert calls == [(100.0, 105.0)] and saved[-1]['check_deadline'] == 105.0
    def child():
        calls.clear()
        subprocess.run = lambda command, **kw: calls.append(kw) or 'done'
        original = {deadline_name:'999', 'EXAMPLE':'kept'}
        assert scope['run_before_deadline'](['unused'], deadline=105, local_limit=20, env=original) == 'done'
        assert calls[0]['timeout'] == 5 and calls[0]['env'][deadline_name] == '105'
        assert original[deadline_name] == '999'
    def child_overrun():
        def fake(command, **kw):
            clock[0] = 105.0
            return SimpleNamespace(returncode=0)
        subprocess.run = fake
        try:
            expect_error(lambda: scope['run_before_deadline'](['unused'], deadline=105, local_limit=60, env={}), TimeoutError)
        finally:
            clock[0] = 100.0
    def no_spawn_expired():
        calls.clear()
        subprocess.run = lambda *a, **kw: calls.append(kw)
        expect_error(lambda: scope['run_before_deadline'](['unused'], deadline=99, local_limit=60, env={}), TimeoutError)
        assert not calls
    for name, fn in [('inherited_deadline_not_reset',inherited), ('expired_before_first_stage',expired),
                     ('invalid_deadlines_fail_closed',invalid), ('first_child_inherits_and_clamps',child),
                     ('zero_exit_after_deadline_rejected',child_overrun), ('expired_does_not_spawn',no_spawn_expired)]:
        case(name, fn)
    return {'kind':'deadline_difference_unit_checks', 'source_sha256':hashlib.sha256(raw).hexdigest(),
            'passed':all(r['passed'] for r in results), 'cases':results,
            'provenance':'actual delivered function bodies; controlled clock and subprocess doubles',
            'research_suite_executed':False, 'training_executed':False,
            'original_probe_executed':False, 'shared_allowance_reset':False,
            'formal_acceptance':False}
