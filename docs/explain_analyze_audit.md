# EXPLAIN ANALYZE: the audit query, before and after its index

Date: 2026-11-05 · PostgreSQL 16.15 (Docker, `tollgate-pg`) · DB: `tollgate`

## The query under test

The hot audit-report query: one agent's decisions in the last 24 hours,
grouped by decision and reason code.

```sql
SELECT decision, reason_code, count(*) AS n, sum(amount) AS total_amount
FROM audit_log
WHERE agent_id = 1
  AND created_at >= now() - interval '24 hours'
GROUP BY decision, reason_code
ORDER BY n DESC;
```

## Data profile

| Metric                                        | Rows    |
|-----------------------------------------------|---------|
| `audit_log` total                             | 200,012 |
| agent_id = 1                                  | 40,010  |
| agent_id = 1 **and** within last 24 h (target)| ~5,720  |

The target slice is ~2.9% of the table: selective enough that an index
should win, but not so tiny that the result is uninteresting. Seed rows
were spread uniformly over 7 days across 5 agents, then `ANALYZE` ran so
the planner had accurate statistics.

---

## BEFORE — no index (migration `2417403fe4aa` only)

```text
 Sort  (cost=5314.98..5315.03 rows=20 width=52) (actual time=112.971..113.113 rows=8 loops=1)
   Sort Key: (count(*)) DESC
   Sort Method: quicksort  Memory: 25kB
   Buffers: shared hit=1935
   ->  Finalize GroupAggregate  (cost=5311.74..5314.54 rows=20 width=52) (actual time=112.638..112.802 rows=8 loops=1)
         Group Key: decision, reason_code
         Buffers: shared hit=1932
         ->  Gather Merge  (cost=5311.74..5314.04 rows=20 width=52) (actual time=112.540..112.687 rows=10 loops=1)
               Workers Planned: 1
               Workers Launched: 1
               Buffers: shared hit=1932
               ->  Sort  (cost=4311.73..4311.78 rows=20 width=52) (actual time=90.809..90.811 rows=5 loops=2)
                     Sort Key: decision, reason_code
                     Sort Method: quicksort  Memory: 26kB
                     Buffers: shared hit=1932
                     Worker 0:  Sort Method: quicksort  Memory: 25kB
                     ->  Partial HashAggregate  (cost=4311.05..4311.30 rows=20 width=52) (actual time=90.641..90.646 rows=5 loops=2)
                           Group Key: decision, reason_code
                           Batches: 1  Memory Usage: 24kB
                           Buffers: shared hit=1924
                           Worker 0:  Batches: 1  Memory Usage: 24kB
                           ->  Parallel Seq Scan on audit_log  (cost=0.00..4277.08 rows=3397 width=18) (actual time=0.017..72.688 rows=2863 loops=2)
                                 Filter: ((agent_id = 1) AND (created_at >= (now() - '24:00:00'::interval)))
                                 Rows Removed by Filter: 97143
                                 Buffers: shared hit=1924
 Planning:
   Buffers: shared hit=129
 Planning Time: 2.707 ms
 Execution Time: 113.758 ms
```

**Reading it:** the planner had no way to find agent 1's recent rows except
reading the *entire table*. Two parallel workers each scanned ~100k rows and
discarded ~97,143 of them (`Rows Removed by Filter`). Every one of the 1,935
shared buffers (8 kB pages — the whole 15 MB table) was touched to produce
5,726 matching rows. Execution: **113.8 ms**, and the cost grows linearly
with total table size forever: as audit_log accumulates history, this query
gets slower every day even though the 24-hour slice stays the same size.

---

## The fix — migration `fa956bf87c5a`

```sql
CREATE INDEX ix_audit_log_agent_created
    ON audit_log (agent_id, created_at DESC)
    INCLUDE (decision, reason_code, amount);
```

Why this shape:

- **`(agent_id, created_at DESC)`** — equality column first, range column
  second. The index is pre-sorted exactly the way the query filters: walk to
  `agent_id = 1`, then scan its entries newest-first and stop at the 24-hour
  boundary. Rows read ≈ rows returned.
- **`INCLUDE (decision, reason_code, amount)`** — the query's other three
  columns ride along in the index *leaves* (not the sort key). That makes the
  scan **index-only**: Postgres never visits the heap (table) at all.
- Heap-fetch avoidance additionally requires the visibility map to say
  "all rows on this page are visible to everyone", which `VACUUM` maintains.
  Without regular vacuuming, an index-only scan degrades into heap fetches
  anyway — the INCLUDE columns alone are not sufficient.

