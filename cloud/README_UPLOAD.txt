BikeHunter NL Wide Search V1
============================

WHAT THIS ADDS
- Marktplaats NL (existing, price fix retained)
- AutoScout24 NL (NEW: private + dealer listings, exact-ad verification)
- Motoroccasion NL (NEW: dealer inventory)
- viaBOVAG NL (NEW: BOVAG dealer inventory)
- 2dehands BE retained
- AutoScout24 DE retained, including exact-ad price/link accuracy fix

IMPORTANT
- No Supabase schema changes.
- No GitHub secrets changes.
- No Cloudflare/dashboard changes.
- The first successful scan of each NEW source is treated as a baseline, so Telegram does not send every old listing as a new alert.
- Facebook Marketplace is NOT included in this package. It is login/session dependent and is not reliable from the unattended GitHub Actions scanner.

UPLOAD
1. Open GitHub -> MagnusLenard/BikeHunter -> cloud
2. Add file -> Upload files
3. Drag ALL 7 .py files from this folder into GitHub
4. Confirm replacement of the existing files when shown
5. Commit changes
6. Run BikeHunter once manually
7. In the GitHub Action log you should see these NL source names:
   Marktplaats NL
   AutoScout24 NL
   Motoroccasion NL
   viaBOVAG NL

If one website blocks GitHub temporarily, BikeHunter records a warning and keeps the older listings from that source active instead of deleting them.
