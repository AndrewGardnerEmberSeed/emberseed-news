# EmberSeed AI News

The nightly AI news round-up for emberseedautomation.com.au.

Every night from 1am Adelaide time, a GitHub Action runs `build.py`, which:

1. reads the feeds in `feeds.json`
2. keeps AI stories from the last day, dropping robotics, very technical items and articles already used in the last three editions
3. has Claude pick the 4–8 most interesting stories and rewrite them following `style-guide.md`
4. drops any story whose numbers can't be found in its source
5. publishes the edition, one page per story, an archive and `feed.xml` (RSS) to GitHub Pages

Each edition is labelled "Summarised by AI, read by Andrew". Andrew reads each edition in the morning and corrects anything that's wrong.

GitHub often starts scheduled jobs late (on 15 October, by four hours) or skips them, so the job tries every hour and accepts any run from 1am to 10am. There's only ever one edition a day, and the hourly runs that find nothing to do leave the site alone.

## Status (16 October 2026)

- Live at https://andrewgardneremberseed.github.io/emberseed-news/. The News links on emberseedautomation.com.au go to `/news`, which a Squarespace URL mapping redirects here.
- The old Squarespace News page is switched off, at `/news-first-edition`.
- The job made its first edition on 16 October 2026. It was run by hand, because GitHub started that night's scheduled runs too late.
- InnovationAus refuses GitHub's servers (403 Forbidden), so its stories don't come through. The other 39 feeds work.

## Setup

Done:

- The public repository, with **Settings → Pages → Source** set to **GitHub Actions**.
- The variable `NEWS_SITE_URL`, set to `https://andrewgardneremberseed.github.io/emberseed-news`.
- The secret `ANTHROPIC_API_KEY` (Andrew only). The key must:
  - belong to one workspace. A key for all workspaces is refused unless every request names a workspace.
  - have no expiry, or the news stops when it lapses.

  A separate workspace for the news lets it have its own spend limit (the Default workspace can't have one).

Optional, custom address:

- In Squarespace → Domains → DNS, add a CNAME record: host `news`, value `andrewgardneremberseed.github.io`.
- Put `news.emberseedautomation.com.au` in **Settings → Pages → Custom domain** and tick **Enforce HTTPS**.
- Change the `NEWS_SITE_URL` variable to `https://news.emberseedautomation.com.au`.
- Point the `/news` URL mapping in Squarespace (Settings → Developer Tools → URL Mappings) at the new address.

## Icons

`static/` holds the site's icons: the EmberSeed flame and seed in amber on navy. They're used for browser tabs, the iPhone home screen (`apple-touch-icon.png`, labelled "AI News") and Android (`manifest.webmanifest`).

- `favicon.svg` is the master.
- The PNGs were rendered from it at 180, 192, 512 and 32 pixels with headless Chrome.
- Every build copies `static/` into the site.
- iPhones keep the icon they saved when the page was added, so to see a new icon, remove the home-screen shortcut and add it again from Safari.

## Running it by hand

Go to **Actions → Nightly AI news → Run workflow** and choose a mode:

- **build** makes a new edition now. It replaces that day's edition if there already is one.
- **render** rebuilds the pages and feed from `data/`, after fixing a story.
- **test** does a full run with Claude and prints the stories in the log, but publishes nothing.

## Fixing a story

1. Edit `data/YYYY-MM-DD.json`. On GitHub, open the file, click the pencil, make the change and commit.
2. Run the workflow in **render** mode.

## Testing locally

- `python3 build.py --no-ai --force` checks the feeds and pages without calling Claude.
- `ANTHROPIC_API_KEY=... python3 build.py --dry-run` does a full run that prints the stories and saves nothing.

## Models and costs

Since 16 October 2026, Claude Sonnet 5.5 picks the stories and Claude Haiku 5.5 writes them. Andrew chose this to bring the cost down from about US$0.47 a night on Opus 5.5.

| Setup | Cost a night | A month |
|---|---|---|
| Sonnet 5.5 picks and checks, Haiku 5.5 writes (current, from 17 October) | about US$0.09–0.11 | about US$3 |
| Sonnet 5.5 picks, Haiku 5.5 writes (16–17 October) | about US$0.04 | about US$1.20 |
| Opus 5.5 for both (until 16 October) | about US$0.47 | about US$14 |

- Most of the cost is Sonnet: reading the list of 70–120 candidates, then checking the finished stories against their
  sources. The eight Haiku write-ups together cost about a cent.
- The workspace's spend limit is US$5 a month. Test runs count too (about US$0.10 each).
- GitHub Actions and Pages are free for a public repository.
- Each run's log ends with its tokens and approximate cost per model.

### Changing the models

- **To change them for good,** set the repository variables `NEWS_PICK_MODEL` and `NEWS_WRITE_MODEL`, for example both to `claude-opus-5-5` for the best quality.
- **To try a model on one test run,** fill in the `pick_model` and `write_model` boxes when running the workflow in **test** mode. The nightly setup isn't affected.
- **To change how hard the models think,** set `NEWS_EFFORT` to `low`, `medium` (the default) or `high`. Higher costs more.

## How the Claude calls work

- Current Claude models always think before they answer, and the thinking counts toward `max_tokens`. That's why the limit is 16,000: the first test, at 1,500, ran out before writing anything.
- The replies use structured outputs, so they always match the JSON shape `build.py` expects.
- Sonnet and Opus calls send `fallbacks: "default"`, which reruns a request on a suitable model if a safety check declines it. Haiku has no such fallback.
- The style guide is cached across the write-up calls, so each read after the first costs a tenth as much.
- Code, not Claude, fills in the source name and "also reported by", from the feeds the story came from.
- A headline over ten words gets one quick extra call to shorten it.

## The checks (from 17 October 2026)

The first night on the cheaper models (17 October) had problems:
- five of eight stories were in one category;
- a story was written from a feed's 600-character excerpt, so it had no period or market for its main figure;
- a "why it matters" told Australian businesses they'd need a Chinese export licence.

Before anything is published now:
- **Tip-offs.** YouTube, podcasts and newsletters (the "Commentators & newsletters" feeds) are tip-offs. A story needs a
  news outlet or the company itself.
- **Spread.** At most three stories in any one category. The picker ranks three reserves for when stories fail.
- **Something to read.** A story is skipped when its article couldn't be read and only the feed's excerpt is left. The
  log names it.
- **Numbers.** Every number in a story must appear in its sources.
- **Sub-editor.** Sonnet reads every story against the sources the writer had: the main article to 12,000 characters,
  the others shorter. It flags only real errors: claims the sources don't make, lost conditions, and advice that
  doesn't follow or is aimed at the wrong reader. It passes, corrects or drops each story, and the log lists what it
  found. Corrections must pass the numbers check too. If the check itself fails, the stories go out as written and
  the log says so.
- Picking allows at most two stories about any one company.
- Rate limits and busy errors are retried automatically.
