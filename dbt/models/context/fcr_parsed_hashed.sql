-- Each parsed FCR with a hash of its content, so a changed parse is told apart from an unchanged one
select
    document_id,
    parsed_content,
    {{ dbt_context_engineering.content_hash('parsed_content') }} as content_hash
from {{ ref('stg_fcr_parsed_documents') }}
