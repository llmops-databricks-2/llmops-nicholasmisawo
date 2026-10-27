-- The reference list of entities (insurers and reinsurers), with its ';'-separated columns split into arrays
select
    name,
    assets_bn_usd,
    filter(transform(split(coalesce(domains, ''), ';'), value -> trim(value)), value -> value != '') as domains,
    filter(transform(split(coalesce(previous_name, ''), ';'), value -> trim(value)), value -> value != '') as previous_names,
    filter(transform(split(coalesce(fcr_pages, ''), ';'), value -> trim(value)), value -> value != '') as fcr_pages
from {{ ref('bermuda_entities') }}
