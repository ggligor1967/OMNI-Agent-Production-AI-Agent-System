import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestAuthBootstrapCli(unittest.TestCase):
    def setUp(self):
        from agent.auth import AuthManager

        self.tmpdir = tempfile.mkdtemp()
        self.auth = AuthManager(
            secret="test_secret_32chars_xxxxxxxxxx",
            db_path=os.path.join(self.tmpdir, "auth.db"),
            enforce_auth=True,
            bootstrap_token="bootstrap_secret_32chars_xxxxxxxxx",
        )

    def _run_bootstrap(self, admin_key: str, user_id: str = "bootstrap-admin"):
        from main import run_create_admin_bootstrap

        prompts = iter([user_id, "Bootstrap Admin"])
        secrets = iter([admin_key, admin_key])
        stdout = io.StringIO()
        result = run_create_admin_bootstrap(
            auth_manager=self.auth,
            input_fn=lambda _prompt="": next(prompts),
            secret_reader=lambda _prompt="": next(secrets),
            output_stream=stdout,
        )
        return result, stdout.getvalue()

    def test_build_arg_parser_accepts_create_admin(self):
        from main import build_arg_parser

        args = build_arg_parser().parse_args(["--create-admin"])
        self.assertTrue(args.create_admin)
        self.assertEqual(args.mode, "cli")

    def test_first_admin_creation_succeeds_and_authenticates(self):
        from agent.auth import verify_jwt

        admin_key = "omni_admin_bootstrap_key_value_1234567890"
        result, output = self._run_bootstrap(admin_key)

        self.assertEqual(result.user_id, "bootstrap-admin")
        self.assertEqual(result.role, "admin")
        ctx = self.auth.authenticate(api_key=admin_key)
        self.assertTrue(ctx.authenticated)
        self.assertTrue(ctx.is_admin)
        payload = verify_jwt(result.jwt, self.auth.secret)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["sub"], "bootstrap-admin")
        self.assertIn("Initial admin created", output)
        self.assertNotIn(admin_key, output)

    def test_second_creation_attempt_is_refused(self):
        self._run_bootstrap("omni_admin_bootstrap_key_value_1234567890")

        with self.assertRaises(RuntimeError):
            self._run_bootstrap("omni_admin_bootstrap_key_value_1234567899", user_id="other-admin")

    def test_weak_secret_is_rejected(self):
        from main import run_create_admin_bootstrap

        prompts = iter(["bootstrap-admin", "Bootstrap Admin"])
        secrets = iter(["too-short", "too-short"])

        with self.assertRaises(ValueError):
            run_create_admin_bootstrap(
                auth_manager=self.auth,
                input_fn=lambda _prompt="": next(prompts),
                secret_reader=lambda _prompt="": next(secrets),
                output_stream=io.StringIO(),
            )

    def test_no_secret_appears_in_output(self):
        admin_key = "omni_admin_bootstrap_key_value_abcdefghijk"
        result, output = self._run_bootstrap(admin_key)

        self.assertNotIn(admin_key, output)
        self.assertNotIn(self.auth.bootstrap_token, output)
        self.assertNotIn(result.jwt, output)
