-- The entity list: the BMA-licensed insurers in search scope, with what we know beyond the BMA's list.
-- The search plan searches for each by name (and alias), in licence-class order, largest groups first;
-- curation names every FCR after the insurer it matches, and keeps only those FCRs.
select
    coalesce(details.display_name, insurer.name) as name,
    insurer.bma_name,
    insurer.fcr_class,
    insurer.fcr_business as business,
    insurer.licence_types,
    insurer.fcr_search_priority,
    details.group_assets_bn_usd,
    filter(transform(split(coalesce(details.domains, ''), ';'), value -> lower(trim(value))), value -> value != '') as domains,
    filter(transform(split(coalesce(details.aliases, ''), ';'), value -> trim(value)), value -> value != '') as aliases,
    filter(transform(split(coalesce(details.fcr_pages, ''), ';'), value -> trim(value)), value -> value != '') as fcr_pages
from {{ ref('stg_bma_insurers') }} as insurer
left join {{ ref('fcr_entity_details') }} as details
    on details.bma_name = insurer.bma_name
where insurer.in_search_scope
