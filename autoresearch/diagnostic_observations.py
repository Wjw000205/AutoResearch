"""Capture bounded numeric observations on stdout, independently of the exit status."""
import json
import math
import re

PREFIX = b'CPU_OBSERVATION '
FIELD = 'required_cpu_observations'
NAME_PATTERN = r'^[a-zA-Z][a-zA-Z0-9_]{0,95}(?![\s\S])'


def names(value):
    if (not isinstance(value, list) or not 1 <= len(value) <= 256
            or any(not isinstance(k, str) or not re.fullmatch(NAME_PATTERN, k) for k in value)
            or len(set(value)) != len(value)):
        raise ValueError('Diagnostic observations require unique numeric measurement names')
    return value


class Capture:
    def __init__(self, required):
        self.required = set(names(required))
        self.values = {}
        self.pending = b''
        self.error = None

    def feed(self, chunk):
        lines = (self.pending + chunk).split(b'\n')
        self.pending = lines.pop()
        for line in lines:
            if not line.startswith(PREFIX):
                continue
            try:
                name, value = json.loads(line[len(PREFIX):])
                if name not in self.required or type(value) not in (int, float) or not math.isfinite(value):
                    raise ValueError
                self.values[name] = value
            except (TypeError, ValueError, OverflowError):
                self.error = 'Invalid diagnostic observation record'

    def verify(self, metrics):
        reported = {k.removeprefix('cpu_observation.'): v for k, v in metrics.items() if k.startswith('cpu_observation.')}
        if self.error or self.values.keys() != self.required or reported != self.values:
            raise ValueError(self.error or 'Final metrics differ from the complete declared diagnostic observations')
