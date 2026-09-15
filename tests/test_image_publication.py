"""Run the workflow's publication decisions with synthetic GitHub/registry state."""

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ghcr.yml"
GIT_BASH = Path("C:/Program Files/Git/bin/bash.exe")
BASH = str(GIT_BASH) if os.name == "nt" and GIT_BASH.exists() else shutil.which("bash")


def step_script(name):
    source = WORKFLOW.read_text(encoding="utf-8")
    step = source.split(f"      - name: {name}\n", 1)[1]
    step = step.split("\n      - ", 1)[0].split("\n  promote:", 1)[0]
    return textwrap.dedent(step.split("        run: |\n", 1)[1])


@unittest.skipUnless(BASH, "Bash is required to exercise GitHub Actions shell steps")
class ImagePublicationTests(unittest.TestCase):
    def test_reusable_quality_gate_cannot_cancel_its_caller(self):
        ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        image_workflow = WORKFLOW.read_text(encoding="utf-8")
        ci_group = ci.split("  group: ", 1)[1].splitlines()[0]
        image_group = image_workflow.split("  group: ", 1)[1].splitlines()[0]
        self.assertNotEqual(ci_group, image_group)

    def run_step(self, name, variables, prefix=""):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            env = dict(os.environ, **variables)
            env.update(GITHUB_OUTPUT=output.as_posix(), GITHUB_STEP_SUMMARY=output.as_posix())
            result = subprocess.run(
                [BASH, "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", prefix + step_script(name)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            return result, output.read_text() if output.exists() else ""

    def test_only_trusted_channels_publish_candidates(self):
        cases = [
            ("push", "refs/heads/main", True),
            ("push", "refs/heads/dev", True),
            ("push", "refs/tags/v2.4.1", True),
            ("workflow_dispatch", "refs/heads/main", True),
            ("workflow_dispatch", "refs/heads/dev", True),
            ("workflow_dispatch", "refs/heads/feature", False),
            ("push", "refs/heads/v2_test", False),
            ("pull_request", "refs/heads/main", False),
            ("pull_request", "refs/pull/12/merge", False),
        ]
        for event, ref, expected in cases:
            with self.subTest(event=event, ref=ref):
                result, output = self.run_step(
                    "Select candidate image",
                    {"GITHUB_EVENT_NAME": event, "GITHUB_REF": ref, "GITHUB_REPOSITORY": "Owner/HookWise"},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"publish={str(expected).lower()}\n", output)
                self.assertIn("image=ghcr.io/owner/hookwise\n", output)

    def test_missing_or_unreadable_reviewers_block_production(self):
        for response, status in [("true", "0"), ("false", "0"), ("", "0"), ("true", "1")]:
            with self.subTest(response=response, status=status):
                result, _ = self.run_step(
                    "Refuse production promotion without required reviewers",
                    {"GITHUB_REPOSITORY": "owner/hookwise", "RESPONSE": response, "STATUS": status},
                    'gh() { printf "%s" "$RESPONSE"; return "$STATUS"; };\n',
                )
                self.assertEqual(result.returncode == 0, response == "true" and status == "0")

    def test_production_copies_tested_digest_and_preserves_previous_image(self):
        image = "ghcr.io/owner/hookwise"
        digest = "sha256:" + "a" * 64
        previous = "sha256:" + "b" * 64
        result, output = self.run_step(
            "Promote without rebuilding and verify aliases",
            {
                "IMAGE": image,
                "DIGEST": digest,
                "PREVIOUS_DIGEST": previous,
                "TAGS_JSON": "{}",
                "TEST_TAGS": f"{image}:main\n{image}:latest",
                "ACTUAL_DIGEST": digest,
            },
            'jq() { printf "%s\\n" "$TEST_TAGS"; };\n'
            'docker() { if [[ "$*" == *"imagetools inspect"* ]]; then printf "%s" "$ACTUAL_DIGEST"; '
            'else printf "%s\\n" "$*" >> "$GITHUB_OUTPUT"; fi; };\n',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"--tag {image}:rollback {image}@{previous}", output)
        self.assertIn(f"--tag {image}:main --tag {image}:latest {image}@{digest}", output)
        self.assertLess(output.index(":rollback"), output.index(":latest"))

    def test_digest_mismatch_is_a_failed_promotion(self):
        result, _ = self.run_step(
            "Promote without rebuilding and verify aliases",
            {
                "IMAGE": "ghcr.io/owner/hookwise",
                "DIGEST": "sha256:" + "a" * 64,
                "PREVIOUS_DIGEST": "none",
                "TAGS_JSON": "{}",
            },
            'jq() { printf "%s\\n" "$IMAGE:latest"; };\ndocker() { printf "%s" "wrong-digest"; };\nsleep() { :; };\n',
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not resolve to the tested digest", result.stdout)


if __name__ == "__main__":
    unittest.main()
