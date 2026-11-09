"""The agent's Unity Catalog function: get_fcr_details, governed SQL over the FCR register.

A UC function lives next to the tables it reads, under Unity Catalog's permissions, and the managed MCP
server for UC functions (/api/2.0/mcp/functions/<catalog>/<schema>/<function>) offers it to an agent as a
tool. The agent calls it without importing or reimplementing its SQL.

get_fcr_details lists the FCRs registered for one insurer, newest first, as a JSON array. The insurer is
matched like curation.institution_key: letters and digits only, legal suffixes dropped, so "Wilton Re"
finds "Wilton Reinsurance Bermuda Limited" (and its domain, wiltonre.bm). A key shorter than
MIN_NAME_KEY_CHARS matches nothing rather than every insurer.

Registering runs on the SQL warehouse through the Statement Execution API, so it needs no Spark and no
psycopg, and works from a laptop or a job.
"""

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState
from loguru import logger

from fcr_curator.config import ProjectConfig
from fcr_curator.curation import MIN_NAME_KEY_CHARS
from fcr_curator.tables import REGISTER_TABLE

GET_FCR_DETAILS = "get_fcr_details"
ENTITIES_TABLE = "dim_fcr_entities"  # One row per listed insurer, with its licence class (dbt marts)
STATEMENT_WAIT = "50s"  # The longest the Statement Execution API waits before returning a pending statement

# curation.LEGAL_SUFFIXES in Java regex syntax; doubled backslashes because it sits in a SQL string literal
LEGAL_SUFFIXES_REGEX = r"(?i)\\b(ltd|limited|inc|plc|llc|sa|se|ag|group)\\b\\.?"


def insurer_key_sql(name_sql: str) -> str:
    """Return SQL turning name_sql into curation.institution_key's form: suffixes dropped, letters and digits."""
    return f"regexp_replace(lower(regexp_replace({name_sql}, '{LEGAL_SUFFIXES_REGEX}', '')), '[^a-z0-9]', '')"


def get_fcr_details_query(config: ProjectConfig, insurer_sql: str) -> str:
    """Return the query get_fcr_details runs, for insurer_sql (its parameter, or a literal when testing)."""
    schema = config.full_schema_name
    insurer_key = insurer_key_sql(insurer_sql)
    return f"""SELECT to_json(sort_array(collect_list(named_struct(
        'reporting_year', r.reporting_year,
        'registered_name', r.registered_name,
        'fcr_class', e.fcr_class,
        'business', e.business,
        'file_url', r.file_url,
        'downloaded', r.downloaded_at IS NOT NULL
    )), false))
    FROM {schema}.{REGISTER_TABLE} r
    JOIN {schema}.{ENTITIES_TABLE} e ON e.name = r.registered_name
    WHERE length({insurer_key}) >= {MIN_NAME_KEY_CHARS}
      AND regexp_replace(lower(concat_ws(' ', r.registered_name, r.domain)), '[^a-z0-9]', '')
          LIKE concat('%', {insurer_key}, '%')"""


def get_fcr_details_sql(config: ProjectConfig) -> str:
    """Return the statement that creates or replaces get_fcr_details in the config's schema."""
    return f"""CREATE OR REPLACE FUNCTION {config.full_schema_name}.{GET_FCR_DETAILS}(
    insurer STRING COMMENT 'The insurer or reinsurer, e.g. ''Wilton Re''; matched loosely, legal suffixes ignored'
)
RETURNS STRING
COMMENT 'Lists the Financial Condition Reports registered for one Bermuda insurer or reinsurer, newest first, as a JSON array of reporting_year, registered_name, fcr_class, business, file_url and downloaded. Returns [] when none match.'
RETURN ({get_fcr_details_query(config, "insurer")})"""


def uc_function_mcp_url(host: str, config: ProjectConfig, function: str) -> str:
    """Return the managed MCP server URL that serves one UC function as a tool."""
    return f"{host.rstrip('/')}/api/2.0/mcp/functions/{config.catalog}/{config.schema}/{function}"


def register_uc_functions(workspace: WorkspaceClient, config: ProjectConfig) -> list[str]:
    """Create or replace the agent's UC functions; return their full names.

    Raises RuntimeError, quoting the warehouse's message, when a statement doesn't succeed.
    """
    function = f"{config.full_schema_name}.{GET_FCR_DETAILS}"
    response = workspace.statement_execution.execute_statement(
        statement=get_fcr_details_sql(config), warehouse_id=config.warehouse_id, wait_timeout=STATEMENT_WAIT
    )
    state = response.status.state if response.status else None
    if state != StatementState.SUCCEEDED:
        error = response.status.error.message if response.status and response.status.error else state
        raise RuntimeError(f"Registering {function} didn't succeed: {error}")
    logger.info(f"Registered {function}")
    return [function]
