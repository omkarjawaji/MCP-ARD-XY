# Global routing policy

Apply these rules before reading any single tool's own routing instructions.

1. **Numbers in tables vs. narrative in documents.** Counts, sums, trends and rankings come from a `genie_*` space. Policies, processes and how-we-work answers come from `docs_agentic_search`.
2. **Pick the narrowest domain that owns the metric.** "Revenue" is ambiguous: invoiced or collected money belongs to `genie_finance_ledger`; units, orders and order value belong to `genie_sales_orders`.
3. **"Customers" is shared.** What customers bought -> `genie_sales_orders`. How customers are doing (tickets, health, CSAT, churn risk) -> `genie_customer_support`. What customers owe or paid -> `genie_finance_ledger`.
4. **Stored vs. live.** Data the company recorded (including FX rates on invoices) lives in a space. Current public market data (live FX) is `fx_latest` / `fx_history`. A question that compares stored data with a live value (for example "the rate on our invoices vs today") starts with the tool that owns the stored data; the live lookup is a follow-up call.
5. **Web and time.** A specific URL -> `fetch_*`. Clock or timezone questions -> `time_*`.
6. **If two tools still look right**, choose the one whose "DO NOT USE" list does not mention the question's topic, and prefer the tool that owns the stored/company data. Answer NONE only when no candidate could plausibly answer the question at all; an ambiguous question still gets your best single choice.
