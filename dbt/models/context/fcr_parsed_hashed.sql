-- Each parsed FCR still in the register, with a hash of its content, so a changed parse is told apart
-- from an unchanged one
select
    parse.document_id,
    parse.parsed_content,
    {{ dbt_context_engineering.content_hash('parse.parsed_content') }} as content_hash
from {{ ref('fcr_parsed_documents') }} as parse
inner join {{ ref('int_fcr_documents') }} as fcr
    on fcr.document_id = parse.document_id
