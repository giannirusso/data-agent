# Architecture diagrams

These are the same diagrams as the submission README. Production components are the target deployment. The prototype runs the agent loop, SQLite, JSON logs, and a BM25 Golden Bucket on one machine.

## Production architecture

```mermaid
flowchart LR
    subgraph Clients
        CLI["CLI (prototype)<br/>typer + rich"]
        WEB["Future: Slack / Web UI"]
    end

    subgraph Edge["Identity & Edge"]
        IAP["Identity-Aware Proxy / OIDC SSO"]
    end

    subgraph Compute["Cloud Run (stateless, autoscaled)"]
        AGENT["Agent Service<br/>FastAPI + LangGraph"]
        INGEST["Golden Bucket Ingestion<br/>Cloud Run Job"]
        MINER["Feedback Miner<br/>Cloud Run Job (nightly)"]
    end

    subgraph AI["Vertex AI"]
        GEMPRO["Gemini Pro-tier<br/>SQL + analysis"]
        GEMFLASH["Gemini Flash-tier<br/>guard, routing, fallback"]
        EMB["Text Embeddings API"]
    end

    subgraph Data["Data Layer"]
        BQRAW[("BigQuery<br/>thelook_ecommerce (raw, PII)")]
        BQSAFE[("BigQuery analytics_safe<br/>authorized views + RLS + policy tags")]
        PG[("Cloud SQL PostgreSQL<br/>reports, audit_log, user_prefs,<br/>entitlements, LangGraph checkpoints,<br/>pgvector golden index")]
        GCS[("GCS Golden Bucket<br/>Question to SQL to Report trios")]
        REDIS[("Memorystore Redis<br/>query/result cache")]
    end

    subgraph Ops["Config, Secrets and Observability"]
        SM["Secret Manager"]
        LF["LangFuse<br/>traces, prompt mgmt, scores"]
        MON["Cloud Logging + Monitoring<br/>dashboards and alerts"]
    end

    CLI --> IAP
    WEB --> IAP
    IAP -->|"user identity"| AGENT
    AGENT -->|"guard / route / fallback"| GEMFLASH
    AGENT -->|"SQL gen / analysis"| GEMPRO
    AGENT -->|"query embedding"| EMB
    AGENT -->|"dry-run + SELECT (max bytes billed)"| BQSAFE
    BQSAFE -. "authorized view over" .-> BQRAW
    AGENT <-->|"reports, prefs, checkpoints, hybrid search"| PG
    AGENT <--> REDIS
    AGENT -->|"spans, scores"| LF
    AGENT -->|"persona prompt (cached, TTL)"| LF
    AGENT -->|"structured logs, metrics"| MON
    AGENT --> SM
    GCS -->|"object finalize (Eventarc)"| INGEST
    INGEST -->|"validate SQL (dry-run)"| BQSAFE
    INGEST --> EMB
    INGEST -->|"upsert trio + vectors"| PG
    MINER -->|"read traces + feedback"| LF
    MINER -->|"candidate trios for analyst review"| PG
    LF --> MON
```

## Agent flow

