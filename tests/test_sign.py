import datetime
import os
import tempfile
import unittest
import urllib.parse

from colab_autostop.sign import MAX_EXPIRES, Storage, setting, sign_url, storage_from

AK, SK = "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
T0 = datetime.datetime(2013, 5, 24, tzinfo=datetime.timezone.utc)


def query(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


class AwsTestVector(unittest.TestCase):
    def test_matches_aws_documentation_example(self):
        # "Authenticating Requests: Using Query Parameters (AWS Signature Version 4)", Amazon S3 API reference:
        # GET examplebucket/test.txt, us-east-1, 2013-05-24T00:00:00Z, expires 86400.
        url = sign_url("GET", "https", "examplebucket.s3.amazonaws.com", "/test.txt", "us-east-1", AK, SK, 86400,
                       now=T0)
        self.assertEqual(query(url)["X-Amz-Signature"],
                         "aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404")
        self.assertTrue(url.startswith("https://examplebucket.s3.amazonaws.com/test.txt?X-Amz-Algorithm="))


class Providers(unittest.TestCase):
    def st(self, provider, **kw):
        return Storage(provider=provider, bucket="bkt", access_key_id=AK, secret_access_key=SK, **kw)

    def test_r2(self):
        url = self.st("r2", account_id="acct123").presign("PUT", "jobs/a b/results.tgz", 600, now=T0)
        u = urllib.parse.urlsplit(url)
        self.assertEqual(u.netloc, "acct123.r2.cloudflarestorage.com")
        self.assertEqual(u.path, "/bkt/jobs/a%20b/results.tgz")
        q = query(url)
        self.assertEqual(q["X-Amz-Credential"], f"{AK}/20130524/auto/s3/aws4_request")
        self.assertEqual(q["X-Amz-Expires"], "600")
        self.assertEqual(q["X-Amz-SignedHeaders"], "host")
        self.assertRegex(q["X-Amz-Signature"], "^[0-9a-f]{64}$")

    def test_r2_needs_account(self):
        with self.assertRaises(ValueError):
            self.st("r2").presign("PUT", "k")

    def test_s3_virtual_host_and_path_style(self):
        url = self.st("s3", region="ap-northeast-1").presign("GET", "k.txt", now=T0)
        self.assertEqual(urllib.parse.urlsplit(url).netloc, "bkt.s3.ap-northeast-1.amazonaws.com")
        self.assertEqual(urllib.parse.urlsplit(url).path, "/k.txt")
        self.assertIn("/ap-northeast-1/s3/aws4_request", query(url)["X-Amz-Credential"])
        url = self.st("s3", path_style=True).presign("GET", "k.txt", now=T0)
        self.assertEqual(urllib.parse.urlsplit(url).netloc, "s3.us-east-1.amazonaws.com")
        self.assertEqual(urllib.parse.urlsplit(url).path, "/bkt/k.txt")

    def test_s3_matches_low_level_signer(self):
        url = self.st("s3", region="eu-west-1").presign("PUT", "x/y", 900, now=T0)
        ref = sign_url("PUT", "https", "bkt.s3.eu-west-1.amazonaws.com", "/x/y", "eu-west-1", AK, SK, 900, now=T0)
        self.assertEqual(url, ref)

    def test_gcs_uses_goog4_names(self):
        url = self.st("gcs").presign("PUT", "k", now=T0)
        u, q = urllib.parse.urlsplit(url), query(url)
        self.assertEqual(u.netloc, "storage.googleapis.com")
        self.assertEqual(u.path, "/bkt/k")
        self.assertEqual(q["X-Goog-Algorithm"], "GOOG4-HMAC-SHA256")
        self.assertEqual(q["X-Goog-Credential"], f"{AK}/20130524/auto/storage/goog4_request")
        self.assertIn("X-Goog-Signature", q)
        self.assertFalse(any(k.startswith("X-Amz-") for k in q))

    def test_custom_endpoint_keeps_port(self):
        url = self.st("custom", endpoint="http://minio.local:9000").presign("GET", "k", now=T0)
        u = urllib.parse.urlsplit(url)
        self.assertEqual((u.scheme, u.netloc, u.path), ("http", "minio.local:9000", "/bkt/k"))

    def test_method_and_key_change_signature(self):
        st = self.st("r2", account_id="a")
        sigs = {query(st.presign(m, k, now=T0))["X-Amz-Signature"] for m in ("GET", "PUT") for k in ("a", "b")}
        self.assertEqual(len(sigs), 4)

    def test_expiry_bounds(self):
        st = self.st("r2", account_id="a")
        st.presign("PUT", "k", MAX_EXPIRES)
        for bad in (0, MAX_EXPIRES + 1):
            with self.assertRaises(ValueError):
                st.presign("PUT", "k", bad)

    def test_public_config_has_no_secret(self):
        cfg = self.st("r2", account_id="a").public_config()
        self.assertNotIn(SK, repr(cfg))
        self.assertNotIn(AK, repr(cfg))


class Settings(unittest.TestCase):
    def test_file_variant(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "secret")
            with open(p, "w") as f:
                f.write("  s3cr3t\n")
            env = {"AUTOSTOP_SECRET_ACCESS_KEY_FILE": p}
            self.assertEqual(setting("AUTOSTOP_SECRET_ACCESS_KEY", env=env), "s3cr3t")
            self.assertEqual(setting("AUTOSTOP_SECRET_ACCESS_KEY", "flag", env=env), "flag")

    def test_storage_from_env_and_missing(self):
        env = {"AUTOSTOP_PROVIDER": "gcs", "AUTOSTOP_BUCKET": "b", "AUTOSTOP_ACCESS_KEY_ID": "a",
               "AUTOSTOP_SECRET_ACCESS_KEY": "s"}
        self.assertEqual(storage_from(env=env).provider, "gcs")
        del env["AUTOSTOP_SECRET_ACCESS_KEY"]
        with self.assertRaises(ValueError) as cm:
            storage_from(env=env)
        self.assertIn("AUTOSTOP_SECRET_ACCESS_KEY", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
