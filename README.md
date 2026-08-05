# Korean Kanakaraju – SVC Kurnool Ticket Tracker

This project checks the District movie page approximately every five minutes for:

- **Movie:** Korean Kanakaraju
- **Theatre:** SVC Cinemas, City Square Mall, Kurnool
- **Date:** August 6, 2026
- **Shows:** Any released showtime

It sends a Telegram alert when showtimes first appear and sends another alert only when additional showtimes are detected.

## 1. Create a GitHub repository

1. Sign in to GitHub.
2. Select **New repository**.
3. Name it `korean-kanakaraju-ticket-tracker`.
4. A public repository is simplest for free GitHub Actions usage.
5. Create the repository without adding starter files.

## 2. Upload this project

Extract the ZIP on your phone or computer.

In the new GitHub repository:

1. Select **Add file → Upload files**.
2. Upload everything inside the extracted folder.
3. Make sure `.github/workflows/ticket-check.yml` is included.
4. Commit the files to the `main` branch.

If GitHub's mobile uploader does not show the hidden `.github` folder, use the GitHub website in desktop mode or upload the ZIP contents with GitHub Desktop.

## 3. Add Telegram secrets

Open:

**Repository → Settings → Secrets and variables → Actions → New repository secret**

Create these two secrets:

### `TELEGRAM_TOKEN`

Paste the complete token supplied by `@BotFather`.

### `TELEGRAM_CHAT_ID`

Paste the numeric chat ID found through:

`https://api.telegram.org/botYOUR_TOKEN/getUpdates`

Before using `getUpdates`, open your bot, press **Start**, and send `hello`.

Never put the Telegram token directly in `tracker.py`.

## 4. Enable GitHub Actions

Open the repository's **Actions** tab.

If GitHub asks you to enable workflows, select **I understand my workflows, go ahead and enable them**.

## 5. Test Telegram

1. Open **Actions**.
2. Choose **Korean Kanakaraju Ticket Tracker**.
3. Select **Run workflow**.
4. Enable **Send only a Telegram test message**.
5. Select **Run workflow**.

You should receive:

`GitHub ticket tracker test successful.`

## 6. Run a real manual check

Run the workflow again, but leave the Telegram-test option disabled.

Open the workflow run and inspect the `Check tickets` logs. Before booking opens, a normal result is:

- Movie found: true
- Theatre found: false, or true
- Target date found: false, or true
- Detected showtimes: []
- Available: false

No Telegram booking alert is sent until the correct theatre, date and at least one showtime are all detected.

## 7. Automatic schedule

The workflow contains:

```yaml
schedule:
  - cron: "*/5 * * * *"
```

GitHub schedules are expressed in UTC, but every-five-minutes is the same in all time zones. GitHub may occasionally delay scheduled jobs during busy periods.

## Duplicate-alert protection

The workflow caches `state/ticket_state.json`.

Example:

1. `7:00 PM` and `10:30 PM` appear → one alert.
2. Same times remain → no repeated alert.
3. `4:00 PM` is added later → another alert only for the newly detected time.

## Diagnostics

When the tracker fails, the workflow uploads a temporary diagnostic artifact containing:

- `result.json`
- `theatre-section.txt`
- `page.png`, when a screenshot could be captured

Open the failed Actions run and download the debug artifact from its **Artifacts** section.

## Important limitations

District can change its page layout or anti-bot protections. A browser tracker cannot be guaranteed permanently reliable. Verify the first manual run before relying on it.

Scheduled GitHub Actions runs can start later than the exact cron time, especially during periods of high load.

## Stop the tracker

After August 6:

1. Open **Actions**.
2. Select the workflow.
3. Use the menu to disable the workflow.

Alternatively, delete `.github/workflows/ticket-check.yml`.
