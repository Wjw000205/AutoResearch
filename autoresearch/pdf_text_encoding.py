"""Make PDF extractor strings representable without changing original bytes."""
import re

_SURROGATES = re.compile(r'[\ud800-\udfff]')


def normalize(text):
    """Combine UTF-16 pairs and mark invalid code units with U+FFFD.

    Pypdf can return isolated surrogates for broken PDF font mappings. Keep
    their positions visible rather than dropping text or failing UTF-8 hashes.
    """
    count = len(_SURROGATES.findall(text))
    if not count:
        return text, 0, 0
    normalized = text.encode('utf-16-le', errors='surrogatepass').decode('utf-16-le', errors='replace')
    return normalized, count, normalized.count('\ufffd') - text.count('\ufffd')
