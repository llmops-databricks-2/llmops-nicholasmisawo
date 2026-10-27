-- Each downloaded FCR with the insurer on the entity list it belongs to (the register keeps no other)
select
    fcr.document_id,
    entity.name as company,
    entity.fcr_class,
    entity.business,
    fcr.registered_name,
    fcr.institution,
    fcr.domain,
    fcr.title,
    fcr.reporting_year,
    fcr.file_url,
    fcr.volume_path,
    file.downloaded_at
from {{ ref('int_fcr_register') }} as fcr
inner join {{ ref('stg_fcr_volume_files') }} as file
    on file.volume_path = fcr.volume_path
inner join {{ ref('stg_fcr_entities') }} as entity
    on entity.name = fcr.registered_name
