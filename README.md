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

## Costs

GitHub Actions and Pages are free for a public repository.

Claude (Opus 5.5 at medium effort) costs about US$0.45 a run, roughly US$14 a month. The first editions used about 55,000 input and 12,000 output tokens a run, at US$4 and US$20 per million. Each run's log ends with its token count.

To make it cheaper, set a repository variable:

- `NEWS_EFFORT` to `low`
- or `NEWS_MODEL` to `claude-sonnet-5-5`, which is half the price per token

Check the stories still read well after either change.

## How the Claude calls work

- Opus 5.5 always thinks before it answers, and its thinking counts toward `max_tokens`. That's why the limit is 16,000: the first test, at 1,500, ran out before writing anything.
- The replies use structured outputs, so they always match the JSON shape `build.py` expects.
- `fallbacks: "default"` reruns a request on a suitable model if a safety check declines it.
- Rate limits and busy errors are retried automatically.
