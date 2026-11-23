### Issues to be fixed eventually

- Event matching logic has gaps
    1. ISIN not found, the company name is stored without ISIN. Download BSE Securities list to help with this.
    2. If company name on BSE does not match with the company name on the NSE, then the ISIN might not be found and events might not match. This is an issue.
    3. Incorrect event types can be flagged.
    4. Events from the same company code within 30 minutes of each other (NSE and BSE filings) are grouped into the same event regardless of event type. So only one LLM Decison call would be made for this. (Currently)