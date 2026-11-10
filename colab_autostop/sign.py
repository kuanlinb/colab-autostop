"""Presigned URLs for S3-compatible storage, standard library only.

Supports Cloudflare R2, AWS S3, Google Cloud Storage (XML API with HMAC keys) and any S3-compatible endpoint.
The signature is AWS Signature Version 4 in query-string form; GCS uses the same math with its own names
(GOOG4-HMAC-SHA256 and X-Goog-* parameters). Only the host header is signed and the payload is UNSIGNED-PAYLOAD,
so a plain `curl -T file URL` can use a PUT URL.
"""
import datetime
import hashlib
import hmac
import os
import urllib.parse
from dataclasses import dataclass

MAX_EXPIRES = 7 * 24 * 3600  # SigV4 presigned URLs (S3, R2, GCS) cannot live longer than 7 days

DIALECTS = {
    "aws": {"alg": "AWS4-HMAC-SHA256", "prefix": "AWS4", "param": "X-Amz-", "service": "s3", "req": "aws4_request"},
    "goog": {"alg": "GOOG4-HMAC-SHA256", "prefix": "GOOG4", "param": "X-Goog-", "service": "storage",
             "req": "goog4_request"},
}
PROVIDERS = ("r2", "s3", "gcs", "custom")


def _q(s, safe="-_.~"):
    return urllib.parse.quote(s, safe=safe)


def sign_url(method, scheme, host, path, region, access_key_id, secret_access_key, expires,
             now=None, dialect="aws"):
    """Return a presigned URL for `method` on `scheme://host/path` (path already starts with '/')."""
    d = DIALECTS[dialect]
    expires = int(expires)
    if not 1 <= expires <= MAX_EXPIRES:
        raise ValueError(f"expires must be between 1 and {MAX_EXPIRES} seconds, got {expires}")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    stamp, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    scope = f"{day}/{region}/{d['service']}/{d['req']}"
    p = d["param"]
    query = {
        p + "Algorithm": d["alg"],
        p + "Credential": f"{access_key_id}/{scope}",
        p + "Date": stamp,
        p + "Expires": str(expires),
        p + "SignedHeaders": "host",
    }
    canonical_query = "&".join(f"{_q(k)}={_q(v)}" for k, v in sorted(query.items()))
    canonical_path = _q(path, safe="/-_.~")
    canonical_request = "\n".join(
        [method, canonical_path, canonical_query, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD"])
    string_to_sign = "\n".join(
        [d["alg"], stamp, scope, hashlib.sha256(canonical_request.encode()).hexdigest()])
    key = (d["prefix"] + secret_access_key).encode()
    for part in (day, region, d["service"], d["req"]):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return f"{scheme}://{host}{canonical_path}?{canonical_query}&{p}Signature={signature}"


@dataclass
class Storage:
    """Where results go. Secrets are never written anywhere by this package."""
    provider: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    account_id: str = ""   # R2 only
    region: str = ""       # S3 (default us-east-1) or custom endpoints
    endpoint: str = ""     # custom only, e.g. https://minio.example.com:9000
    path_style: bool = False  # S3 only: bucket in the path instead of the host name

    def locate(self, key):
        """(scheme, host, path, region, dialect) for an object key."""
        key = key.lstrip("/")
        if not key:
            raise ValueError("empty object key")
        if self.provider == "r2":
            if not self.account_id:
                raise ValueError("R2 needs an account id")
            return "https", f"{self.account_id}.r2.cloudflarestorage.com", f"/{self.bucket}/{key}", "auto", "aws"
        if self.provider == "s3":
            region = self.region or "us-east-1"
            if self.path_style:
                return "https", f"s3.{region}.amazonaws.com", f"/{self.bucket}/{key}", region, "aws"
            return "https", f"{self.bucket}.s3.{region}.amazonaws.com", f"/{key}", region, "aws"
        if self.provider == "gcs":
            return "https", "storage.googleapis.com", f"/{self.bucket}/{key}", "auto", "goog"
        if self.provider == "custom":
            u = urllib.parse.urlsplit(self.endpoint)
            if u.scheme not in ("http", "https") or not u.netloc:
                raise ValueError("custom provider needs an endpoint like https://host[:port]")
            return u.scheme, u.netloc, f"/{self.bucket}/{key}", self.region or "us-east-1", "aws"
        raise ValueError(f"unknown provider {self.provider!r}; use one of {', '.join(PROVIDERS)}")

    def presign(self, method, key, expires=3600, now=None):
        scheme, host, path, region, dialect = self.locate(key)
        return sign_url(method.upper(), scheme, host, path, region, self.access_key_id,
                        self.secret_access_key, expires, now=now, dialect=dialect)

    def public_config(self):
        """Settings that are safe to store in a local run record (no secrets)."""
        return {"provider": self.provider, "bucket": self.bucket, "account_id": self.account_id,
                "region": self.region, "endpoint": self.endpoint, "path_style": self.path_style}


def setting(name, explicit=None, env=os.environ):
    """A value from a flag, else $NAME, else the contents of the file named by $NAME_FILE."""
    if explicit:
        return explicit
    if env.get(name):
        return env[name]
    path = env.get(name + "_FILE")
    if path:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            return f.read().strip()
    return ""


def storage_from(provider=None, bucket=None, account_id=None, region=None, endpoint=None, path_style=None,
                 env=os.environ):
    """Build a Storage from explicit values, falling back to AUTOSTOP_* environment variables."""
    st = Storage(
        provider=(setting("AUTOSTOP_PROVIDER", provider, env) or "r2").lower(),
        bucket=setting("AUTOSTOP_BUCKET", bucket, env),
        access_key_id=setting("AUTOSTOP_ACCESS_KEY_ID", None, env),
        secret_access_key=setting("AUTOSTOP_SECRET_ACCESS_KEY", None, env),
        account_id=setting("AUTOSTOP_ACCOUNT_ID", account_id, env),
        region=setting("AUTOSTOP_REGION", region, env),
        endpoint=setting("AUTOSTOP_ENDPOINT", endpoint, env),
        path_style=bool(path_style) or env.get("AUTOSTOP_PATH_STYLE", "") not in ("", "0", "false"),
    )
    missing = [n for n, v in (("AUTOSTOP_BUCKET", st.bucket), ("AUTOSTOP_ACCESS_KEY_ID", st.access_key_id),
                              ("AUTOSTOP_SECRET_ACCESS_KEY", st.secret_access_key)) if not v]
    if missing:
        raise ValueError("missing storage setting(s): " + ", ".join(missing)
                         + " (set the variable, or NAME_FILE pointing to a file that holds it)")
    st.locate("probe")  # validates provider-specific settings early
    return st
