-- ai_parse_document output per FCR; an empty error list is stored as the text 'null' by older runs
select
    document_id,
    parsed_content,
    nullif(nullif(parse_errors, 'null'), '[]') as parse_errors
from {{ source('landed', 'silver_parsed_fcr_documents') }}
where parsed_content is not null
