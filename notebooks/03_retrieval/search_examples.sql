-- Search the FCR knowledge base from the UI, without Python.
--
-- The same searches as search_knowledge_base.py, for one question about Wilton Re's 2024 capital, written as
-- the rewritten query would run them: search text "solvency ratio capital requirement", filters company
-- "wilton" and reporting year 2024. Query rewriting, reranking and the token budget need the chat model,
-- so they're only in the Python notebook.
--
-- The knowledge base (dbt's fcr_knowledge_base) has: source_key, text, account_key (the company), ts
-- (31 December of the reporting year), citation_url (the FCR opened at the chunk's page) and embedding.
-- system.ai.gte-large-en is the model behind course_ops.gateway.embed (the same text embeds identically
-- through both), so a question embedded here can be compared with the stored embeddings.


-- 1. Semantic search with filters
-- Where: Databricks SQL editor, any SQL warehouse.
-- Delete the WHERE line to see unfiltered results pull in other companies.

WITH question AS (
  SELECT ai_query('system.ai.gte-large-en', 'solvency ratio capital requirement') AS vector
)
SELECT account_key AS company, year(ts) AS reporting_year, citation_url, left(text, 100) AS excerpt,
       round(aggregate(zip_with(kb.embedding, question.vector, (a, b) -> a * b), 0D, (total, x) -> total + x)
             / (sqrt(aggregate(kb.embedding, 0D, (total, x) -> total + x * x))
                * sqrt(aggregate(question.vector, 0D, (total, x) -> total + x * x))), 3) AS similarity
FROM llmops_dev.nicholasmisawo.fcr_knowledge_base AS kb CROSS JOIN question
WHERE year(ts) = 2024 AND concat_ws(' ', account_key, citation_url) ILIKE '%wilton%'
ORDER BY similarity DESC LIMIT 5;


-- 2. The question's embedding, for the hybrid search in Lakebase (which can't embed text)
-- Where: Databricks SQL editor. Copy the whole [...] list it returns.

SELECT to_json(ai_query('system.ai.gte-large-en', 'solvency ratio capital requirement')) AS vector;


-- 3. Keyword search with filters
-- Where: Lakebase SQL editor, project lb-llmops-nicholasmisawo, branch production, database databricks_postgres.

SELECT account_key AS company, extract(year from ts) AS reporting_year, citation_url, left(text, 100) AS excerpt,
       round(ts_rank_cd(to_tsvector('english', text), words)::numeric, 3) AS rank
FROM nicholasmisawo.fcr_knowledge_base_lakebase_dev, websearch_to_tsquery('english', 'solvency or ratio or capital or requirement') AS words
WHERE extract(year from ts) = 2024 AND concat_ws(' ', account_key, citation_url) ILIKE '%wilton%'
  AND to_tsvector('english', text) @@ words
ORDER BY rank DESC LIMIT 5;


-- 4. Hybrid search: semantic and keyword fused with Reciprocal Rank Fusion (k = 60)
-- Where: Lakebase SQL editor. Replace <PASTE VECTOR> with the list from query 2, keeping the quotes.

WITH filtered AS (
  SELECT * FROM nicholasmisawo.fcr_knowledge_base_lakebase_dev
  WHERE extract(year from ts) = 2024 AND concat_ws(' ', account_key, citation_url) ILIKE '%wilton%'
),
semantic AS (
  SELECT source_key, row_number() OVER (ORDER BY embedding <=> '<PASTE VECTOR>'::vector) AS position
  FROM filtered ORDER BY position LIMIT 20
),
keyword AS (
  SELECT source_key, row_number() OVER (ORDER BY ts_rank_cd(to_tsvector('english', text), words) DESC) AS position
  FROM filtered, websearch_to_tsquery('english', 'solvency or ratio or capital or requirement') AS words
  WHERE to_tsvector('english', text) @@ words ORDER BY position LIMIT 20
)
SELECT kb.account_key AS company, extract(year from kb.ts) AS reporting_year, kb.citation_url, left(kb.text, 100) AS excerpt,
       round(sum(1.0 / (60 + ranked.position)), 4) AS rrf_score
FROM (SELECT * FROM semantic UNION ALL SELECT * FROM keyword) AS ranked JOIN filtered AS kb USING (source_key)
GROUP BY kb.source_key, kb.account_key, kb.ts, kb.citation_url, kb.text
ORDER BY rrf_score DESC LIMIT 5;
