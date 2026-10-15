# EmberSeed AI News

The nightly AI news round-up for emberseedautomation.com.au.

Every night at 1am Adelaide time, a GitHub Action runs `build.py`, which:

1. reads the feeds in `feeds.json`
2. keeps AI stories from the last day, dropping robotics and very technical items
3. has Claude pick the 4–8 most interesting stories and rewrite them following `style-guide.md`
4. drops any story whose numbers can't be found in its source
5. publishes the edition, one page per story, an archive and `feed.xml` (RSS) to GitHub Pages

Each edition is labelled "Summarised by AI, read by Andrew". Andrew reads each edition in the morning and corrects anything that's wrong.

## One-time setup

1. Create the repository from this folder, then go to **Settings → Pages** and set **Source** to **GitHub Actions**.
2. Go to **Settings → Secrets and variables → Actions**:
   - Add the secret `ANTHROPIC_API_KEY` (from console.anthropic.com). Only Andrew handles this key.
   - Add the variable `NEWS_SITE_URL`: either `https://news.emberseedautomation.com.au`, or the GitHub Pages address to start with.
3. Custom address (optional):
   - In Squarespace → Domains → DNS, add a CNAME record: host `news`, value `andrewgardneremberseed.github.io`.
   - Then put `news.emberseedautomation.com.au` in **Settings → Pages → Custom domain** and tick **Enforce HTTPS**.
4. To make the first edition straight away, go to **Actions → Nightly AI news → Run workflow**.

## Fixing a story

Edit `data/YYYY-MM-DD.json`, then run the workflow again. The pages and the feed are rebuilt from the data files.

## Testing locally

- `python3 build.py --no-ai --force` checks the feeds and pages without calling Claude.
- `ANTHROPIC_API_KEY=... python3 build.py --force` does a full run.

## Costs

GitHub Actions and Pages are free for a public repository. Claude costs a few cents a night. To make it cheaper, set the repository variable `NEWS_MODEL` to `claude-sonnet-5-5`.
