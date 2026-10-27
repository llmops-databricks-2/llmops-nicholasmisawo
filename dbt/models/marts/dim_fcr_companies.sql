-- One row per company with an FCR or on the reference list: which years it has FCRs for and whether
-- it's on the list, so "who has no FCR for last year?" is a query, not a search
with coverage as (
    select
        company,
        max(registered_name) is not null as on_reference_list,
        count(*) as fcrs,
        array_sort(collect_set(reporting_year)) as reporting_years,
        max(reporting_year) as latest_reporting_year
    from {{ ref('int_fcr_documents') }}
    group by company
)

select
    coalesce(coverage.company, entity.name) as company,
    coalesce(coverage.on_reference_list, entity.name is not null) as on_reference_list,
    entity.assets_bn_usd,
    coalesce(coverage.fcrs, 0) as fcrs,
    coverage.reporting_years,
    coverage.latest_reporting_year
from coverage
full outer join {{ ref('stg_fcr_entities') }} as entity
    on entity.name = coverage.company
