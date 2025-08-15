# Upbit Unlisted Token Deposit Monitor

This tool watches the Arkham Intel Upbit entity page and flags tokens that appear there but are not currently listed on Upbit (based on the Upbit public markets API).

## Setup

1. Install Python dependencies:

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

2. (Optional) Configure Telegram notifications by setting environment variables:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

If unset, the script will just print to stdout.

## Run

```bash
python monitor_upbit_unlisted.py --headed --interval=10
```

- `--headed`: run with a visible browser (recommended to avoid anti-bot blocking). Omit for headless.
- `--interval`: seconds between DOM scans. Network responses are processed live as they arrive.

## Notes

- The script uses heuristics to extract tokens from both network responses and the visible DOM on `intel.arkm.com`. The site may change over time; adjust selectors if needed.
- Upbit-listed tokens are fetched from `https://api.upbit.com/v1/market/all` and compared by symbol (the part after the hyphen in markets like `KRW-BTC`).
- If Arkham blocks headless automation, use `--headed`.
