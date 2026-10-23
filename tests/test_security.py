"""Credential roles must remain separated, including bearer-token clients."""
import unittest

from fastapi import HTTPException
from starlette.requests import Request
from coordinator.security import ApiKeyAuth


class SecurityTests(unittest.TestCase):
    def test_duplicate_or_empty_role_credentials_are_rejected(self):
        for keys in (("same", "same", "consumer"), ("admin", "agent", "admin"),
                     ("admin", "agent", "agent"), ("admin", "", "consumer")):
            with self.subTest(keys=keys), self.assertRaises(ValueError):
                ApiKeyAuth(*keys)

    def test_each_role_accepts_only_its_credential_or_admin(self):
        auth = ApiKeyAuth("admin", "agent", "consumer")
        for name in ("admin", "agent", "consumer"):
            check = getattr(auth, "require_" + name)
            for supplied in ("admin", "agent", "consumer", "invalid", ""):
                for bearer in (False, True):
                    request = Request({"type": "http", "headers":
                        [(b"authorization", ("Bearer " + supplied).encode())] if bearer else []})
                    key = None if bearer else supplied
                    with self.subTest(role=name, supplied=supplied, bearer=bearer):
                        if supplied in ("admin", name):
                            check(request, key)
                        else:
                            with self.assertRaises(HTTPException) as error:
                                check(request, key)
                            self.assertEqual(error.exception.status_code, 401)
