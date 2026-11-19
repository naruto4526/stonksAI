# stonksAI

News-driven trading research for Indian equities: read NSE/BSE corporate filings as they
are published, let an LLM judge each one, paper-trade the result and measure it.

- `decision-service/`: Python. Filing ingestion and, later, the LLM decision step.
  Today it holds a poller that compares the NSE and BSE announcement sources.
- `trading-service/` (planned): Java/Spring Boot. Risk checks, orders, exits, P&L.

Start with [decision-service/README.md](decision-service/README.md).
