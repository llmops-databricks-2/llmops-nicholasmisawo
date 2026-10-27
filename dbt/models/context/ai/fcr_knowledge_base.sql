{#
  The serving contract the assistant searches, in the package's knowledge_base shape: one row per
  chunk, with the company as account_key, the reporting year's end as ts, a citation_url that opens
  the FCR at the cited page, and the insurer's FCR licence class (Class 4, Long Term Class E, Class 3B
  or Group) as classification, so search can filter on it; its business follows from the class. Incremental: only new or changed rows are merged, so the Change
  Data Feed (which Lakebase follows) carries real changes. The package's merge never deletes, so the
  post-hook removes chunks whose FCR left the register (say, its insurer left the entity list); the
  deletes reach Lakebase through the Change Data Feed too.
#}
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key='source_key',
    post_hook="delete from {{ this }} where source_id not in (select id from {{ ref('fcr_chunks_embedded') }})",
) }}

{{ dbt_context_engineering.knowledge_base([{
    'relation': ref('fcr_chunks_embedded'),
    'source_type': 'fcr',
    'source_id': 'id',
    'account_key': 'company',
    'text': 'chunk_text',
    'embedding': 'embedding',
    'timestamp': 'make_date(reporting_year, 12, 31)',
    'citation_url': "concat(file_url, '#page=', page_id + 1)",
    'classification': 'fcr_class',
}]) }}
