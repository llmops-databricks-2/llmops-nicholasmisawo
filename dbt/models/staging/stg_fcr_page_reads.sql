-- Every disclosures page that loaded, as the query that found its PDFs (page:<url>)
select
    query,
    read_at,
    landed_at
from {{ source('landed', 'raw_fcr_page_reads') }}
