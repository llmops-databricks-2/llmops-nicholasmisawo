-- One row per insurer on the entity list, with the FCRs found for it: its class and business, the
-- reporting years it has FCRs for (an FCR for a year is published the year after), and how many are
-- downloaded. An insurer with no FCR found has fcrs_found = 0 and is still searched for, so "who is
-- missing an FCR?" is a query, not a search.
with fcrs as (
    select
        fcr.registered_name as name,
        count(*) as fcrs_found,
        count(file.volume_path) as fcrs_downloaded,
        array_sort(collect_set(fcr.reporting_year)) as reporting_years,
        max(fcr.reporting_year) as latest_reporting_year
    from {{ ref('int_fcr_register') }} as fcr
    left join {{ ref('stg_fcr_volume_files') }} as file
        on file.volume_path = fcr.volume_path
    group by fcr.registered_name
)

select
    entity.name,
    entity.fcr_class,
    entity.business,
    entity.licence_types,
    entity.group_assets_bn_usd,
    coalesce(fcrs.fcrs_found, 0) as fcrs_found,
    coalesce(fcrs.fcrs_downloaded, 0) as fcrs_downloaded,
    coalesce(fcrs.reporting_years, array()) as reporting_years,
    fcrs.latest_reporting_year,
    fcrs.name is not null as has_fcr
from {{ ref('stg_fcr_entities') }} as entity
left join fcrs
    on fcrs.name = entity.name
