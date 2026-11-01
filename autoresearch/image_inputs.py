"""Task-owned, hash-bound PNG inputs shared by API and native transports."""
from __future__ import annotations

import base64
import hashlib
import io
import math
from pathlib import Path


def resolve(store, run_id, context):
    from PIL import Image
    from .evidence_tools import _plain_path
    refs = context.get('_image_inputs', [])
    if not isinstance(refs, list) or len(refs) > 2:
        raise ValueError('At most two trusted page images per request')
    images = []
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {'artifact_id', 'sha256'}:
            raise ValueError('Image inputs require an artifact ID and SHA256')
        artifact = store.get('artifacts', ref['artifact_id'], {})
        if artifact.get('run_id') != run_id or artifact.get('kind') != 'paper_page_image' or artifact.get('sha256') != ref['sha256']:
            raise ValueError('Image artifact ownership or hash mismatch')
        path = Path(artifact['path'])
        root = (store.path.parent / 'runs' / run_id).resolve()
        _plain_path(path, root)
        if not path.resolve().is_relative_to(root) or path.stat().st_size > 8 * 1024**2:
            raise ValueError('Image path or size exceeds task scope')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != ref['sha256']:
            raise ValueError('Image bytes hash mismatch')
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if image.format != 'PNG' or max(width, height) > 2048 or width * height > 4_194_304:
                raise ValueError('Image format or pixel budget exceeded')
            image.verify()
        # Model-independent conservative allowance, never an official price or
        # exact tokenizer claim. Provider-reported usage settles actual billing.
        allowance = max(4096, math.ceil(width/32)*math.ceil(height/32)*4)
        images.append({'artifact_id': ref['artifact_id'], 'sha256': ref['sha256'],
            'data': data, 'path': str(path), 'width': width, 'height': height, 'tokens': allowance})
    return images


def tokens(images):
    return sum(image['tokens'] for image in images)


def content(protocol, user, images):
    if not images:
        return user
    blocks = []
    for image in images:
        data = base64.b64encode(image['data']).decode('ascii')
        url = 'data:image/png;base64,' + data
        if protocol == 'anthropic':
            blocks.append({'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': data}})
        elif protocol == 'responses':
            blocks.append({'type': 'input_image', 'image_url': url, 'detail': 'high'})
        else:
            blocks.append({'type': 'image_url', 'image_url': {'url': url, 'detail': 'high'}})
    blocks.append({'type': 'input_text' if protocol == 'responses' else 'text', 'text': user})
    return blocks
