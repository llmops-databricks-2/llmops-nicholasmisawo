-- Every attempt to fetch an FCR's PDF, and its outcome
select
    document_id,
    file_url,
    outcome,
    attempted_at,
    landed_at
from {{ source('landed', 'raw_fcr_download_attempts') }}
