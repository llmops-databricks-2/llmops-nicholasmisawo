-- Each downloaded FCR with the company it belongs to: its reference-list entity when matched, else its title's name
select
    fcr.document_id,
    coalesce(fcr.registered_name, fcr.institution) as company,
    fcr.registered_name,
    fcr.institution,
    fcr.domain,
    fcr.title,
    fcr.reporting_year,
    fcr.file_url,
    fcr.volume_path,
    file.downloaded_at,
    entity.assets_bn_usd
from {{ ref('int_fcr_register') }} as fcr
inner join {{ ref('stg_fcr_volume_files') }} as file
    on file.volume_path = fcr.volume_path
left join {{ ref('stg_fcr_entities') }} as entity
    on entity.name = fcr.registered_name
