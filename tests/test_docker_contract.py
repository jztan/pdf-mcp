"""
The Docker deployment and the code must agree on paths and ports.

These assertions catch the failure mode where someone edits one file and
not the other: a config mount that lands where PDFConfig will not look, or
an allow list that silently matches nothing. No image is built and no port
is bound.
"""

import fnmatch
import re
import sys
from pathlib import Path, PurePosixPath

import pytest

yaml = pytest.importorskip("yaml")

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from pdf_mcp.config import _DEFAULT_CONFIG_PATH  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
COMPOSE = REPO / "docker-compose.yml"
DOCKERFILE = REPO / "Dockerfile"
WORKFLOW = REPO / ".github" / "workflows" / "publish-pypi.yml"
DOCKER_CONFIG = REPO / "deploy" / "config.docker.toml"

CONTAINER_PDF_DIR = "/data/pdfs"


def _allow_patterns():
    """Read [paths] allow structurally, not by comment-sensitive parsing."""
    with open(DOCKER_CONFIG, "rb") as f:
        return tomllib.load(f)["paths"]["allow"]


@pytest.fixture(scope="module")
def service():
    data = yaml.safe_load(COMPOSE.read_text())
    return data["services"]["pdf-mcp"]


@pytest.fixture(scope="module")
def dockerfile():
    return DOCKERFILE.read_text()


@pytest.fixture(scope="module")
def workflow():
    """Parsed release workflow.

    PyYAML turns the bare `on:` key into the boolean True (the Norway
    problem); nothing here reads it, so that is harmless.
    """
    return yaml.safe_load(WORKFLOW.read_text())


def _container_home(dockerfile_text):
    """Derive the container user's home from the useradd line."""
    match = re.search(r"useradd[^\n]*?--create-home (\w+)", dockerfile_text)
    assert match, "Dockerfile must create the runtime user with --create-home"
    return PurePosixPath("/home") / match.group(1)


class TestConfigMountLandsWherePDFConfigLooks:
    def test_mount_target_matches_pdfconfig_default(self, service, dockerfile):
        # PDFConfig has no env override, so the config must arrive at
        # Path.home()/.config/pdf-mcp/config.toml inside the container.
        relative = _DEFAULT_CONFIG_PATH.relative_to(Path.home())
        expected = str(_container_home(dockerfile) / relative)

        targets = [v.split(":")[1] for v in service["volumes"]]
        assert (
            expected in targets
        ), f"config must mount at {expected}; compose mounts {targets}"

    def test_config_mount_is_read_only(self, service):
        mount = [v for v in service["volumes"] if "config.toml" in v][0]
        assert mount.endswith(":ro")


class TestAllowListActuallyMatches:
    def test_allow_glob_matches_a_file_under_the_pdf_mount(self):
        patterns = _allow_patterns()
        candidate = f"{CONTAINER_PDF_DIR}/report.pdf"
        assert any(fnmatch.fnmatch(candidate, p) for p in patterns), (
            f"[paths] allow {patterns} matches nothing under "
            f"{CONTAINER_PDF_DIR}; check_path fnmatches the resolved FILE path"
        )

    def test_allow_glob_rejects_paths_outside_the_mount(self):
        patterns = _allow_patterns()
        assert not any(fnmatch.fnmatch("/etc/passwd", p) for p in patterns)

    def test_documents_volume_targets_the_allowed_dir(self, service):
        targets = [v.split(":")[1] for v in service["volumes"]]
        assert CONTAINER_PDF_DIR in targets

    def test_documents_volume_is_read_only(self, service):
        mount = [v for v in service["volumes"] if CONTAINER_PDF_DIR in v][0]
        assert mount.endswith(":ro")


