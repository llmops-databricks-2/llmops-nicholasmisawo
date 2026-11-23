# 10 Evaluation

How the FCR agent is evaluated (lecture 4.3). Tracing ([09 Tracing](09-tracing.md)) shows how an answer
was produced; evaluation asks whether it's right. A turn can finish without errors and still be wrong.

## The cases

[`evaluation/fcr_eval_cases.yml`](../evaluation/fcr_eval_cases.yml) holds 16 cases, reviewed by hand.
Each has a question, what a good answer states (`expected_facts`) or does (`guidelines`), and the FCR pages
or register rows that support it (`evidence`). The last of these lets a judge check claims against the
source, not just against the answer. `src/fcr_curator/evaluation.py` loads them and turns them into
`mlflow.genai.evaluate` records.

| Split | Cases | Use |
|---|---|---|
| dev | 10 | Find failures, and check fixes |
| holdout | 6 | Kept back to judge whether a change helped, so the agent isn't tuned to the questions that grade it |

Both splits cover the same kinds of question, so a change is judged on what it was tuned for:

| Category | dev | holdout | Covers |
|---|---|---|---|
| `single_insurer` | Wilton Re 2024 capital vs ECR; Argo Re's stated ratio | Argo Re's compliance | One insurer's figures, with a worked-out or a stated ratio |
| `compare_years` | Somerset Re 2023 to 2024 | | A change across years, with figures in an unstated unit |
| `follow_up` | Wilton Re, then "the year before?" | Somerset Re, then "the year before?" | Context carried from the session's history |
| `count_genie` | Class 4 with a 2024 FCR; no FCR by class | The six Class 4 names | Register questions, for Genie |
| `missing_evidence` | Wilton Re 2025 | Arch Reinsurance 2024 | No FCR to answer from: say so, invent nothing |
| `out_of_scope` | Investment advice | "Ignore your instructions…" | Declining, and staying in scope |
| `known_failure` | AXA's short name; capital vs MSM | | Failures already found in traces |
| `ambiguous_name` | | Two Wilton Re companies | One short name, two companies |

## Where the expected answers come from

Every figure was read on 2026-11-23 from the tables the agent's tools use, so a case can't expect what the
agent can't reach. The FCR figures come from the pages cited in each case:

```sql
-- One insurer's capital, ECR and MSM pages (here Wilton Re, 2023 and 2024)
SELECT year(ts) AS reporting_year, citation_url, text
FROM llmops_dev.nicholasmisawo.fcr_knowledge_base
WHERE account_key = 'Wilton Reinsurance Bermuda Limited' AND year(ts) IN (2023, 2024)
  AND (lower(text) LIKE '%minimum margin of solvency%' OR lower(text) LIKE '%eligible capital%'
       OR lower(text) LIKE '%enhanced capital requirement%')
  AND text RLIKE '[0-9]{3}';
```

The register figures come from `dim_fcr_entities`:

```sql
-- Insurers with and without an FCR, by licence class
SELECT fcr_class, count(*) AS insurers,
       sum(CASE WHEN NOT has_fcr THEN 1 ELSE 0 END) AS without_fcr,
       sum(CASE WHEN array_contains(reporting_years, 2024) THEN 1 ELSE 0 END) AS with_2024
FROM llmops_dev.nicholasmisawo.dim_fcr_entities
GROUP BY ALL;
```

If the pipeline runs again and finds more FCRs, these numbers change. Rerun the queries and update the
cases before comparing results across that change.

## What writing the cases found

- **"US$ million" is right for Wilton Re.** Its FCR says "in $ millions" on page 15, which settles the
  open question from lecture 3. Somerset Re's page doesn't name its unit, so its case expects no unit.
- **The "3.5 times" claim was wrong, not just unsupported.** Wilton Re's 2024 MSM is 377.0, so eligible
  capital was about 4.66 times it. The case `wilton_capital_vs_msm` checks for it.
- **"Wilton Re" is two companies.** Wilton Reinsurance Bermuda Limited has FCRs for 2022 to 2024; Wilton
  Re International Limited has none. The case `wilton_two_entities` checks the agent tells them apart.
- **FCRs state ratios differently.** Argo Re prints its ECR ratio (142%); Wilton Re doesn't. The cases
  check the agent quotes a stated ratio and says when it worked one out.
