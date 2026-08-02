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
    The container-internal port appears in four places: the compose
    environment pin, the compose publish target, the compose healthcheck
    URL, and the Dockerfile (EXPOSE and HEALTHCHECK). If they disagree,
    the deployment is a running container that answers nothing, so any
    edit that moves one site must fail here.
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
