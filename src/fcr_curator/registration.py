"""Log the FCR agent with its evaluation metrics, and register it in Unity Catalog (lecture 4.4).

As the course's log_register_agent, with real release identifiers: the commit and the job run are passed in,
where the course hard-codes "abc" and "unset". Logging and registering don't deploy anything, and registering
isn't a release gate: whether a version is good enough is read from its metrics, not from its being registered.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import mlflow
from mlflow import MlflowClient
from mlflow.entities.model_registry import ModelVersion

import fcr_curator
from fcr_curator.config import ProjectConfig

AGENT_MODEL_FILE = "agent_model.py"  # The models-from-code entry point, in the package
ENV_PACK = "databricks_model_serving"  # Packs the serverless environment with the version, as the course does


@dataclass(frozen=True)
class Release:
    """What produced a version: the code's commit and the job run that logged it."""

    git_sha: str
    job_run_id: str

    def tags(self) -> dict[str, str]:
        """Return the identifiers as tags for the run and the model version."""
        return {"git_sha": self.git_sha, "job_run_id": self.job_run_id}


def agent_model_config(config: ProjectConfig, env: str, genie_space_id: str | None) -> dict[str, Any]:
    """Return the model_config agent_model.py builds the agent from: the environment, Genie space and project."""
    return {"env": env, "genie_space_id": genie_space_id, "project": config.model_dump(by_alias=True, mode="json")}


def prefixed_metrics(prefix: str, metrics: dict[str, float]) -> dict[str, float]:
    """Return metrics under prefix (dev/correctness/mean), so the dev and holdout splits log side by side."""
    return {f"{prefix}/{name}": float(value) for name, value in metrics.items()}


def log_and_register(
    config: ProjectConfig,
    env: str,
    genie_space_id: str | None,
    release: Release,
    metrics: dict[str, float],
    input_example: dict[str, Any],
) -> ModelVersion:
    """Log the agent with its metrics, register it as a new version, and point the alias at it.

    Logging checks the input example by loading the agent and answering it, so it calls the model and tools.
    """
    package = Path(fcr_curator.__file__).parent
    with mlflow.start_run(run_name=f"fcr-agent-{datetime.now(UTC):%Y-%m-%d}", tags=release.tags()):
        model_info = mlflow.pyfunc.log_model(
            name="agent",
            python_model=str(package / AGENT_MODEL_FILE),
            code_paths=[str(package)],
            input_example=input_example,
            model_config=agent_model_config(config, env, genie_space_id),
        )
        mlflow.log_metrics(metrics)
    version = mlflow.register_model(
        model_uri=model_info.model_uri,
        name=config.registered_model_name,
        tags=release.tags(),
        env_pack=ENV_PACK,
    )
    MlflowClient().set_registered_model_alias(
        config.registered_model_name, config.evaluation.model_alias, version.version
    )
    return version
