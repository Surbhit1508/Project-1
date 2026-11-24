# Model card: customer churn

## Purpose
Rank currently active customers by their probability of placing **no order in the next
90 days**, so a retention team can spend a limited budget (vouchers,
outreach) where it matters. Output: `gold.churn_scores` (one row per active customer, with
risk band and expected revenue at risk).

## Data
* Population: customers with at least one non-cancelled order in the 180 days before the cutoff.
* Features: 22 point-in-time features: recency, frequency,
  monetary, trend, cadence ("how overdue is this customer relative to their own rhythm"),
  returns/cancellations/discount usage, category breadth, device mix, and the SCD2 customer
  attributes valid at the cutoff. Order status is taken *as known at the cutoff* (a return
  processed later does not leak back).
* Training set: monthly snapshots 2024-05-01 to 2025-01-01
  (12,265 rows). Every training label window closes before the test cutoff.
* Test set: out-of-time snapshot at 2025-04-01 (2,424 customers).

## Results (out-of-time test)

| model | ROC-AUC | PR-AUC | Brier | precision@10% | lift@10% |
|---|---|---|---|---|---|
| baseline recency | 0.817 | 0.836 | - | 93.4% | 1.74x |
| logistic regression | 0.837 | 0.869 | 0.164 | 99.2% | 1.85x |
| gradient boosting | 0.832 | 0.857 | 0.168 | 97.9% | 1.83x |

Selected model: **logistic regression** (highest PR-AUC).

### Permutation importance (drop in PR-AUC when the feature is shuffled)

| feature | importance |
|---|---|
| recency_days | +0.2237 |
| return_rate | +0.0236 |
| acquisition_channel | +0.0148 |
| orders_last_30d | +0.0074 |
| orders_last_90d | +0.0038 |
| tenure_days | +0.0032 |
| order_trend_90d | +0.0030 |
| days_since_signup | +0.0024 |
| overdue_ratio | +0.0024 |
| n_orders | +0.0020 |

## Interpretation and caveats
* **Recency is a strong baseline.** In non-contractual retail "days since last order" alone is
  very predictive. The model's value is in the *ranking among recently active customers*:
  cadence, trend and bad experiences (returns) separate a quiet regular from a fading one.
* **Seasonality / drift.** The churn base rate differs by cutoff month (holiday months pull
  customers back). Retrain monthly and monitor the base rate and PR-AUC per scoring run.
* **Correlated rows.** A customer appears in several monthly snapshots. That is fine for
  training but means the effective sample size is smaller than the row count.
* **Not causal.** A high score says who is likely to leave, not who will respond to an
  intervention; an uplift model (with a randomised holdout) is the next step.
* Data is synthetic; numbers illustrate the method, not a real business.