class TestPortPublishing:
    def test_published_port_binds_loopback_and_targets_exposed_port(
        self, service, dockerfile
    ):
        exposed = re.search(r"^EXPOSE (\d+)", dockerfile, re.M).group(1)
        published = service["ports"][0]
        assert published.startswith(
            "127.0.0.1:"
        ), f"{published} must bind loopback; TLS belongs to a host proxy"
        assert published.endswith(f":{exposed}")

    def test_cache_is_a_named_volume_not_a_bind_mount(self, service):
        mount = [v for v in service["volumes"] if v.endswith(":/data/cache")][0]
        source = mount.split(":")[0]
        assert not source.startswith("."), (
            "cache must be a named volume: a Linux bind mount is host-uid "
            "owned and PDFCache.__init__ chmods the cache dir, which "
            "requires ownership"
        )
        assert source in yaml.safe_load(COMPOSE.read_text())["volumes"]

    def test_compose_has_no_obsolete_version_key(self):
        assert "version" not in yaml.safe_load(COMPOSE.read_text())


class TestContainerPortAgreesEverywhere:
    """
    The container-internal port appears in six places: the compose
    environment pin, the compose publish target, the compose healthcheck
    URL, the Dockerfile (EXPOSE and HEALTHCHECK), and the release
    workflow's smoke job (its `docker run -p` publish and the
    127.0.0.1 URLs it probes). If they disagree, the deployment is a
    running container that answers nothing, so any edit that moves one
    site must fail here.
    """

    def test_compose_pins_the_container_port(self, service):
        env = service.get("environment", {})
        assert str(env.get("PDF_MCP_HTTP_PORT")) == "8000", (
            "compose must pin PDF_MCP_HTTP_PORT so a value in .env cannot "
            "move the app off the port the publish target assumes"
        )

    def test_all_port_sites_agree(self, service, dockerfile):
        pinned = str(service["environment"]["PDF_MCP_HTTP_PORT"])

        publish_target = service["ports"][0].rsplit(":", 1)[1]
        assert publish_target == pinned

        compose_probe = " ".join(service["healthcheck"]["test"])
        assert f"localhost:{pinned}/" in compose_probe

        exposed = re.search(r"^EXPOSE (\d+)", dockerfile, re.M).group(1)
        assert exposed == pinned

        docker_probe = re.search(
            r"^HEALTHCHECK.*?CMD (.+)$", dockerfile, re.M | re.S
        ).group(1)
        assert f"localhost:{pinned}/" in docker_probe

    def test_smoke_job_publishes_the_exposed_port(self, workflow, dockerfile):
        # The smoke job runs the image with an explicit `-p host:container`
        # and polls 127.0.0.1:<host>. Moving EXPOSE without moving these
        # passes every other test and then hangs the release's health poll
        # after the tag has already been pushed.
        exposed = re.search(r"^EXPOSE (\d+)", dockerfile, re.M).group(1)
        run_text = " ".join(
            s.get("run", "") for s in workflow["jobs"]["smoke"]["steps"]
        )
        publishes = re.findall(r"-p 127\.0\.0\.1:(\d+):(\d+)", run_text)
        assert publishes, "smoke must publish the container port explicitly"
        for host, container in publishes:
            assert container == exposed, (
                f"smoke publishes container port {container} but the "
                f"Dockerfile EXPOSEs {exposed}"
            )
            assert f"http://127.0.0.1:{host}/" in run_text


