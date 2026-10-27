-- Each FCR with the company it belongs to: its reference-list entity when matched, else its title's name
select
    fcr.document_id,
    coalesce(fcr.registered_name, fcr.institution) as company,
    fcr.registered_name,
    fcr.institution,
    fcr.domain,
    fcr.title,
    fcr.reporting_year,
    fcr.file_url,
    entity.assets_bn_usd
from {{ ref('stg_fcr_documents') }} as fcr
left join {{ ref('stg_fcr_entities') }} as entity
    on entity.name = fcr.registered_name
