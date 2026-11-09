# Docs

How the Financial Condition Report (FCR) Curator works, in reading order: the setup, the map of the
layers, each pipeline stage from finding FCRs to answering questions, then the context engineering that
spans them. Each stage page explains how it works, then records the findings from running it on real FCRs.

| Page | Covers |
|---|---|
| [01 Environment](01-environment.md) | Provisioned resources, model services, environments and CI |
| [02 Layers and dbt](02-layers-and-dbt.md) | The layers, the dbt models and where the dbt context engineering package is used |
| [03 Data pipeline](03-data-pipeline.md) | The job, finding FCRs on a search budget and on disclosures pages, curation, the raw tables |
| [04 Chunking](04-chunking.md) | Parsing, the two chunking strategies compared, chunk boundaries, the embedding window |
| [05 Lakebase](05-lakebase.md) | Embeddings and the Lakebase copy of the knowledge base |
| [06 Search](06-search.md) | Semantic, filtered, keyword and hybrid search, reranking, query rewriting, the token budget |
| [07 Context engineering](07-context-engineering.md) | Every strategy that controls what reaches the model, and where it lives |
| [08 Agent](08-agent.md) | The agent's tools, orchestration (a Mermaid sequence diagram) and session memory |

## Submissions

Dated snapshots of what was delivered for each lecture, in `submissions/`.

| Lecture | Submission |
|---|---|
| 2 | [The retrieval foundation](submissions/lecture-02-retrieval-foundation.md): processed documents and chunks, chunking strategies compared, the Lakebase synced table, semantic, filtered and hybrid search |
| 3 | [An agent with tools and memory](submissions/lecture-03-agent.md): three tools (Lakebase pgvector, a UC function through MCP, a custom function) plus Genie, the orchestration diagram, session memory in Lakebase |