class TestPullFirstImageContract:
    """`./deploy.sh` pulls a published image; `--build` still builds.

    The image name lives in exactly one place (the workflow's env.IMAGE).
    Compose must point at that same name with an INTERPOLATED tag: a
    hardcoded version here would become a fourth site release.py has to
    bump, which is precisely what this design avoids.
    """

    def test_workflow_declares_the_image_name(self, workflow):
        assert workflow["env"]["IMAGE"] == "ghcr.io/jztan/pdf-mcp"

    def test_compose_image_matches_the_workflow_image_name(self, service, workflow):
        # ghcr.io carries no port, so the first colon separates name from
        # tag even though the tag itself contains ":-".
        match = re.match(r"^(?P<name>[^:]+):(?P<tag>.+)$", service["image"])
        assert match, f"compose image {service['image']!r} has no tag"
        assert match.group("name") == workflow["env"]["IMAGE"]
        assert match.group("tag").startswith("${"), (
            "image tag must be interpolated (e.g. "
            "${PDF_MCP_IMAGE_TAG:-latest}) so a release never has to edit "
            "docker-compose.yml"
        )

    def test_compose_keeps_a_build_block_for_the_build_flag(self, service):
        # `docker compose pull` pulls buildable services by default (it
        # skips them only with --ignore-buildable), so image: and build:
        # coexist. Removing this block would orphan ./deploy.sh --build.
        assert service["build"]["context"] == "."
        assert service["build"]["dockerfile"] == "Dockerfile"

    def test_env_example_documents_the_image_tag(self):
        assert "PDF_MCP_IMAGE_TAG" in (REPO / ".env.example").read_text()


class TestDeployScriptPullsByDefault:
    """deploy.sh must pull unless --build is passed, and must still
    restate no port, no mount, and no image name."""

    @property
    def script(self):
        return (REPO / "deploy.sh").read_text()

    def test_build_flag_is_documented(self):
        # `"--build" in script` would be satisfied by the pre-existing
        # `--build-only`, so assert the standalone case arm itself.
        assert "--build)" in self.script
        assert "--build         Build the image locally" in self.script

    def test_default_path_pulls(self):
        assert "docker compose pull" in self.script

    def test_no_raw_docker_run(self):
        # Every lifecycle verb delegates to compose; a raw `docker run`
        # would have to restate the port and mounts.
        assert "docker run" not in self.script

    def test_image_name_is_not_restated(self):
        assert "ghcr.io" not in self.script

    def test_pull_failure_points_at_the_build_escape_hatch(self):
        # A private package, an unreleased tag, or a bad PDF_MCP_IMAGE_TAG
        # otherwise kills the script with a bare Docker `denied` under
        # `set -e`, on the path the README calls one-command.
        assert "if ! docker compose pull; then" in self.script
        failure_block = self.script.split("if ! docker compose pull; then", 1)[1]
        failure_block = failure_block.split("fi", 1)[0]
        assert "./deploy.sh --build" in failure_block, (
            "a failed pull must name --build as the escape hatch; without "
            "it the user only sees Docker's raw error"
        )


class TestMultiArchBuildJobs:
    """Both architectures build on NATIVE runners.

    The Dockerfile bakes onnxruntime and an ONNX model; QEMU emulation is
    exactly what its header comment warns against, so an emulated build
    must not creep back in.
    """

    def _include(self, workflow):
        return workflow["jobs"]["build-image"]["strategy"]["matrix"]["include"]

    def test_both_architectures_are_built(self, workflow):
        platforms = {e["platform"] for e in self._include(workflow)}
        assert platforms == {"linux/amd64", "linux/arm64"}

    def test_arm64_uses_a_native_arm_runner(self, workflow):
        runners = {e["platform"]: e["runner"] for e in self._include(workflow)}
        assert runners["linux/arm64"].endswith("-arm"), (
            "arm64 must build on a native runner; QEMU is unreliable for "
            "onnxruntime, per the Dockerfile header"
        )
        assert not runners["linux/amd64"].endswith("-arm")

    def test_no_qemu_setup(self, workflow):
        text = WORKFLOW.read_text()
        assert "setup-qemu-action" not in text

    def test_build_does_not_wait_for_tests(self, workflow):
        # No `needs:` lets build-image start in parallel with the test
        # matrix instead of queueing behind it, which is what the job's
        # own comment says.
        assert "needs" not in workflow["jobs"]["build-image"]

    def test_assemble_publishes_a_staging_tag_not_a_version_tag(self, workflow):
        steps = workflow["jobs"]["assemble"]["steps"]
        run_text = " ".join(s.get("run", "") for s in steps)
        assert "imagetools create" in run_text
        assert "sha-" in run_text


