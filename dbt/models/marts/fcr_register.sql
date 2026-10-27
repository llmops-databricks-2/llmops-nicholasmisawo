-- The FCR register the Python tasks read: every curated FCR, with when its PDF arrived in the volume
-- (empty until the download task fetches it) and how its downloads have gone. search_fcrs skips
-- institutions and years it already holds; download_fcrs fetches what's missing.
with attempts as (
    select
        document_id,
        count(*) as download_attempts,
        max_by(outcome, attempted_at) as last_download_outcome
    from {{ ref('stg_fcr_download_attempts') }}
    group by document_id
)

select
    fcr.*,
    file.downloaded_at,
    coalesce(attempts.download_attempts, 0) as download_attempts,
    attempts.last_download_outcome
from {{ ref('int_fcr_register') }} as fcr
left join {{ ref('stg_fcr_volume_files') }} as file
    on file.volume_path = fcr.volume_path
left join attempts
    on attempts.document_id = fcr.document_id
