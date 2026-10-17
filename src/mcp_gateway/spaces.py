"""Genie-like "spaces": each is a SQLite database plus routing metadata.

The domains overlap on purpose (customers appear in sales and support; "revenue"
appears in sales and finance) so that choosing the right space is a real decision
for a decider that reads `description` + `routing_instructions`.
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path


@dataclass(frozen=True)
class Space:
    key: str
    title: str
    description: str
    routing_instructions: str
    sample_questions: list[str]
    tags: list[str]
    ddl: str
    seeder: str  # name of a function in this module
    notes: str = ""
    extra: dict = field(default_factory=dict)


SALES_DDL = """
CREATE TABLE customers(customer_id INTEGER PRIMARY KEY, name TEXT, segment TEXT, country TEXT, signup_date TEXT);
CREATE TABLE products(product_id INTEGER PRIMARY KEY, name TEXT, category TEXT, list_price REAL);
CREATE TABLE orders(order_id INTEGER PRIMARY KEY, customer_id INTEGER, order_date TEXT, status TEXT, channel TEXT);
CREATE TABLE order_items(order_id INTEGER, product_id INTEGER, quantity INTEGER, unit_price REAL, discount_pct REAL);
"""

SUPPORT_DDL = """
CREATE TABLE customers(customer_id INTEGER PRIMARY KEY, name TEXT, plan TEXT, region TEXT, health_score INTEGER);
CREATE TABLE tickets(ticket_id INTEGER PRIMARY KEY, customer_id INTEGER, opened_at TEXT, closed_at TEXT, severity TEXT, category TEXT, status TEXT);
CREATE TABLE csat(ticket_id INTEGER, score INTEGER, comment TEXT);
"""

HR_DDL = """
CREATE TABLE departments(dept_id INTEGER PRIMARY KEY, name TEXT, cost_center TEXT);
CREATE TABLE employees(emp_id INTEGER PRIMARY KEY, name TEXT, dept_id INTEGER, title TEXT, hire_date TEXT, salary REAL, country TEXT, status TEXT);
CREATE TABLE attrition(emp_id INTEGER, left_on TEXT, reason TEXT);
"""

FINANCE_DDL = """
CREATE TABLE invoices(invoice_id INTEGER PRIMARY KEY, customer_name TEXT, issued_on TEXT, due_on TEXT, amount_usd REAL, status TEXT, cost_center TEXT);
CREATE TABLE payments(payment_id INTEGER PRIMARY KEY, invoice_id INTEGER, paid_on TEXT, amount_usd REAL, method TEXT);
CREATE TABLE budgets(cost_center TEXT, fiscal_quarter TEXT, budget_usd REAL, actual_usd REAL);
CREATE TABLE fx_rates(rate_date TEXT, base TEXT, quote TEXT, rate REAL);
"""

SPACES: list[Space] = [
    Space(
        key="sales_orders",
        title="Sales & Orders",
        description=(
            "Order-level sales data: customers, products, orders and line items. "
            "Answers questions about units sold, order counts, product/category "
            "performance, channels, discounts and gross order value."
        ),
        routing_instructions=(
            "USE for: units, orders, products, categories, channels, discounts, "
            "best-sellers, customer segments by purchasing. "
            "DO NOT USE for: invoiced/collected cash, budgets, DSO, FX (use finance_ledger); "
            "tickets, CSAT, customer health (use customer_support); headcount (use hr_people)."
        ),
        sample_questions=[
            "Top 5 product categories by units sold last quarter",
            "How many orders came through the web channel in 2025?",
            "Average discount by customer segment",
        ],
        tags=["sales", "orders", "products", "customers"],
        ddl=SALES_DDL,
        seeder="seed_sales",
    ),
    Space(
        key="customer_support",
        title="Customer Support",
        description=(
            "Support operations: support tickets, severities, resolution times and "
            "CSAT scores, plus each customer's plan, region and health score."
        ),
        routing_instructions=(
            "USE for: tickets, backlog, severity, time-to-resolve, CSAT, churn-risk or "
            "health score, plan tier. "
            "DO NOT USE for: what customers bought or order value (use sales_orders); "
            "billing/payments (use finance_ledger)."
        ),
        sample_questions=[
            "Which customers have the lowest health score and open sev1 tickets?",
            "Average time to resolve by ticket category",
            "CSAT trend by plan tier",
        ],
        tags=["support", "tickets", "csat", "customers"],
        ddl=SUPPORT_DDL,
        seeder="seed_support",
    ),
    Space(
        key="hr_people",
        title="HR & People",
        description=(
            "Workforce data: employees, departments, titles, salaries, hire dates "
            "and attrition with leaving reasons."
        ),
        routing_instructions=(
            "USE for: headcount, salary, hiring, attrition, departments, titles. "
            "DO NOT USE for: budget vs actual spend by cost center (use finance_ledger)."
        ),
        sample_questions=[
            "Headcount and average salary by department",
            "Top attrition reasons in the last year",
            "How many people were hired in 2025?",
        ],
        tags=["hr", "employees", "attrition", "salary"],
        ddl=HR_DDL,
        seeder="seed_hr",
    ),
    Space(
        key="finance_ledger",
        title="Finance Ledger",
        description=(
            "Accounting view: invoices, payments received, budgets vs actuals by "
            "cost center and daily FX rates. 'Revenue' here means invoiced and "
            "collected USD, not order value."
        ),
        routing_instructions=(
            "USE for: invoiced revenue, cash collected, overdue invoices, DSO, budget "
            "variance, FX conversion. "
            "DO NOT USE for: unit/product/channel questions (use sales_orders); "
            "tickets or CSAT (use customer_support); salaries (use hr_people)."
        ),
        sample_questions=[
            "Total overdue invoice amount by cost center",
            "Budget variance by fiscal quarter",
            "Cash collected per month this year",
        ],
        tags=["finance", "invoices", "budget", "fx", "revenue"],
        ddl=FINANCE_DDL,
        seeder="seed_finance",
    ),
]

SPACES_BY_KEY = {s.key: s for s in SPACES}

_COUNTRIES = ["US", "DE", "IN", "BR", "JP", "GB", "FR"]
_NAMES = [
    "Acme", "Globex", "Initech", "Umbrella", "Hooli", "Stark", "Wayne", "Wonka",
    "Soylent", "Cyberdyne", "Tyrell", "Massive Dynamic", "Vandelay", "Pied Piper",
    "Dunder", "Oscorp", "Aperture", "Black Mesa", "Gringotts", "Nakatomi",
]


def _d(base: date, days: int) -> str:
    return (base + timedelta(days=days)).isoformat()


def seed_sales(c: sqlite3.Connection, rng: random.Random) -> None:
    start = date(2024, 1, 1)
    segs = ["enterprise", "mid-market", "smb"]
    c.executemany(
        "INSERT INTO customers VALUES(?,?,?,?,?)",
        [(i + 1, f"{n} Corp", rng.choice(segs), rng.choice(_COUNTRIES), _d(start, rng.randint(0, 300)))
         for i, n in enumerate(_NAMES)],
    )
    cats = {"Hardware": ["Sensor", "Gateway", "Router"], "Software": ["Analytics", "Backup", "Monitor"],
            "Services": ["Onboarding", "Premium Support", "Training"]}
    rows = []
    pid = 1
    for cat, names in cats.items():
        for n in names:
            rows.append((pid, n, cat, round(rng.uniform(50, 2000), 2)))
            pid += 1
    c.executemany("INSERT INTO products VALUES(?,?,?,?)", rows)
    oid = 1
    for _ in range(600):
        cust = rng.randint(1, len(_NAMES))
        odate = _d(start, rng.randint(0, 640))
        c.execute("INSERT INTO orders VALUES(?,?,?,?,?)",
                  (oid, cust, odate, rng.choices(["shipped", "pending", "cancelled"], [8, 1, 1])[0],
                   rng.choice(["web", "partner", "field-sales"])))
        for _ in range(rng.randint(1, 4)):
            p = rng.choice(rows)
            c.execute("INSERT INTO order_items VALUES(?,?,?,?,?)",
                      (oid, p[0], rng.randint(1, 20), p[3], rng.choice([0, 0, 5, 10, 15])))
        oid += 1


def seed_support(c: sqlite3.Connection, rng: random.Random) -> None:
    start = date(2024, 6, 1)
    c.executemany(
        "INSERT INTO customers VALUES(?,?,?,?,?)",
        [(i + 1, f"{n} Corp", rng.choice(["free", "pro", "enterprise"]),
          rng.choice(["NA", "EMEA", "APAC", "LATAM"]), rng.randint(20, 100))
         for i, n in enumerate(_NAMES)],
    )
    for tid in range(1, 401):
        opened = rng.randint(0, 480)
        closed = None if rng.random() < 0.15 else _d(start, opened + rng.randint(0, 14))
        c.execute("INSERT INTO tickets VALUES(?,?,?,?,?,?,?)",
                  (tid, rng.randint(1, len(_NAMES)), _d(start, opened), closed,
                   rng.choices(["sev1", "sev2", "sev3"], [1, 3, 6])[0],
                   rng.choice(["bug", "billing-question", "how-to", "outage", "feature-request"]),
                   "open" if closed is None else "closed"))
        if closed and rng.random() < 0.7:
            c.execute("INSERT INTO csat VALUES(?,?,?)",
                      (tid, rng.randint(1, 5), rng.choice(["fast", "slow", "helpful", "unclear", ""])))


def seed_hr(c: sqlite3.Connection, rng: random.Random) -> None:
    depts = ["Engineering", "Sales", "Support", "Finance", "People"]
    c.executemany("INSERT INTO departments VALUES(?,?,?)",
                  [(i + 1, d, f"CC-{100 + i}") for i, d in enumerate(depts)])
    start = date(2020, 1, 1)
    titles = ["Analyst", "Engineer", "Manager", "Director", "Associate"]
    for eid in range(1, 151):
        status = rng.choices(["active", "left"], [8, 2])[0]
        c.execute("INSERT INTO employees VALUES(?,?,?,?,?,?,?,?)",
                  (eid, f"Employee {eid}", rng.randint(1, len(depts)), rng.choice(titles),
                   _d(start, rng.randint(0, 2000)), round(rng.uniform(45000, 190000), 2),
                   rng.choice(_COUNTRIES), status))
        if status == "left":
            c.execute("INSERT INTO attrition VALUES(?,?,?)",
                      (eid, _d(date(2025, 1, 1), rng.randint(0, 600)),
                       rng.choice(["compensation", "career growth", "relocation", "manager", "other"])))


def seed_finance(c: sqlite3.Connection, rng: random.Random) -> None:
    start = date(2025, 1, 1)
    ccs = ["CC-100", "CC-101", "CC-102", "CC-103"]
    pid = 1
    for iid in range(1, 301):
        issued = rng.randint(0, 600)
        amount = round(rng.uniform(500, 40000), 2)
        paid = rng.random() < 0.7
        due = issued + 30
        status = "paid" if paid else ("overdue" if due < 560 else "open")
        c.execute("INSERT INTO invoices VALUES(?,?,?,?,?,?,?)",
                  (iid, f"{rng.choice(_NAMES)} Corp", _d(start, issued), _d(start, due), amount, status,
                   rng.choice(ccs)))
        if paid:
            c.execute("INSERT INTO payments VALUES(?,?,?,?,?)",
                      (pid, iid, _d(start, issued + rng.randint(5, 50)), amount,
                       rng.choice(["wire", "card", "ach"])))
            pid += 1
    for q in ["2025-Q1", "2025-Q2", "2025-Q3", "2025-Q4", "2026-Q1"]:
        for cc in ccs:
            b = round(rng.uniform(100000, 400000), 2)
            c.execute("INSERT INTO budgets VALUES(?,?,?,?)", (cc, q, b, round(b * rng.uniform(0.8, 1.2), 2)))
    base = {"EUR": 0.92, "GBP": 0.79, "INR": 83.0, "JPY": 150.0}
    for off in range(0, 600, 7):
        for q, r in base.items():
            c.execute("INSERT INTO fx_rates VALUES(?,?,?,?)",
                      (_d(start, off), "USD", q, round(r * rng.uniform(0.97, 1.03), 4)))


def ensure_space_db(space: Space, data_dir: Path) -> Path:
    """Create and seed the SQLite file for a space if it does not exist yet."""
    path = data_dir / "spaces" / f"{space.key}.db"
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(space.ddl)
        globals()[space.seeder](conn, random.Random(space.key))
        conn.commit()
    except Exception:
        conn.close()
        path.unlink(missing_ok=True)
        raise
    conn.close()
    return path
