# Docs

How the FCR Curator works and what we found building it, in reading order: the setup, the map of the
layers, each pipeline stage from finding FCRs to answering questions, then the context engineering that
spans them. Each stage page explains how it works, then records the findings from running it on real FCRs.

| Page | Covers |
|---|---|
| [01 Environment](01-environment.md) | Provisioned resources, model services, environments and CI |
| [02 Layers and dbt](02-layers-and-dbt.md) | The layers, the dbt models (laid out like jaffle-logistics) and where the context engineering package is used |
| [03 Data pipeline](03-data-pipeline.md) | The job, finding FCRs on a search budget and on disclosures pages, curation, the raw tables |
| [04 Chunking](04-chunking.md) | Parsing, the two chunking strategies compared, chunk boundaries, the embedding window |
| [05 Lakebase](05-lakebase.md) | Embeddings and the Lakebase copy of the knowledge base |
| [06 Search](06-search.md) | Semantic, filtered, keyword and hybrid search, reranking, query rewriting, the token budget |
| [07 Context engineering](07-context-engineering.md) | Every strategy that controls what reaches the model, and where it lives |