class TestSmokeGate:
    """Smoke runs on BOTH native runners and exercises the parts a
    /health probe cannot reach.

    Check 1 is the reason multi-arch publishing is safe at all: it loads
    the arch-specific onnxruntime wheel and the baked model with the
    network off. /health touches neither.
    """

    def test_smoke_runs_on_both_architectures(self, workflow):
        include = workflow["jobs"]["smoke"]["strategy"]["matrix"]["include"]
        runners = {e["runner"] for e in include}
        assert any(r.endswith("-arm") for r in runners)
        assert any(not r.endswith("-arm") for r in runners)

    def test_smoke_waits_for_the_staging_manifest(self, workflow):
        needs = workflow["jobs"]["smoke"]["needs"]
        needs = {needs} if isinstance(needs, str) else set(needs)
        assert "assemble" in needs

    def test_smoke_checks_the_embedding_backend_offline(self, workflow):
        run_text = " ".join(
            s.get("run", "") for s in workflow["jobs"]["smoke"]["steps"]
        )
        assert "--network none" in run_text
        assert "TextEmbedding" in run_text

    def test_smoke_checks_auth_and_tools(self, workflow):
        run_text = " ".join(
            s.get("run", "") for s in workflow["jobs"]["smoke"]["steps"]
        )
        assert "tools/list" in run_text
        assert "pdf_search" in run_text
        assert "401" in run_text
        assert "initialize" in run_text
        assert "mcp-session-id" in run_text.lower()


class TestPromotionGate:
    """No version tag may exist unless BOTH architectures passed smoke
    AND the PyPI upload succeeded. Every edge into promote-image is a
    gate; removing one silently ships an untested or orphaned image."""

    def test_publish_pypi_waits_for_the_image_build(self, workflow):
        # A broken Dockerfile, or an image that builds but does not run,
        # must stop the release BEFORE PyPI, which is the one step that
        # cannot be unwound.
        needs = set(workflow["jobs"]["publish"]["needs"])
        assert {"build-image", "smoke"} <= needs

    def test_promote_gates_on_smoke_and_pypi(self, workflow):
        needs = set(workflow["jobs"]["promote-image"]["needs"])
        assert {"assemble", "smoke", "publish"} <= needs

    def test_promote_creates_semver_tags_from_the_staging_tag(self, workflow):
        steps = workflow["jobs"]["promote-image"]["steps"]
        meta = [s for s in steps if "metadata-action" in str(s.get("uses"))]
        assert meta, "promote-image must compute tags with metadata-action"
        tags = meta[0]["with"]["tags"]
        assert "type=semver,pattern={{version}}" in tags
        assert "type=semver,pattern={{major}}.{{minor}}" in tags
        assert "type=semver,pattern={{major}}" in tags
        assert meta[0]["with"]["flavor"] == "latest=auto"
        run_text = " ".join(s.get("run", "") for s in steps)
        assert "imagetools create" in run_text
        assert "sha-" in run_text, "promotion must reuse the staging manifest"

    def test_pypi_upload_is_idempotent(self, workflow):
        steps = workflow["jobs"]["publish"]["steps"]
        run_text = " ".join(s.get("run", "") for s in steps)
        assert "--skip-existing" in run_text

    def test_publishing_jobs_only_run_on_a_tag_push(self, workflow):
        # workflow_dispatch is build-only validation. Without these
        # guards a manual run would twine-upload to real PyPI and move
        # the version tags.
        for job in ("publish", "promote-image", "assemble", "smoke"):
            assert "github.event_name == 'push'" in workflow["jobs"][job]["if"]

    def test_build_image_pushes_only_on_a_tag_push(self, workflow):
        # build-image carries no `if:`, so the test above cannot cover it.
        # Its guard lives inside the build-push-action `outputs:` string.
        # Simplifying that to `push=true` would keep the whole suite green
        # while every manual workflow_dispatch pushed untagged digests
        # into GHCR, where nothing cleans them up.
        steps = workflow["jobs"]["build-image"]["steps"]
        build = [s for s in steps if "build-push-action" in str(s.get("uses"))]
        assert build, "build-image must build with docker/build-push-action"
        outputs = build[0]["with"]["outputs"]
        assert "push=${{ github.event_name == 'push' }}" in outputs, (
            "build-image must push only on a tag push; a literal push=true "
            f"would publish digests on every workflow_dispatch. Got: {outputs!r}"
        )

    def test_promotion_fails_loudly_when_no_tags_were_computed(self, workflow):
        steps = workflow["jobs"]["promote-image"]["steps"]
        run_text = " ".join(s.get("run", "") for s in steps)
        assert '[ -n "$tags" ]' in run_text


