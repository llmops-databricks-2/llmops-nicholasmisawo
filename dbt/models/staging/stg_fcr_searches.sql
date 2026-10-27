-- Every billed SerpAPI search, one row per results page
select
    query,
    reason,
    page_start,
    results,
    fcr_results,
    searched_at,
    landed_at
from {{ source('landed', 'raw_fcr_searches') }}
