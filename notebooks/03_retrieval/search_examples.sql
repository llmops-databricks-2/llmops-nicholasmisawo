-- Search the FCR chunks from the UI, without Python.
--
-- The same searches as search_fcr_chunks.py, for one question about Wilton Re's 2024 capital, written as
-- the rewritten query would run them: search text "solvency ratio capital requirement", filters
-- institution "wilton" and reporting year 2024. Query rewriting, reranking and the token budget need the
-- chat model, so they're only in the Python notebook.
--
-- system.ai.gte-large-en is the model behind course_ops.gateway.embed (the same text embeds identically
-- through both), so a question embedded here can be compared with the stored chunk embeddings.


-- 1. Semantic search with filters
-- Where: Databricks SQL editor, any SQL warehouse.
-- Delete the WHERE line to see unfiltered results pull in other insurers.

WITH question AS (
  SELECT ai_query('system.ai.gte-large-en', 'solvency ratio capital requirement') AS vector
)
SELECT coalesce(registered_name, institution) AS institution, reporting_year, page_id + 1 AS page, left(text, 100) AS excerpt,
       round(aggregate(zip_with(chunk.embedding, question.vector, (a, b) -> a * b), 0D, (total, x) -> total + x)
             / (sqrt(aggregate(chunk.embedding, 0D, (total, x) -> total + x * x))
                * sqrt(aggregate(question.vector, 0D, (total, x) -> total + x * x))), 3) AS similarity
FROM llmops_dev.nicholasmisawo.gold_fcr_chunks_by_section AS chunk CROSS JOIN question
WHERE reporting_year = 2024 AND concat_ws(' ', registered_name, institution, domain, title) ILIKE '%wilton%'
ORDER BY similarity DESC LIMIT 5;


-- 2. The question's embedding, for the hybrid search in Lakebase (which can't embed text)
-- Where: Databricks SQL editor. Copy the whole [...] list it returns.

SELECT to_json(ai_query('system.ai.gte-large-en', 'solvency ratio capital requirement')) AS vector;


-- 3. Keyword search with filters
-- Where: Lakebase SQL editor, project lb-llmops-nicholasmisawo, branch production, database databricks_postgres.

SELECT coalesce(registered_name, institution) AS institution, reporting_year, page_id + 1 AS page, left(text, 100) AS excerpt,
       round(ts_rank_cd(to_tsvector('english', text), words)::numeric, 3) AS rank
FROM nicholasmisawo.fcr_chunks_dev, websearch_to_tsquery('english', 'solvency or ratio or capital or requirement') AS words
WHERE reporting_year = 2024 AND concat_ws(' ', registered_name, institution, domain, title) ILIKE '%wilton%'
  AND to_tsvector('english', text) @@ words
ORDER BY rank DESC LIMIT 5;


-- 4. Hybrid search: semantic and keyword fused with Reciprocal Rank Fusion (k = 60)
-- Where: Lakebase SQL editor. Replace <PASTE VECTOR> with the list from query 2, keeping the quotes.

WITH filtered AS (
  SELECT * FROM nicholasmisawo.fcr_chunks_dev
  WHERE reporting_year = 2024 AND concat_ws(' ', registered_name, institution, domain, title) ILIKE '%wilton%'
),
semantic AS (
  SELECT id, row_number() OVER (ORDER BY embedding <=> '<PASTE VECTOR>'::vector) AS position
  FROM filtered ORDER BY position LIMIT 20
),
keyword AS (
  SELECT id, row_number() OVER (ORDER BY ts_rank_cd(to_tsvector('english', text), words) DESC) AS position
  FROM filtered, websearch_to_tsquery('english', 'solvency or ratio or capital or requirement') AS words
  WHERE to_tsvector('english', text) @@ words ORDER BY position LIMIT 20
)
SELECT coalesce(chunk.registered_name, chunk.institution) AS institution, chunk.reporting_year, chunk.page_id + 1 AS page, left(chunk.text, 100) AS excerpt,
       round(sum(1.0 / (60 + ranked.position)), 4) AS rrf_score
FROM (SELECT * FROM semantic UNION ALL SELECT * FROM keyword) AS ranked JOIN filtered AS chunk USING (id)
GROUP BY chunk.id, chunk.registered_name, chunk.institution, chunk.reporting_year, chunk.page_id, chunk.text
ORDER BY rrf_score DESC LIMIT 5;
