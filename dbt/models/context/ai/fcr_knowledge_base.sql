{#
  The serving contract the assistant searches, in the package's knowledge_base shape: one row per
  chunk, with the company as account_key, the reporting year's end as ts, and a citation_url that
  opens the FCR at the cited page. Incremental: only new or changed rows are merged, so the Change
  Data Feed (which Lakebase follows) carries real changes. By the package's design it never deletes;
  the orphan test below catches chunks that left upstream.
#}
{{ config(materialized='incremental', incremental_strategy='merge', unique_key='source_key') }}

{{ dbt_context_engineering.knowledge_base([{
    'relation': ref('fcr_chunks_embedded'),
    'source_type': 'fcr',
    'source_id': 'id',
    'account_key': 'company',
    'text': 'chunk_text',
    'embedding': 'embedding',
    'timestamp': 'make_date(reporting_year, 12, 31)',
    'citation_url': "concat(file_url, '#page=', page_id + 1)",
}]) }}
