# alerts/ (owner: A) - stub

Rule engine, notifiers and the cost-based threshold helper (CLAUDE.md section 9).
Not built in the foundation (Phases 0 to 3). Will hold: `engine.py` (hysteresis, minimum
consecutive evaluations, one open alert per rule and node, auto-resolve), `notifiers/` (alerts
table, log file, Telegram), `threshold.py` (`p* = C_FP / (C_FP + C_FN)`). Rules live in
`config/alerts.yaml`.
