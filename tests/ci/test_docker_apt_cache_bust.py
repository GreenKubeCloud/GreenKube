from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APT_CACHE_BUST_VALUE = "APT_CACHE_BUST=${{ github.run_id }}-${{ github.run_attempt }}"


def test_dockerfile_security_upgrade_layers_use_cache_bust_arg() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    upgrade_layers = [
        block for block in dockerfile.split("\n\n") if "apt-get update" in block and "apt-get upgrade" in block
    ]

    assert len(upgrade_layers) == 2
    assert dockerfile.count("ARG APT_CACHE_BUST") == 2
    for layer in upgrade_layers:
        assert "https://deb.debian.org" in layer
        assert "Acquire::Retries=5" in layer
        assert "APT::Update::Error-Mode=any" in layer

    final_image_upgrade_layer = upgrade_layers[-1]
    assert "APT_CACHE_BUST" in final_image_upgrade_layer


def test_ci_docker_builds_refresh_security_upgrade_layer_per_run() -> None:
    workflow_paths = [
        PROJECT_ROOT / ".github/workflows/security.yml",
        PROJECT_ROOT / ".github/workflows/dev-build.yml",
        PROJECT_ROOT / ".github/workflows/release.yml",
    ]

    for workflow_path in workflow_paths:
        workflow = workflow_path.read_text(encoding="utf-8")

        assert "pull: true" in workflow
        assert "build-args:" in workflow
        assert APT_CACHE_BUST_VALUE in workflow
