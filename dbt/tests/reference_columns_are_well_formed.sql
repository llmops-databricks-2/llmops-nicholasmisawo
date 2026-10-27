-- Detail rows whose multi-value columns can't be used: a domain with a path never matches a host, and a
-- page without https:// can't be fetched. Any row returned fails the test.
select bma_name, domains, fcr_pages
from {{ ref('fcr_entity_details') }}
where domains like '%/%'
   or exists(split(coalesce(fcr_pages, ''), ';'), page -> trim(page) != '' and not trim(page) rlike '^https?://')
