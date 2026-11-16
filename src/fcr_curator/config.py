"""Configuration management for the FCR Curator."""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

# Folders searched for a project file: the current one and its parents
CONFIG_SEARCH_DEPTH = 3


class AgentSettings(BaseModel):
    """Limits on the agent's turns and its tools' results, shared by every environment."""

    max_tool_rounds: int = Field(..., gt=0, description="Rounds of tool calls before the model must answer")
    history_messages: int = Field(..., gt=0, description="A session's latest messages loaded each turn")
    answer_max_tokens: int = Field(..., gt=0, description="Tokens the model may spend on each reply")
    default_search_results: int = Field(..., gt=0, description="Passages search_fcr returns by default")
    max_search_results: int = Field(..., gt=0, description="Most passages search_fcr returns")
    excerpt_chars: int = Field(..., gt=0, description="Characters of each search_fcr passage")
    max_page_chars: int = Field(..., gt=0, description="Characters of a page extract_fcr_content returns")

    @model_validator(mode="after")
    def default_within_maximum(self) -> "AgentSettings":
        """Fail at load, not mid-turn, when search_fcr's default exceeds the maximum it accepts."""
        if self.default_search_results > self.max_search_results:
            raise ValueError("default_search_results must not exceed max_search_results")
        return self


class ProjectConfig(BaseModel):
    """Project configuration model."""

    usage_policy_id: str | None = Field(None, description="Usage policy id")
    catalog: str = Field(..., description="Unity Catalog name")
    db_schema: str = Field(..., description="Schema name", alias="schema")
    volume: str = Field(..., description="Volume name")
    llm_endpoint: str = Field(..., description="AI Gateway chat model service name")
    embedding_endpoint: str = Field(
        ..., description="Serving endpoint for ai_query, AI Search and DatabricksServingEndpoint"
    )
    embedding_service: str = Field(..., description="AI Gateway embedding model service, for the OpenAI client")
    experiment_path: str = Field(..., description="MLflow experiment path for this environment")
    warehouse_id: str = Field(..., description="Warehouse ID")
    lakebase_instance: str = Field(..., description="Lakebase project ID")

    serpapi_secret_scope: str = Field("fcr_curator", description="Databricks secret scope holding the SerpAPI key")
    serpapi_secret_key: str = Field("serpapi_api_key", description="Secret key name of the SerpAPI key")

    system_prompt: str = Field(..., description="System prompt for the FCR agent, read from system_prompt_file")
    agent: AgentSettings = Field(..., description="Limits on the agent's turns and tools")

    model_config = {"populate_by_name": True}

    @classmethod
    def from_yaml(cls, config_path: str, env: str = "dev") -> "ProjectConfig":
        """Load configuration from YAML file.

        Args:
            config_path: Path to the YAML configuration file
            env: Environment name (dev, acc, prd)

        Returns:
            ProjectConfig instance

        """
        if env not in ["prd", "acc", "dev"]:
            raise ValueError(f"Invalid environment: {env}. Expected 'prd', 'acc', or 'dev'")

        with open(config_path) as f:
            config_data = yaml.safe_load(f)

        if env not in config_data:
            raise ValueError(f"Environment '{env}' not found in config file")

        env_config = config_data[env]
        # The usage policy is shared by every environment, so it sits at the top level of the file
        if "usage_policy_id" in config_data:
            env_config["usage_policy_id"] = config_data["usage_policy_id"]
        if "agent" in config_data:
            env_config["agent"] = config_data["agent"]
        # The prompt is shared too, and kept in its own file (prompts/), relative to this one
        if "system_prompt_file" in config_data:
            prompt_path = Path(config_path).parent / config_data["system_prompt_file"]
            env_config["system_prompt"] = prompt_path.read_text(encoding="utf-8").strip()

        return cls(**env_config)

    @property
    def schema(self) -> str:
        """Alias for db_schema for backward compatibility."""
        return self.db_schema

    @property
    def full_schema_name(self) -> str:
        """Get fully qualified schema name."""
        return f"{self.catalog}.{self.db_schema}"


def find_project_file(relative_path: str) -> Path:
    """Return a file at the repository root, looking in the current folder and its parents.

    Notebooks run from notebooks/<stage>/, so files at the repository root are two levels up. An absolute
    path, or one found nowhere, is returned as given.
    """
    path = Path(relative_path)
    if path.is_absolute():
        return path
    folders = [Path.cwd(), *Path.cwd().parents][:CONFIG_SEARCH_DEPTH]
    return next((folder / path for folder in folders if (folder / path).exists()), path)


def load_config(config_path: str = "project_config.yml", env: str = "dev") -> ProjectConfig:
    """Load the settings for env from config_path (see find_project_file)."""
    return ProjectConfig.from_yaml(str(find_project_file(config_path)), env)


def get_widget(name: str, default: str | None = None) -> str | None:
    """Return a notebook widget or job parameter, or default when it's empty or there isn't one.

    As the course's get_widget does, so a notebook runs as a job task, interactively, and from a laptop.
    The missing widget's error differs by runtime (KeyError locally, a widget error on Databricks), so any
    error means the default.
    """
    try:
        from databricks.sdk.runtime import dbutils

        return dbutils.widgets.get(name).strip() or default
    except Exception:  # noqa: BLE001 - see the docstring
        return default
