"""The FCR agent's MLflow models-from-code entry point (lecture 4.4).

mlflow.pyfunc.log_model logs this file, not a pickled agent. When the model loads (in serving, or to check
the input example), this runs with the model_config given at logging: the environment, the Genie space and
the project configuration, so the agent is built just as FCRAgent.from_config builds it in a notebook.
"""

from mlflow.models import ModelConfig, set_model

from fcr_curator.agent import FCRAgent
from fcr_curator.config import ProjectConfig

model_config = ModelConfig().to_dict()
set_model(
    FCRAgent.from_config(
        ProjectConfig.model_validate(model_config["project"]),
        model_config["env"],
        model_config.get("genie_space_id"),
    )
)
