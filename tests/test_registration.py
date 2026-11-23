"""Tests for what's logged with the agent: its model config, metric names and release tags."""

from pathlib import Path

from fcr_curator.config import ProjectConfig
from fcr_curator.registration import Release, agent_model_config, prefixed_metrics

PROJECT_CONFIG = Path(__file__).resolve().parents[1] / "project_config.yml"


def test_agent_model_config_rebuilds_the_same_project_config() -> None:
    """agent_model.py rebuilds the agent from this, so the project config must survive the JSON round trip."""
    config = ProjectConfig.from_yaml(str(PROJECT_CONFIG), "dev")

    model_config = agent_model_config(config, "dev", "space-1")

    assert ProjectConfig.model_validate(model_config["project"]) == config
    assert (model_config["env"], model_config["genie_space_id"]) == ("dev", "space-1")


def test_prefixed_metrics_keep_the_two_splits_apart() -> None:
    """The dev and holdout means are logged side by side under their split's name."""
    assert prefixed_metrics("holdout", {"correctness/mean": 0.5}) == {"holdout/correctness/mean": 0.5}


def test_release_tags_name_the_commit_and_the_job_run() -> None:
    """A version records what produced it."""
    assert Release("bd27c59", "916596661469909").tags() == {"git_sha": "bd27c59", "job_run_id": "916596661469909"}


def test_registered_model_name_is_in_the_environments_schema() -> None:
    """Each environment registers to its own catalog."""
    names = {env: ProjectConfig.from_yaml(str(PROJECT_CONFIG), env).registered_model_name for env in ("dev", "prd")}

    assert names == {"dev": "llmops_dev.nicholasmisawo.fcr_agent", "prd": "llmops_prd.nicholasmisawo.fcr_agent"}
