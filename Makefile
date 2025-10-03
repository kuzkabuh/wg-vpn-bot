run:
\t. .venv/bin/activate && uvicorn app.webhook:app --host 127.0.0.1 --port 8081

logs:
\tsudo journalctl -u wg-vpn-bot -f

restart:
\tsudo systemctl daemon-reload && sudo systemctl restart wg-vpn-bot

health:
\tcurl -sS http://127.0.0.1:8081/health