class TestImageNameIsSingleSourced:
    """`ghcr.io` may appear only in the env declaration and in login
    `registry:` inputs. A literal image reference anywhere else, such as
    inside a run: block, would silently bypass env.IMAGE."""

    def test_no_literal_image_reference_outside_the_env_declaration(self):
        offenders = []
        for num, line in enumerate(WORKFLOW.read_text().splitlines(), 1):
            if "ghcr.io" not in line:
                continue
            stripped = line.strip()
            if stripped.startswith("IMAGE:") or stripped == "registry: ghcr.io":
                continue
            offenders.append(f"{num}: {stripped}")
        assert (
            not offenders
        ), "reference the image via env.IMAGE or $IMAGE, not a literal: " + "; ".join(
            offenders
        )


class TestReleaseScriptImageName:
    def test_release_py_image_matches_the_workflow(self, workflow):
        """One image name, two consumers. A rename in either file that
        does not update the other fails here rather than at release time."""
        import sys

        sys.path.insert(0, str(REPO / "scripts"))
        import release

        assert release.GHCR_IMAGE == workflow["env"]["IMAGE"]


class TestClientWaitOutlivesTheWorkflow:
    """release.py must wait longer than the workflow can legally run.

    The critical path is build-image, assemble, smoke, publish,
    promote-image. If the client's budget is smaller than the sum of those
    jobs' own caps, release.py can give up first and report "RELEASE
    INCOMPLETE" for a release that is still running and about to publish
    to PyPI and move the `latest` tag. That is a false failure reported at
    the moment the operator is most likely to act on it, so raising a
    timeout-minutes in the workflow must fail here until max_wait follows.
    """

    # Jobs on the critical path that carry no timeout-minutes of their
    # own. GitHub's default is 6 hours, so these are budgeted by judgment,
    # not read from the file.
    UNTIMED_ALLOWANCE_MINUTES = {"assemble": 5, "publish": 10, "promote-image": 5}

    def test_max_wait_exceeds_the_sum_of_job_timeouts(self, workflow):
        import inspect
        import sys

        sys.path.insert(0, str(REPO / "scripts"))
        import release

        critical_path = ["build-image", "assemble", "smoke", "publish", "promote-image"]
        total_minutes = 0
        for job in critical_path:
            capped = workflow["jobs"][job].get("timeout-minutes")
            if capped is None:
                capped = self.UNTIMED_ALLOWANCE_MINUTES[job]
            total_minutes += int(capped)

        max_wait = (
            inspect.signature(release.wait_for_publish_workflow)
            .parameters["max_wait"]
            .default
        )
        assert max_wait > total_minutes * 60, (
            f"release.py wait_for_publish_workflow max_wait={max_wait}s is not "
            f"greater than the workflow's critical path of "
            f"{total_minutes * 60}s ({total_minutes} minutes); the client "
            "would give up while the workflow is still publishing"
        )