After applying: `VACUUM ANALYZE audit_log;`

---

## AFTER — with the covering index

First run (cold index — pages not yet in shared buffers):

```text
 Sort  (cost=343.83..343.85 rows=9 width=52) (actual time=16.995..16.997 rows=8 loops=1)
   Sort Key: (count(*)) DESC
   Sort Method: quicksort  Memory: 25kB
   Buffers: shared hit=7 read=50
   ->  HashAggregate  (cost=343.57..343.69 rows=9 width=52) (actual time=16.748..16.755 rows=8 loops=1)
         Group Key: decision, reason_code
         Batches: 1  Memory Usage: 24kB
         Buffers: shared hit=4 read=50
         ->  Index Only Scan using ix_audit_log_agent_created on audit_log  (cost=0.42..286.52 rows=5705 width=18) (actual time=1.200..7.733 rows=5722 loops=1)
               Index Cond: ((agent_id = 1) AND (created_at >= (now() - '24:00:00'::interval)))
               Heap Fetches: 0
               Buffers: shared hit=4 read=50
 Planning:
   Buffers: shared hit=177
 Planning Time: 9.543 ms
 Execution Time: 17.368 ms
```

Second run (warm cache):

```text
 Sort  (cost=343.65..343.67 rows=9 width=52) (actual time=6.597..6.600 rows=8 loops=1)
   Sort Key: (count(*)) DESC
   Sort Method: quicksort  Memory: 25kB
   Buffers: shared hit=57
   ->  HashAggregate  (cost=343.39..343.51 rows=9 width=52) (actual time=6.468..6.475 rows=8 loops=1)
         Group Key: decision, reason_code
         Batches: 1  Memory Usage: 24kB
         Buffers: shared hit=54
         ->  Index Only Scan using ix_audit_log_agent_created on audit_log  (cost=0.42..286.40 rows=5699 width=18) (actual time=0.360..2.520 rows=5712 loops=1)
               Index Cond: ((agent_id = 1) AND (created_at >= (now() - '24:00:00'::interval)))
               Heap Fetches: 0
               Buffers: shared hit=54
 Planning:
   Buffers: shared hit=167
 Planning Time: 3.561 ms
 Execution Time: 8.958 ms
```

**Reading it:** `Parallel Seq Scan` became `Index Only Scan`.
`Heap Fetches: 0` confirms the covering index + visibility map are doing
their job — the table itself was never touched. The plan also got *simpler*
(no Gather Merge, no parallel worker needed for 54 page reads).

---

## Results

| Metric                      | Before (seq scan) | After (cold) | After (warm) | Change (warm) |
|-----------------------------|-------------------|--------------|--------------|----------------|
| Execution time              | 113.758 ms        | 17.368 ms    | 8.958 ms     | **12.7× faster** |
| Shared buffers touched      | 1,935             | 57           | 57           | **34× less I/O** |
| Heap rows examined          | 200,012           | 0            | 0            | —              |
| Rows actually returned      | 5,726             | 5,722        | 5,712        | same slice     |
| Parallel workers            | 1                 | 0            | 0            | frees CPU      |
| Scaling with table growth   | O(table size)     | O(matched rows) | O(matched rows) | **linear → flat** |

The last row is the real win. Before, every audit row ever written made this
query slower. After, the cost depends only on the agent's activity inside
the 24-hour window — a year of accumulated history changes nothing.

## Trade-offs (recorded honestly)

- **Write amplification:** every INSERT into audit_log now also writes the
  index. Table = 15 MB; indexes = 16 MB (the covering index is most of that,
  because INCLUDE duplicates three columns into the leaves). Audit writes
  happen once per decision — cheap relative to what they protect.
- **INCLUDE columns are query-specific.** If future audit queries need other
  columns index-only, either they use a plain index scan (still fast, just
  with heap fetches) or the index gets revised.
- **Vacuum discipline matters.** `Heap Fetches: 0` depends on the visibility
  map; a bloated, never-vacuumed table would silently degrade this win.
  Autovacuum handles it at this scale.

## Reproduce

```bash
docker exec tollgate-pg psql -U tollgate -d tollgate   # seeded dev database
# before:  git checkout the tree at migration 2417403fe4aa, or DROP INDEX ix_audit_log_agent_created;
# after:   uv run alembic upgrade head && psql -c "VACUUM ANALYZE audit_log;"
```
