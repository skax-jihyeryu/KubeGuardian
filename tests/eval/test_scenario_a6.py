"""Offline tests for the A6 Deployment mutations."""

import copy
import importlib.util
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

SCENARIO = Path(__file__).resolve().parents[2] / "scenarios" / "A6" / "scenario.py"
spec = importlib.util.spec_from_file_location("scenario_a6", SCENARIO)
scenario = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scenario)


class ScenarioTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name) / ".state" / "dev-gateway.json"
        self.env = [
            {"name": "WORKER", "value": "2"},
            {"name": "ADMIN_SERVICE_URL", "value": "http://admin:8001"},
            {"name": "AUTH_VERIFY_URL", "value": "http://admin:8001/api/v1/auth/verify"},
        ]
        self.original = copy.deepcopy(self.env)
        self.gateway = {
            "name": "gateway",
            "image": "kg-target/gateway:local",
            "env": self.env,
        }
        self.metadata = {"uid": "gateway-uid", "resourceVersion": "1"}
        self.race = False
        self.rollout_fails = False
        self.patches = 0
        for name, replacement in (
            ("STATE", self.state),
            ("guard", lambda: "https://127.0.0.1:12345"),
            ("deployment", lambda: (self.metadata, self.gateway)),
            ("check_fallback", lambda _: None),
            ("kubectl", self.kubectl),
        ):
            mocked = patch.object(scenario, name, replacement)
            mocked.start()
            self.addCleanup(mocked.stop)

    def kubectl(self, *args, patch_body=None):
        if args[0] == "patch":
            uid, version, change = json.loads(patch_body)
            if self.race:
                self.metadata["resourceVersion"] = "concurrent"
            if uid != {
                "op": "test",
                "path": "/metadata/uid",
                "value": self.metadata["uid"],
            } or version != {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": self.metadata["resourceVersion"],
            }:
                raise RuntimeError("patch rejected")
            index = int(change["path"].rsplit("/", 1)[1])
            if change["op"] == "remove":
                self.env.pop(index)
            else:
                self.env.insert(index, change["value"])
            self.patches += 1
        elif args[:2] != ("rollout", "status"):
            self.fail("Unexpected Kubernetes command")
        elif self.rollout_fails:
            raise RuntimeError("rollout failed")

    def test_remove_one_entry_and_restore_it_in_order(self):
        scenario.main("inject")
        self.assertEqual(self.env, [self.original[0], self.original[2]])
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        scenario.main("verify")
        scenario.main("reset")
        scenario.main("reset")
        self.assertEqual(self.env, self.original)
        self.assertEqual(self.patches, 2)
        self.assertFalse(self.state.exists())

    def test_verify_requires_the_saved_omission(self):
        scenario.main("inject")
        self.env.insert(1, self.original[1])
        with self.assertRaisesRegex(RuntimeError, "injected state"):
            scenario.main("verify")

    def test_inject_refuses_value_from(self):
        self.env[1] = {
            "name": "ADMIN_SERVICE_URL",
            "valueFrom": {"configMapKeyRef": {"name": "config", "key": "admin-url"}},
        }
        with self.assertRaisesRegex(RuntimeError, "literal"):
            scenario.main("inject")
        self.assertEqual(self.patches, 0)
        self.assertFalse(self.state.exists())

    def test_reset_refuses_replacement_deployment(self):
        scenario.main("inject")
        self.metadata["uid"] = "replacement-gateway"
        with self.assertRaisesRegex(RuntimeError, "changed"):
            scenario.main("reset")
        self.assertEqual(self.patches, 1)
        self.assertTrue(self.state.exists())

    def test_reset_refuses_changed_env(self):
        scenario.main("inject")
        self.env[0]["value"] = "3"
        with self.assertRaisesRegex(RuntimeError, "changed"):
            scenario.main("reset")
        self.assertEqual(self.patches, 1)
        self.assertTrue(self.state.exists())

    def test_reset_patch_checks_resource_version(self):
        scenario.main("inject")
        self.race = True
        with self.assertRaisesRegex(RuntimeError, "patch rejected"):
            scenario.main("reset")
        self.assertEqual(self.patches, 1)
        self.assertTrue(self.state.exists())

    def test_failed_inject_can_reset_without_a_write(self):
        self.race = True
        with self.assertRaisesRegex(RuntimeError, "patch rejected"):
            scenario.main("inject")
        scenario.main("reset")
        self.assertEqual(self.env, self.original)
        self.assertEqual(self.patches, 0)
        self.assertFalse(self.state.exists())

    def test_failed_rollout_after_mutation_can_restore_original_env(self):
        self.rollout_fails = True
        with self.assertRaisesRegex(RuntimeError, "rollout failed"):
            scenario.main("inject")
        self.assertEqual(self.env, [self.original[0], self.original[2]])
        self.assertTrue(self.state.exists())

        self.rollout_fails = False
        scenario.main("reset")
        self.assertEqual(self.env, self.original)
        self.assertEqual(self.patches, 2)
        self.assertFalse(self.state.exists())


class PreflightTest(unittest.TestCase):
    def test_rejects_non_dev_namespace_without_kubectl(self):
        with (
            patch.dict(os.environ, {"KG_NAMESPACE": "production"}),
            patch.object(scenario, "run") as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "dev test namespace"):
                scenario.guard()
            run.assert_not_called()

    def test_rejects_non_minikube_context(self):
        with (
            patch.dict(os.environ, {"KG_NAMESPACE": "dev"}),
            patch.object(scenario, "run", return_value="other-context") as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "minikube context"):
                scenario.guard()
            run.assert_called_once_with("kubectl", "config", "current-context")

    def test_rejects_prefixed_env_from_key_before_exec(self):
        container = {"envFrom": [{"prefix": "ADMIN_", "secretRef": {"name": "gateway-settings"}}]}
        with patch.object(scenario, "kubectl", return_value="service_url\n") as kubectl:
            with self.assertRaisesRegex(RuntimeError, "also supplies ADMIN_SERVICE_URL"):
                scenario.check_fallback(container)
            kubectl.assert_called_once_with(
                "get",
                "secret",
                "gateway-settings",
                "--ignore-not-found=false",
                "-o",
                "go-template={{range $key, $value := .data}}{{println $key}}{{end}}",
            )


if __name__ == "__main__":
    unittest.main()
