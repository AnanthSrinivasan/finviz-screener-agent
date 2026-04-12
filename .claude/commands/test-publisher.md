# Test Publisher

Fire 3 EventBridge test events to the finviz-events bus using finviz-screener-bot credentials from .env, then tail CloudWatch logs for the XPublisher Lambda to verify they were processed.

Event types:
- MarketDailySummary — no-op in Lambda (future Slack/Discord), expect "skipped" in logs
- ScreenerCompleted — SetupOfDay tweet with Finviz chart, expect 201
- PersistencePick — PersistencePick tweet with market state line + Finviz chart, expect 201

Steps:
1. Load AWS credentials from .env (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)
2. Fire MarketDailySummary event
3. Fire ScreenerCompleted event
4. Fire PersistencePick event
5. Wait 8 seconds for Lambda to process
6. Fetch the last 30 CloudWatch log events from the XPublisher log group and display them

Use these exact test payloads:

MarketDailySummary:
```json
{"date":"2026-04-12","market_state":"GREEN","fear_greed":58,"spy_above_200ma":true}
```

ScreenerCompleted:
```json
{"date":"2026-04-12","market_state":"GREEN","fear_greed":58,"top_pick":{"ticker":"AAOI","quality_score":87,"section":"stage2","rel_vol":2.3,"vcp":true,"entry_price":34.52,"stop_price":31.20},"total_tickers":115,"preview_report_url":"https://ananthsrinivasan.github.io/finviz-screener-agent/preview/2026-04-12.html","full_report_url":"https://ananthsrinivasan.github.io/finviz-screener-agent/reports/2026-04-12.html"}
```

PersistencePick:
```json
{"date":"2026-04-12","ticker":"LUNR","persistence_days":4,"quality_score":82,"section":"stage2"}
```

The CloudWatch log group is `/aws/lambda/PublisherStack-XPublisher*` in eu-central-1.
Show the raw log messages so we can see whether tweet text rendered correctly or if the X API call failed (expected until credentials are verified).
