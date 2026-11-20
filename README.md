# Houston Cash Games

A free, self-updating website showing the cash games running at every Houston-area poker room on PokerAtlas, with a running history (busiest times, Champions Club share, 7-day ranking, downloadable spreadsheets).

**To set it up, follow [SETUP.md](SETUP.md).**

## What's in here

- `scraper/scrape.py` signs in to PokerAtlas, reads every room's "Games in Progress," and saves the reading.
- `.github/workflows/update.yml` runs the reader every 15 minutes on GitHub Actions and publishes the site.
- `site/index.html` is the website.
- `data/` holds the readings (filled in automatically).
- `config.json` holds the settings (highlighted room, areas, rooms to skip or add).
- `tests/test_parsers.py` has quick checks for the reader (`python tests/test_parsers.py`).
