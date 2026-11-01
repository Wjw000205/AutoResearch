"""Read text, search pages and inspect images under the application's gates."""
from ...pdf_tools import LocalPDF
from .manifest import build


class CodexPDFReader(LocalPDF):
    def manifest(self, run_id, source_id, *, expected_sha256=None):
        document, _ = self.original(run_id, source_id)
        if expected_sha256 is not None and expected_sha256 != document['text_sha256']:
            raise ValueError('PDF text SHA256 does not match the requested snapshot')
        return build(self.engine, run_id, source_id)

    def info(self, run_id, source_id):
        info = super().info(run_id, source_id)
        manifest = self.manifest(run_id, source_id)
        return {**info, 'repository': {k: manifest[k] for k in ('artifact_id', 'manifest_path', 'version')},
                'manifest_tool': {'name': 'pdf.manifest', 'arguments': {'source_id': source_id}},
                'image_tool': {'name': 'pdf.render', 'arguments': {'source_id': source_id, 'page': 1,
                    'expected_sha256': info['text_sha256']}}}

    def render(self, run_id, source_id, page, *, expected_sha256, crop=None):
        from ...paper_media import render
        args = {'source_id': source_id, 'page': page, 'expected_sha256': expected_sha256}
        if crop is not None:
            args['crop'] = crop
        return render(self.engine, run_id, args)
