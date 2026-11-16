"""Your first test: project_config.yml loads with the course's ProjectConfig for dev.

`ci.yml` runs every test under tests/ on each pull request and each push to main; add yours here.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from fcr_curator.config import AgentSettings, ProjectConfig

PROJECT_CONFIG = Path(__file__).resolve().parents[1] / "project_config.yml"


def test_project_config_loads_for_dev() -> None:
    """Load the provisioned settings without requiring optional lecture resources."""
    cfg = ProjectConfig.from_yaml(str(PROJECT_CONFIG), "dev")

    assert (cfg.full_schema_name, cfg.volume) == ("llmops_dev.nicholasmisawo", "fcr_documents")
    assert cfg.warehouse_id == "86722fe1a817f6aa"


def test_project_config_inherits_top_level_usage_policy() -> None:
    """The usage policy is set once at the top of the file and applies to every environment."""
    for env in ("dev", "acc", "prd"):
        cfg = ProjectConfig.from_yaml(str(PROJECT_CONFIG), env)

        assert cfg.usage_policy_id == "ffe08ae3-a783-3201-ab0c-2fff24538807"


def test_project_config_names_the_gateway_service_the_openai_client_calls() -> None:
    """The OpenAI client needs the Unity Gateway service; the serving endpoint is for SQL and AI Search."""
    cfg = ProjectConfig.from_yaml(str(PROJECT_CONFIG), "dev")

    assert cfg.embedding_service == "course_ops.gateway.embed"
    assert cfg.embedding_endpoint == "databricks-gte-large-en"


def test_project_config_loads_lakebase_instance() -> None:
    """Each environment names its provisioned Lakebase project."""
    cfg = ProjectConfig.from_yaml(str(PROJECT_CONFIG), "dev")

    assert cfg.lakebase_instance == "lb-llmops-nicholasmisawo"


def test_project_config_reads_the_system_prompt_and_tool_guidance_from_the_prompts_folder() -> None:
    """Every environment gets the prompt in system_prompt_file, tool guidance included."""
    prompt = (PROJECT_CONFIG.parent / "prompts/fcr_curator.txt").read_text(encoding="utf-8").strip()
    for env in ("dev", "acc", "prd"):
        cfg = ProjectConfig.from_yaml(str(PROJECT_CONFIG), env)

        assert cfg.system_prompt == prompt
        assert "HOW TO USE THE TOOLS" in cfg.system_prompt


def test_project_config_gives_every_environment_the_agent_limits() -> None:
    """The agent section is set once at the top of the file and applies to every environment."""
    for env in ("dev", "acc", "prd"):
        agent = ProjectConfig.from_yaml(str(PROJECT_CONFIG), env).agent

        assert (agent.max_tool_rounds, agent.history_messages, agent.max_search_results) == (8, 20, 10)


def test_agent_settings_refuse_a_default_search_count_above_the_maximum() -> None:
    """A default search_fcr would reject is caught when the config loads."""
    with pytest.raises(ValidationError, match="default_search_results"):
        AgentSettings(
            max_tool_rounds=8,
            history_messages=20,
            answer_max_tokens=4000,
            default_search_results=11,
            max_search_results=10,
            excerpt_chars=800,
            max_page_chars=12000,
        )
