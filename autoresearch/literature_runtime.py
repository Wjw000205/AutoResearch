"""Optional, CPU-only paper models. Network downloads occur only in setup()."""
from __future__ import annotations

import hashlib
import json
import math
import threading
from pathlib import Path

MODEL = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
_LOCK = threading.RLock()
_MODELS = {}


class _MemoryEncoder:
    """Reuse the verified offline multilingual bundle and its token windows."""
    model_id = MODEL
    provenance = 'local_cpu'

    def __init__(self, data_dir):
        from tokenizers import Tokenizer
        with _LOCK:
            self.model, self.model_version = load(data_dir, 'embedding')
            self.tokenizer = Tokenizer.from_str(self.model.model.tokenizer.to_str())
            self.tokenizer.no_truncation()

    def vectors(self, text):
        with _LOCK:
            resource_gate()
            offsets = [(a, b) for a, b in self.tokenizer.encode(text).offsets if b > a]
            parts = [text[offsets[i][0]:offsets[min(i+479, len(offsets)-1)][1]]
                     for i in range(0, len(offsets), 440)] or ['']
            return [vector.tolist() for vector in self.model.embed(parts, batch_size=8)]


def memory_encoder(intelligence):
    """An explicit injectable adapter or existing local bundle; never a new URL.

    Adapters expose model_id, model_version, provenance and vectors(text).
    This permits an already-authorized embedding provider without automatically
    transmitting research text to any chat provider or downloading weights.
    """
    adapter = getattr(intelligence, 'embedding_backend', None)
    if adapter is None:
        return _MemoryEncoder(intelligence.store.path.parent)
    if not all(isinstance(getattr(adapter, field, None), str) and getattr(adapter, field)
               for field in ('model_id', 'model_version', 'provenance')) or not callable(getattr(adapter, 'vectors', None)):
        raise ValueError('Embedding adapter needs a pinned model ID/version and vectors(text)')
    return adapter


def _validated_vectors(vectors):
    values = [[float(value) for value in row] for row in vectors]
    if (not values or not values[0] or any(len(row) != len(values[0]) for row in values)
            or any(not math.isfinite(value) for row in values for value in row)):
        raise ValueError('Embedding vectors must be finite with consistent dimensions')
    return values


def cached_vectors(store, run_id, encoder, text):
    """Content SHA + pinned model version cache; no duplicate passage plaintext."""
    from .context_compiler import fingerprint
    from .storage import now
    content_sha = hashlib.sha256(text.encode('utf-8')).hexdigest()
    identity = fingerprint([encoder.model_id, encoder.model_version, content_sha])
    key = f'{run_id or "global"}:memory-semantic:{identity}'
    saved = store.get('semantic_vectors', key)
    if (saved and saved.get('content_sha256') == content_sha
            and saved.get('model_version') == encoder.model_version
            and saved.get('digest') == fingerprint(saved.get('vectors'))):
        return _validated_vectors(saved['vectors']), True
    values = _validated_vectors(encoder.vectors(text))
    store.put('semantic_vectors', key, {'id': key, 'run_id': run_id or 'global',
        'model': encoder.model_id, 'model_version': encoder.model_version,
        'content_sha256': content_sha, 'vectors': values, 'digest': fingerprint(values),
        'provenance': encoder.provenance, 'created_at': now()})
    return values, False


def cosine_score(query_vectors, passage_vectors):
    query, passage = _validated_vectors(query_vectors), _validated_vectors(passage_vectors)
    if len(query[0]) != len(passage[0]):
        raise ValueError('Query/passage embedding dimensions differ')
    def cosine(a, b):
        norm = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
        return sum(x*y for x, y in zip(a, b))/norm if norm else 0.
    return max(cosine(a, b) for a in query for b in passage)


def root(data_dir):
    return Path(data_dir).resolve() / 'models' / 'literature'


def resource_gate():
    import psutil
    if psutil.virtual_memory().available < 2 * 1024**3:
        raise ValueError('Local paper models require 2 GiB available RAM; deferred')


def ocr_params(directory):
    from rapidocr import OCRVersion, ModelType
    return {'Global.model_root_dir': str(directory), 'Global.log_level': 'error',
        'EngineConfig.onnxruntime.intra_op_num_threads': 2,
        'EngineConfig.onnxruntime.inter_op_num_threads': 1,
        'Det.ocr_version': OCRVersion.PPOCRV4, 'Det.model_type': ModelType.MOBILE,
        'Rec.ocr_version': OCRVersion.PPOCRV4, 'Rec.model_type': ModelType.MOBILE}


