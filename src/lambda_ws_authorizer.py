"""
Non-VPC Lambda: REQUEST authorizer for the Site Voice WebSocket API.

API Gateway WebSocket has no COGNITO_USER_POOLS authorizer (unlike the REST
API's CognitoAuthorizer), so the Cognito idToken is verified here in code:
RS256 signature via the pool's JWKS, plus exp / issuer / token_use=id. The
idToken rides in the $connect handshake `Authorization` header. On success we
return an IAM Allow policy + context {sub}; ws-connect (in-VPC) resolves the
sub to the user/company and upserts ws_connections. Non-VPC so JWKS can be
fetched over the internet (BUG-36: an in-VPC fn has no egress).

Env: WS_USER_POOL_IDS = comma-separated Cognito pool ids to trust (the same
pool(s) the REST CognitoAuthorizer trusts — OrgUserPoolId).
"""
import os

import jwt  # PyJWT (jwt-layer); PyJWT[crypto] pulls cryptography for RS256

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
POOL_IDS = [p for p in os.environ.get("WS_USER_POOL_IDS", "").split(",") if p]

# One PyJWKClient per pool, cached across warm invokes (each caches the fetched
# signing keys internally, so steady state does zero JWKS fetches).
_jwks_clients = {}


def _jwks_client(pool_id):
    client = _jwks_clients.get(pool_id)
    if client is None:
        url = f"https://cognito-idp.{REGION}.amazonaws.com/{pool_id}/.well-known/jwks.json"
        client = jwt.PyJWKClient(url)
        _jwks_clients[pool_id] = client
    return client


def _bearer(headers):
    # Handshake header is case-insensitive; scan case-folded. Tolerate an
    # optional "Bearer " prefix (mobile sends the raw token).
    for k, v in (headers or {}).items():
        if k.lower() == "authorization" and v:
            return v[7:] if v.lower().startswith("bearer ") else v
    return None


def _verify(token):
    """Return the token's claims if it validates against ANY trusted pool,
    else raise. Tries each pool's issuer + JWKS; first success wins."""
    for pool_id in POOL_IDS:
        issuer = f"https://cognito-idp.{REGION}.amazonaws.com/{pool_id}"
        try:
            signing_key = _jwks_client(pool_id).get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token, signing_key.key, algorithms=["RS256"],
                issuer=issuer, options={"verify_aud": False})
        except Exception:
            continue
        if claims.get("token_use") == "id" and claims.get("sub"):
            return claims
    raise ValueError("token did not validate against any trusted pool")


def _policy(effect, resource, sub):
    return {
        "principalId": sub or "unknown",
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [{"Action": "execute-api:Invoke",
                           "Effect": effect, "Resource": resource}],
        },
        "context": {"sub": sub} if sub else {},
    }


def lambda_handler(event, context):
    token = _bearer(event.get("headers"))
    method_arn = event.get("methodArn", "*")
    if not token:
        raise Exception("Unauthorized")   # API Gateway maps this to 401
    try:
        claims = _verify(token)
    except Exception:
        raise Exception("Unauthorized")
    return _policy("Allow", method_arn, claims["sub"])
