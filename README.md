# EmberSeed AI News

The nightly AI news round-up for emberseedautomation.com.au.

Every night at 1am Adelaide time, a GitHub Action runs `build.py`, which:

1. reads the feeds in `feeds.json`
2. keeps AI stories from the last day, dropping robotics and very technical items
3. has Claude pick the 4–8 most interesting stories and rewrite them following `style-guide.md`
4. drops any story whose numbers can't be found in its source
5. publishes the edition, one page per story, an archive and `feed.xml` (RSS) to GitHub Pages

Each edition is labelled "Summarised by AI, read by Andrew". Andrew reads each edition in the morning and corrects anything that's wrong.

GitHub sometimes starts scheduled jobs late or skips them, so backup runs follow an hour or two after 1am. There's only ever one edition a day.

## Status (15 October 2026)

- Live at https://andrewgardneremberseed.github.io/emberseed-news/. The News links on emberseedautomation.com.au go to `/news`, which a Squarespace URL mapping redirects here.
- The old Squarespace News page is switched off, at `/news-first-edition`.
- Waiting on the `ANTHROPIC_API_KEY` secret. Until it's added, the nightly runs skip and the 15 October edition stays up.

## One-time setup

Done on 15 October 2026:

- The public repository, with **Settings → Pages → Source** set to **GitHub Actions**.
- The variable `NEWS_SITE_URL`, set to `https://andrewgardneremberseed.github.io/emberseed-news`.

Still to do:

1. **The API key (Andrew only).**
   - Go to **Settings → Secrets and variables → Actions → New repository secret**.
   - Name it `ANTHROPIC_API_KEY` and paste a key from console.anthropic.com.
2. **For an edition straight away**, go to **Actions → Nightly AI news → Run workflow** and choose **build**. Otherwise the first new edition arrives at 1am. A manual build replaces that day's edition if there already is one.
3. **Custom address (optional):**
   - In Squarespace → Domains → DNS, add a CNAME record: host `news`, value `andrewgardneremberseed.github.io`.
   - Put `news.emberseedautomation.com.au` in **Settings → Pages → Custom domain** and tick **Enforce HTTPS**.
   - Change the `NEWS_SITE_URL` variable to `https://news.emberseedautomation.com.au`.
   - Point the `/news` URL mapping in Squarespace (Settings → Developer Tools → URL Mappings) at the new address.

## Fixing a story

1. Edit `data/YYYY-MM-DD.json`. On GitHub, open the file, click the pencil, make the change and commit.
2. Go to **Actions → Nightly AI news → Run workflow** and choose **render**.

The pages and the feed are rebuilt from the data files.

## Testing locally

- `python3 build.py --no-ai --force` checks the feeds and pages without calling Claude.
- `ANTHROPIC_API_KEY=... python3 build.py --force` does a full run.

## Costs

GitHub Actions and Pages are free for a public repository. Claude costs a few cents a night. To make it cheaper, set the repository variable `NEWS_MODEL` to `claude-sonnet-5-5`.
