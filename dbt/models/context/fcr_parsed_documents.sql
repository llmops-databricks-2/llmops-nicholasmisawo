-- ai_parse_document output for each downloaded FCR. Parsing is the costly step, so each PDF is parsed
-- once: a run parses only FCRs not here yet, and full_refresh is off, so even dbt build --full-refresh
-- never parses every PDF again. Figure descriptions are off: they cost extra, and chunks only use text
-- and tables. A parse is kept when its FCR leaves the register (fcr_parsed_hashed leaves it out), so
-- an FCR that comes back isn't parsed twice.
{{ config(materialized='incremental', incremental_strategy='append', full_refresh=false, tblproperties={'quality': 'silver'}) }}

with pending as (
    select document_id, volume_path
    from {{ ref('int_fcr_documents') }}
    {% if is_incremental() %}
    where document_id not in (select document_id from {{ this }})
    {% endif %}
),

parsed as (
    select
        pending.document_id,
        pending.volume_path,
        ai_parse_document(file.content, map('version', '2.0', 'descriptionElementTypes', '')) as parsed
    from pending
    inner join {{ fcr_volume_files() }} as file
        on regexp_replace(file.path, '^dbfs:', '') = pending.volume_path
)

select
    document_id,
    volume_path,
    to_json(parsed) as parsed_content,
    -- NULL when every page parsed, rather than the text 'null' or an empty list
    nullif(nullif(to_json(parsed:error_status), 'null'), '[]') as parse_errors,
    current_timestamp() as parsed_at
from parsed
