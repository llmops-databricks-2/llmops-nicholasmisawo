-- Every FCR PDF any run has found, as the latest run that found it returned it, worked out from the
-- facts the search task appends. A URL is delisted once a later run asked again every query that ever
-- found it (searches, and disclosures pages that loaded) and none of them returned it; a run that finds
-- it again lists it again. A query a run didn't ask says nothing about the URLs it found before.
with hits as (
    select
        *,
        row_number() over (partition by document_id order by landed_at desc) as recency
    from {{ ref('stg_fcr_search_hits') }}
),

found as (
    select
        document_id,
        array_sort(array_distinct(flatten(collect_list(search_queries)))) as search_queries,
        min(ingestion_timestamp) as first_seen_at,
        max(landed_at) as last_found_at
    from hits
    group by document_id
),

asked_by_run as (
    select
        landed_at,
        collect_set(query) as queries
    from (
        select landed_at, query from {{ ref('stg_fcr_searches') }}
        union all
        select landed_at, query from {{ ref('stg_fcr_page_reads') }}
    ) as asked
    group by landed_at
),

delisted as (
    select distinct found.document_id
    from found
    inner join asked_by_run as asked
        on asked.landed_at > found.last_found_at
    where size(found.search_queries) > 0
        and size(array_except(found.search_queries, asked.queries)) = 0
)

select
    latest.document_id,
    latest.title,
    latest.snippet,
    latest.pdf_url,
    latest.displayed_link,
    latest.site_name,
    latest.domain,
    found.search_queries,
    latest.search_rank,
    latest.source,
    latest.ingestion_timestamp,
    found.first_seen_at,
    delisted.document_id is null as is_listed
from hits as latest
inner join found
    on found.document_id = latest.document_id
left join delisted
    on delisted.document_id = latest.document_id
where latest.recency = 1
