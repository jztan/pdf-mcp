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