```mermaid
flowchart TD
    START([User message]) --> GUARD["guard<br/>(rules + optional Flash classifier)"]
    GUARD -->|"malicious / off-topic / PII request"| REFUSE["refuse politely<br/>+ log guard event"]
    GUARD -->|ok| ROUTE{"route_intent"}

    ROUTE -->|"schema question"| SCHEMA["describe_schema<br/>(safe catalog, no SQL)"]
    ROUTE -->|"analysis / report"| RETR["retrieve_golden_trios<br/>(BM25, top-k)"]
    ROUTE -->|"follow-up on last result"| ANALYZE
    ROUTE -->|"list / restore reports"| REPORTS["report_ops (non-destructive)"]
    ROUTE -->|"delete reports"| FIND["find_delete_candidates<br/>(own reports only)"]

    RETR --> PLAN["plan<br/>(1..N sub-questions)"]
    PLAN --> GEN["generate_sql<br/>(Gemini Pro)"]
    GEN --> VAL{"validate_sql<br/>sqlglot: SELECT-only,<br/>whitelist, scope injection, LIMIT"}
    VAL -->|"invalid"| RETRY{"attempts < 3?"}
    VAL -->|"valid"| DRY{"dry_run<br/>bytes <= budget?"}
    DRY -->|"error / over budget"| RETRY
    DRY -->|ok| EXEC["execute<br/>(maximum_bytes_billed, timeout, cache)"]
    EXEC -->|"runtime error"| RETRY
    EXEC --> CHECK{"check_result<br/>empty / suspicious?"}
    CHECK -->|"suspicious"| RETRY
    RETRY -->|"yes: error + SQL + schema fed back"| GEN
    RETRY -->|"no"| GIVEUP["graceful give-up<br/>(clarifying question)"]
    CHECK -->|"ok or legitimately empty"| MORE{"more sub-questions?"}
    MORE -->|yes| GEN
    MORE -->|no| ANALYZE["analyze / write report<br/>(persona + user prefs)"]

    ANALYZE --> SAVE{"save report?"}
    SAVE -->|yes| STORE["save_report (SQLite)"]
    SAVE -->|no| MASK
    STORE --> MASK["pii_mask_output<br/>(regex, Presidio if installed)"]
    SCHEMA --> MASK
    REPORTS --> MASK
    GIVEUP --> MASK
    MASK --> RESP([respond + trace_id])
    REFUSE --> RESP

    FIND -->|"0 found"| RESP
    FIND -->|"1 or more"| CONFIRM["confirm_delete<br/>interrupt: show exact list"]
    CONFIRM -->|"yes / selected numbers"| DEL["delete_reports(exact IDs)<br/>soft delete + audit_log"]
    CONFIRM -->|"anything else / timeout"| CANCEL["cancel, nothing deleted"]
    DEL --> RESP
    CANCEL --> RESP
```

## Data flow for one question

Alice (brands Calvin Klein and Allegra K) asks why Texas customers underspend versus California.

1. The CLI sends her user id, the session id, and the text. Entitlements are loaded from `config/entitlements.yaml`. The model never supplies the brand list.
2. The guard checks injection, PII requests, DML, and off-topic text.
3. The router labels the turn as analysis. A "why / compare" question is planned into sub-questions. A plain "monthly revenue" question stays one step, which keeps free-tier calls down.
4. BM25 returns analyst trios. They are few-shot examples of how to decompose the question, not instructions.
5. Gemini proposes one `SELECT`. `sqlglot` rejects anything that is not a single read-only query, rejects PII columns and `SELECT *` from users, renames base tables to `safe_*` CTEs, and appends `LIMIT`.
6. Those CTEs drop direct identifiers, replace user ids with `FARM_FINGERPRINT(CONCAT(@pii_salt, ...))`, and filter `brand IN UNNEST(@entitled_brands)`. Brand strings and the salt travel as BigQuery query parameters.
7. A dry run must come back under `BQ_MAX_BYTES` (default 1 GB). Only then does `BigQueryRunner.execute` run with `maximum_bytes_billed`.
8. Empty or suspicious frames (negative revenue, for example) are sent back to the model with the error, up to 3 attempts. The same SQL twice stops the loop.
9. The narrative is written from the result frames. A report intent is saved to SQLite after masking.
10. The masker runs again on the final text. The CLI prints the answer, a definitions line, and the `trace_id`.

## Delete flow

"Delete all reports mentioning Client X" loads rows with `owner_user_id = alice` and a keyword match. Bob's reports are never selected. The graph calls `interrupt()` with that exact id list, a hash, and a 10 minute expiry. "yes" or "1,3" deletes only those stored ids, in one transaction with an audit row. Any other reply, a tampered hash, or an expired confirmation cancels. `/restore` undoes the last delete for 30 days.
