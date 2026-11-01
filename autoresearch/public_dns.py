"""Resolve TUN synthetic DNS answers without allowing reserved-address fetches."""
from __future__ import annotations

import ipaddress

import httpx

TUN_RANGES = (ipaddress.ip_network('198.18.0.0/15'), ipaddress.ip_network('fdfe:dcba:9876::/48'))


def synthetic_answers(host, addresses):
    try:
        ipaddress.ip_address(host)
        return False  # Literal IPs can never opt into the domain fallback.
    except ValueError:
        pass
    if not addresses:
        return False
    ips = [ipaddress.ip_address(address.split('%')[0]) for address in addresses]
    return any(not ip.is_global for ip in ips) and all(
        ip.is_global or any(ip in net for net in TUN_RANGES if ip.version == net.version) for ip in ips)


async def resolve_public(host, *, transport=None):
    """Fixed HTTPS resolver, strict question/answer validation, bounded transfer.

    The subsequent paper connection still pins the validated public address and
    verifies TLS against the original hostname on every redirect.
    """
    host = host.rstrip('.').encode('idna').decode('ascii').lower()
    async with httpx.AsyncClient(transport=transport, timeout=12, trust_env=False, follow_redirects=False) as client:
        async with client.stream('GET', 'https://cloudflare-dns.com/dns-query',
                params={'name': host, 'type': 'A'}, headers={'Accept': 'application/dns-json'}) as response:
            response.raise_for_status()
            content = bytearray()
            async for block in response.aiter_bytes():
                content.extend(block)
                if len(content) > 65536:
                    raise ValueError('Public DNS response exceeds limit')
        import json
        payload = json.loads(content)
    questions = payload.get('Question', [])
    if (payload.get('Status') != 0 or not any(q.get('type') == 1
            and str(q.get('name', '')).rstrip('.').lower() == host for q in questions)):
        raise ValueError('Public DNS response does not match requested host')
    addresses = []
    names = {host}
    answers = payload.get('Answer', [])
    for _ in range(8):
        names.update(str(a.get('data', '')).rstrip('.').lower() for a in answers
            if a.get('type') == 5 and str(a.get('name', '')).rstrip('.').lower() in names)
    for answer in answers:
        if answer.get('type') != 1:
            continue
        if str(answer.get('name', '')).rstrip('.').lower() not in names:
            raise ValueError('Public DNS answer is unrelated to requested host')
        ip = ipaddress.ip_address(answer.get('data', ''))
        if ip.version != 4 or not ip.is_global:
            raise ValueError('Public DNS returned a nonpublic address')
        addresses.append(str(ip))
    if not addresses:
        raise ValueError('Public DNS returned no public addresses')
    return list(dict.fromkeys(addresses))
