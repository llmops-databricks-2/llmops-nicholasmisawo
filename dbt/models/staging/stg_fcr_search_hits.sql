-- Every FCR PDF each search run found
select
    document_id,
    title,
    snippet,
    pdf_url,
    displayed_link,
    site_name,
    domain,
    search_queries,
    search_rank,
    source,
    ingestion_timestamp,
    landed_at
from {{ source('landed', 'raw_fcr_search_hits') }}
