-- The BMA's licensed insurers, one row each, with what their licences mean for FCRs: an insurer files
-- an FCR if any of its licences does, is searched by its most important filing licence, and is on the
-- entity list if a filing licence is in search scope and it isn't in liquidation (one being wound up
-- isn't expected to publish another FCR).
with insurer_licences as (
    select
        name,
        explode(transform(split(licence_types, ';'), licence_type -> trim(licence_type))) as licence_type
    from {{ ref('bma_insurance_register') }}
),

licences as (
    select
        insurer_licences.name,
        licence.licence_type,
        licence.business,
        licence.files_fcr,
        licence.fcr_search_priority,
        licence.files_fcr and licence.in_search_scope as in_search_scope
    from insurer_licences
    inner join {{ ref('bma_licence_classes') }} as licence
        on licence.licence_type = insurer_licences.licence_type
),

by_insurer as (
    select
        name,
        array_sort(collect_set(licence_type)) as licence_types,
        array_sort(collect_set(business)) as businesses,
        bool_or(files_fcr) as files_fcr,
        min_by(licence_type, fcr_search_priority) as fcr_class,
        min_by(business, fcr_search_priority) as fcr_business,
        min(fcr_search_priority) as fcr_search_priority,
        bool_or(in_search_scope) as has_licence_in_search_scope
    from licences
    group by name
)

select
    insurer.name,
    insurer.bma_name,
    by_insurer.licence_types,
    by_insurer.businesses,
    by_insurer.files_fcr,
    by_insurer.fcr_class,
    by_insurer.fcr_business,
    by_insurer.fcr_search_priority,
    nullif(trim(insurer.liquidation_status), '') as liquidation_status,
    by_insurer.has_licence_in_search_scope and nullif(trim(insurer.liquidation_status), '') is null as in_search_scope,
    insurer.extracted_on
from {{ ref('bma_insurance_register') }} as insurer
inner join by_insurer
    on by_insurer.name = insurer.name
