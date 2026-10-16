"""Narrow normal weapi transport. No credentials, login, unblock or alternate CDN.

Protocol framing reviewed against api-enhanced 2db6684453297ee86dec0492842f2320b08182d9.
MIT source notice: notices/netease-api-enhanced-MIT.txt. No upstream dependencies
or HTTP server are installed/executed. TLS verification remains enabled.
"""
from __future__ import annotations
import base64
import json
import secrets
import string
import time
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PUBLIC_KEY = b'''-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDgtQn2JZ34ZC28NWYpAUd98iZ37BUrX/aKzmFbt7clFSs6sXqHauqKWqdtLkF2KexO40H1YTX8z2lSgBBOAxLsvaklV8k4cBFK9snQXE9/DDaFt6Rr7iVZMldczhC0JNgTz+SHXT6CBHuX3e9SdB1Ua44oncaTWz7OBGLbCiK45wIDAQAB
-----END PUBLIC KEY-----'''
ALLOWED = {'/api/cloudsearch/pc', '/api/v3/song/detail', '/api/song/enhance/player/url', '/api/song/lyric'}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('NETEASE_REDIRECT_REFUSED')


def frame(data):
    def aes(value, key):
        padder = padding.PKCS7(128).padder()
        value = padder.update(value) + padder.finalize()
        enc = Cipher(algorithms.AES(key), modes.CBC(b'0102030405060708')).encryptor()
        return base64.b64encode(enc.update(value) + enc.finalize())
    secret = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16)).encode('ascii')
    value = json.dumps({**data, 'csrf_token': ''}, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    key = serialization.load_pem_public_key(PUBLIC_KEY).public_numbers()
    return {'params': aes(aes(value, b'0CoJUm6Qyw8W8jud'), secret).decode('ascii'),
            'encSecKey': pow(int.from_bytes(secret[::-1], 'big'), key.e, key.n).to_bytes(128, 'big').hex()}


def request(route, data):
    if route not in ALLOWED:
        raise ValueError('NETEASE_ENDPOINT_REJECTED')
    req = urllib.request.Request('https://music.163.com/weapi/' + route[5:],
        data=urllib.parse.urlencode(frame(data)).encode('ascii'), method='POST', headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36',
            'Origin': 'https://music.163.com', 'Referer': 'https://music.163.com/', 'Accept-Encoding': 'identity',
        })
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=12) as response:
        parts, count, started = [], 0, time.monotonic()
        while True:
            part = getattr(response, 'read1', response.read)(65536)
            count += len(part)
            if count > 1024**2 or time.monotonic()-started > 15:
                raise ValueError('NETEASE_RESPONSE_LIMIT')
            if not part:
                break
            parts.append(part)
    body = b''.join(parts)
    result = json.loads(body)
    if not isinstance(result, dict):
        raise ValueError('NETEASE_RESPONSE_INVALID')
    return result
