"""Records which proxy the Python SDK's transports pick for each case: httpx 0.28.1 (trust_env=True, the HTTP client)
and websockets 17.3 (proxy=True, the channel). Run from the jeff-dev checkout:
    uv run python <this file> > packages/rust/tests/data/proxy_cases.json
Each case: an environment (only these variables set), a server URL, and per transport the proxy picked (its
http://host:port and Basic authorization), null for a direct connection, or "error"."""
import json
import os
import sys
from unittest import mock

import httpx
from websockets.proxy import get_proxy, parse_proxy
from websockets.uri import parse_uri

URLS = ["http://api.example.com", "https://api.example.com", "http://api.example.com:8080/jeff",
        "https://www.example.com:443", "http://example.com", "http://wwwexample.com", "http://localhost:9000",
        "http://127.0.0.1:9000", "http://[::1]:9000", "https://deep.sub.example.org", "http://API.Example.COM:80"]

ENVS = [
    {},
    {"HTTP_PROXY": "http://proxy:3128"},
    {"http_proxy": "http://lower:3128", "HTTP_PROXY": "http://upper:3128"},
    {"HTTPS_PROXY": "http://secure:3129"},
    {"HTTP_PROXY": "http://plain:3128", "HTTPS_PROXY": "http://secure:3129"},
    {"ALL_PROXY": "http://all:3130"},
    {"ALL_PROXY": "http://all:3130", "HTTP_PROXY": "http://plain:3128"},
    {"HTTP_PROXY": "proxy:3128"},
    {"HTTP_PROXY": "http://alice:s3cret@proxy:3128"},
    {"HTTPS_PROXY": "http://alice:s3%40cret@proxy:3128/"},
    {"HTTP_PROXY": "http://alice@proxy:3128"},
    {"HTTP_PROXY": "http://proxy"},
    {"HTTP_PROXY": "http://proxy:3128", "NO_PROXY": "*"},
    {"HTTP_PROXY": "http://proxy:3128", "NO_PROXY": "example.com"},
    {"HTTP_PROXY": "http://proxy:3128", "NO_PROXY": ".example.com"},
    {"HTTP_PROXY": "http://proxy:3128", "HTTPS_PROXY": "http://proxy:3128", "NO_PROXY": "api.example.com"},
    {"HTTP_PROXY": "http://proxy:3128", "NO_PROXY": "localhost,127.0.0.1,::1"},
    {"HTTP_PROXY": "http://proxy:3128", "no_proxy": "  example.org , localhost "},
    {"HTTP_PROXY": "http://proxy:3128", "NO_PROXY": "http://api.example.com:8080"},
    {"HTTP_PROXY": "http://proxy:3128", "NO_PROXY": "example.com:8080"},
    {"HTTP_PROXY": "http://proxy:3128", "NO_PROXY": "127.0.0.0/8"},
    {"HTTP_PROXY": "http://proxy:3128", "NO_PROXY": "EXAMPLE.COM"},
    {"HTTP_PROXY": "http://proxy:3128", "NO_PROXY": "*,example.com"},
    {"HTTP_PROXY": "http://proxy:3128", "NO_PROXY": ""},
    {"HTTP_PROXY": "http://proxy:3128", "no_proxy": "", "NO_PROXY": "example.com"},
    {"HTTP_PROXY": "http://proxy:3128", "REQUEST_METHOD": "GET"},
    {"http_proxy": "http://lower:3128", "REQUEST_METHOD": "GET"},
    {"HTTPS_PROXY": "http://upper:3129", "https_proxy": ""},
    {"WS_PROXY": "http://ws:3131", "HTTP_PROXY": "http://plain:3128"},
    {"WSS_PROXY": "http://wss:3132", "HTTPS_PROXY": "http://secure:3129"},
    {"SOCKS_PROXY": "http://socks:1080"},
    {"HTTPS_PROXY": "socks5://socks:1080"},
    {"HTTP_PROXY": "https://tls-proxy:3128"},
    {"Http_Proxy": "http://mixed:3128"},
    {"HTTP_PROXY": "http://proxy:3128", "NO_PROXY": "wwwexample.com"},
    {"HTTP_PROXY": "http://proxy:3128", "NO_PROXY": "deep.sub.example.org", "HTTPS_PROXY": "http://s:1"},
    {"HTTP_PROXY": "http://proxy:3128", "NO_PROXY": "sub.example.org", "HTTPS_PROXY": "http://s:1"},
]


def httpx_pick(url: str):
    client = httpx.Client(trust_env=True)
    transport = client._transport_for_url(httpx.URL(url))
    if transport is client._transport:
        return None
    pool = transport._pool
    proxy_url = pool._proxy_url
    auth = dict(pool._proxy_headers).get(b"Proxy-Authorization")
    host = proxy_url.host.decode()
    return {"url": f"{proxy_url.scheme.decode()}://{host}:{proxy_url.port or 80}",
            "authorization": auth.decode() if auth else None}


def websockets_pick(url: str):
    ws = "ws" + url[len("http"):]
    proxy = get_proxy(parse_uri(ws.split("/jeff")[0]))
    if proxy is None:
        return None
    parsed = parse_proxy(proxy)
    if parsed.scheme != "http":
        raise ValueError("only http:// proxies")
    import base64
    auth = None
    if parsed.username is not None:
        auth = "Basic " + base64.b64encode(f"{parsed.username}:{parsed.password}".encode()).decode()
    return {"url": f"http://{parsed.host}:{parsed.port}", "authorization": auth}


cases = []
for env in ENVS:
    with mock.patch.dict(os.environ, env, clear=True):
        for url in URLS:
            case = {"env": env, "url": url}
            for name, pick in (("httpx", httpx_pick), ("websockets", websockets_pick)):
                try:
                    case[name] = pick(url)
                except Exception:  # noqa: BLE001 - recorded as an error
                    case[name] = "error"
                if isinstance(case[name], dict) and not case[name]["url"].startswith("http://"):
                    case[name] = "error"  # an https:// or SOCKS proxy: this SDK refuses it
            cases.append(case)
json.dump(cases, sys.stdout, indent=1)
