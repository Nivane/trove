"""OIDC 测试助手:一个 MockTransport 背后的最小 IdP(零真实网络)。

discovery / JWKS / token 三个端点 + 本地 RSA 签名,与生产同一条 PyJWT
校验路径。服务层测试与 API 层测试共用这一份,避免两份 fake 各自漂移。
"""

from __future__ import annotations

import json
import time

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://idp.example.com"
CLIENT_ID = "trove-test"


def make_key(kid: str) -> tuple[object, dict]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return private, jwk


class FakeIdp:
    """可注入 ``httpx.MockTransport`` 的最小 IdP。"""

    def __init__(self, *, issuer: str = ISSUER) -> None:
        self.issuer = issuer
        self.private, self.jwk = make_key("k1")
        self.rotate: tuple[object, dict] | None = None
        self.calls = {"discovery": 0, "jwks": 0, "token": 0}
        self.token_status = 200
        self.token_payload: dict | None = None
        self.token_claims: dict = {}
        #: 回调流程里由测试从 oidc_states 读出的 nonce(签 ID token 用)。
        self.nonce = ""
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/.well-known/openid-configuration"):
            self.calls["discovery"] += 1
            return httpx.Response(200, json={
                "issuer": self.issuer,
                "authorization_endpoint": f"{self.issuer}/authorize",
                "token_endpoint": f"{self.issuer}/token",
                "jwks_uri": f"{self.issuer}/jwks",
            })
        if url.endswith("/jwks"):
            self.calls["jwks"] += 1
            keys = [self.jwk]
            if self.rotate is not None and self.calls["jwks"] > 1:
                keys = [self.rotate[1]]
            return httpx.Response(200, json={"keys": keys})
        if url.endswith("/token"):
            self.calls["token"] += 1
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "nope"})
            payload = self.token_payload or {"id_token": self.sign()}
            return httpx.Response(200, json=payload)
        return httpx.Response(404)

    def sign(self, **overrides) -> str:
        claims = {
            "iss": self.issuer,
            "aud": CLIENT_ID,
            "sub": "subject-1",
            "exp": int(time.time()) + 300,
            "iat": int(time.time()),
            "nonce": self.nonce,
            "preferred_username": "alice",
            "email": "alice@example.com",
            "groups": ["trove-users"],
        }
        claims.update(self.token_claims)
        claims.update(overrides)
        private, jwk = self.rotate if self.rotate is not None else (
            self.private, self.jwk)
        return jwt.encode(
            claims, private, algorithm="RS256", headers={"kid": jwk["kid"]})
