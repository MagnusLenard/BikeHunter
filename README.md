# BikeHunter Cloud Scanner

Runs the BikeHunter marketplace scan every 6 hours using GitHub Actions.

Sources:
- Netherlands: Marktplaats
- Belgium: 2dehands
- Germany: AutoScout24

Cloud state and listings are stored in Supabase. Telegram alerts are sent for new listings and price drops after the first successful baseline scan.

## Required GitHub Actions secrets

- `SUPABASE_URL`
- `SUPABASE_SECRET_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Never commit those values to this repository.
