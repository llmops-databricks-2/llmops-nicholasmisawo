"""Your first test: project_config.yml loads with the course's ProjectConfig for dev.

`ci.yml` runs every test under tests/ on each pull request and each push to main; add yours here.
"""

from pathlib import Path

from consultation_paper_curator.config import ProjectConfig

PROJECT_CONFIG = Path(__file__).resolve().parents[1] / "project_config.yml"


def test_project_config_loads_for_dev() -> None:
    """Load the provisioned settings without requiring optional lecture resources."""
    cfg = ProjectConfig.from_yaml(str(PROJECT_CONFIG), "dev")

    assert cfg.full_volume_path == "llmops_dev.nicholasmisawo.consultation_papers"
    assert cfg.warehouse_id == "0e56acba898dad4a"


def test_project_config_inherits_top_level_usage_policy() -> None:
    """The usage policy is set once at the top of the file and applies to every environment."""
    for env in ("dev", "acc", "prd"):
        cfg = ProjectConfig.from_yaml(str(PROJECT_CONFIG), env)

        assert cfg.usage_policy_id == "ffe08ae3-a783-3201-ab0c-2fff24538807"


def test_project_config_loads_lakebase_instance() -> None:
    """Each environment names its provisioned Lakebase project."""
    cfg = ProjectConfig.from_yaml(str(PROJECT_CONFIG), "dev")

    assert cfg.lakebase_instance == "lb-llmops-nicholasmisawo"
