# Docs

How the FCR Curator works and what we found building it, in pipeline order. Each page explains a stage,
then records the findings from running it on real FCRs.

| Page | Covers |
|---|---|
| [01 Data pipeline](01-data-pipeline.md) | Finding FCRs with SerpAPI on a search budget, curating them, the medallion tables |
| [02 Chunking](02-chunking.md) | Parsing, the two chunking strategies compared, chunk boundaries, the embedding window |
| [03 Lakebase](03-lakebase.md) | Embeddings and the Lakebase synced table |
| [04 Search](04-search.md) | Semantic, filtered, keyword and hybrid search, reranking, query rewriting, the token budget |
| [05 Layers and dbt](05-layers.md) | The layers, the dbt models (laid out like jaffle-logistics) and where the context engineering package is used |
| [Context engineering](context-engineering.md) | Every strategy that controls what reaches the model, and where it lives |
| [Environment](environment.md) | Provisioned resources, model services, environments and CI |