def setup(data_dir):
    """Explicit operator command; public weights only, no API credentials."""
    import shutil
    from fastembed import TextEmbedding
    from rapidocr import RapidOCR
    resource_gate()
    directory = root(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(directory).free < 2 * 1024**3:
        raise ValueError('Model setup requires 2 GiB free disk')
    embedding = TextEmbedding(MODEL, cache_dir=str(directory / 'embedding'), threads=2,
                              providers=['CPUExecutionProvider'])
    list(embedding.embed(['Local model initialization'], batch_size=1))
    RapidOCR(params=ocr_params(directory / 'ocr'))
    files = {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in directory.rglob('*') if p.is_file() and p.suffix in {'.onnx', '.json', '.txt'}
             and p.name != 'manifest.json'}
    manifest = {'version': 1, 'embedding_model': MODEL, 'files': files,
                'runtime': 'CPUExecutionProvider', 'threads': 2}
    temporary = directory / 'manifest.tmp'
    temporary.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    temporary.replace(directory / 'manifest.json')
    return {'ready': True, 'model': MODEL, 'files': len(files),
            'bytes': sum((directory / name).stat().st_size for name in files)}


def load(data_dir, kind):
    """Verify the pinned local bundle once per process; never auto-download."""
    directory = root(data_dir)
    manifest_path = directory / 'manifest.json'
    if not manifest_path.is_file():
        raise ValueError('Local paper models not installed; run python -m autoresearch.literature_runtime --setup DATA_DIR')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    fingerprint = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    key = (str(directory), fingerprint, kind)
    with _LOCK:
        if key in _MODELS:
            return _MODELS[key], fingerprint
        resource_gate()
        if manifest.get('embedding_model') != MODEL or not manifest.get('files'):
            raise ValueError('Invalid paper model manifest')
        for name, sha in manifest['files'].items():
            path = (directory / name).resolve()
            if not path.is_relative_to(directory) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                raise ValueError('Local paper model hash mismatch; setup must be repaired explicitly')
        if kind == 'embedding':
            from fastembed import TextEmbedding
            model = TextEmbedding(MODEL, cache_dir=str(directory / 'embedding'), threads=2,
                                  providers=['CPUExecutionProvider'], local_files_only=True)
        elif kind == 'ocr':
            from rapidocr import RapidOCR
            params = ocr_params(directory / 'ocr')
            for part in ('Det', 'Cls', 'Rec'):
                paths = [directory / name for name in manifest['files']
                         if name.startswith('ocr/') and name.endswith('.onnx') and f'_{part.lower()}' in name.lower()]
                if len(paths) != 1:
                    raise ValueError('OCR model bundle is incomplete')
                params[f'{part}.model_path'] = str(paths[0])
            model = RapidOCR(params=params)
        else:
            raise ValueError('Unknown local paper model kind')
        _MODELS[key] = model
        return model, fingerprint


def semantic_scores(engine, run_id, query, rows):
    """Persist source/hash/model-bound passage vectors; no text leaves this host."""
    import numpy as np
    from tokenizers import Tokenizer
    from .context_compiler import fingerprint
    from .storage import now
    with _LOCK:
        model, version = load(engine.store.path.parent, 'embedding')
        resource_gate()
        tokenizer = Tokenizer.from_str(model.model.tokenizer.to_str())
        tokenizer.no_truncation()

        def parts(text):
            offsets = [(a, b) for a, b in tokenizer.encode(text).offsets if b > a]
            return [text[offsets[i][0]:offsets[min(i+479, len(offsets)-1)][1]]
                    for i in range(0, len(offsets), 440)] or ['']

        def vectors(text):
            result = np.asarray(list(model.embed(parts(text), batch_size=8)), dtype=np.float32)
            return result / np.maximum(np.linalg.norm(result, axis=1, keepdims=True), 1e-12)

        q = vectors(query)
        scores, hits = [], 0
        for row in rows:
            if hasattr(engine, 'check_active'):
                engine.check_active(run_id)
            identity = fingerprint([version, row['source_id'], row['text_sha256'], row['start'], row['end'], row['quote']])
            key = f'{run_id}:semantic:{identity}'
            saved = engine.store.get('semantic_vectors', key)
            if saved and saved.get('digest') == fingerprint(saved.get('vectors')):
                vec = np.asarray(saved['vectors'], dtype=np.float32)
                hits += 1
            else:
                vec = vectors(row['quote'])
                values = vec.tolist()
                engine.store.put('semantic_vectors', key, {'id': key, 'run_id': run_id, 'model': MODEL,
                    'model_fingerprint': version, 'source_id': row['source_id'], 'text_sha256': row['text_sha256'],
                    'start': row['start'], 'end': row['end'], 'vectors': values, 'digest': fingerprint(values), 'created_at': now()})
            scores.append(float(np.max(q @ vec.T)))
        return scores, {'model': MODEL, 'model_fingerprint': version, 'runtime': 'local_cpu',
                        'cached_passages': hits, 'indexed_passages': len(rows), 'truncation': False}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--setup', required=True, type=Path)
    print(json.dumps(setup(parser.parse_args().setup)))
